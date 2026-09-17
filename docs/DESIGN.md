# Agent Messaging Hub — 设计文档

## 1. 项目概述

Mansio 是一个面向 LLM/Agent 的消息中枢系统，为多智能体协作提供统一的通信基础设施。本项目是博士论文 *"Enabling Agentic AI at Scale through Decoupled Abstractions"* 中 Messaging 组件（第 9 章）的参考实现。

### 核心能力

- Agent 间通信（群聊 / 私聊）
- 笔记 / 备忘录（Notebook / Scratch Pad）
- 消息历史持久化
- 记忆存储（Memory）
- 认知过程记录（Thought）
- 广播 / 公告

### 设计原则

| 原则 | 说明 |
|------|------|
| **解耦抽象** | 所有组件通过 Protocol 接口定义，不与具体实现绑定 |
| **分层职责** | 每层有明确边界：Backend 管存储与投递，Bus 管编排，Client SDK 管业务语义 |
| **部署正交** | 用哪种 backend 是服务端启动时的决策，与架构设计正交；Agent 不受影响，始终使用同一套 HTTP API |
| **渐进增强** | 核心功能最小化，高级能力通过可选接口渐进引入 |

---

## 2. 系统架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Delivery Layer                          │
│                 MCP / CLI / REST API                        │
│  （将 Client SDK 能力暴露给外部消费者：LLM、人类、脚本）       │
├─────────────────────────────────────────────────────────────┤
│                    Client SDK Layer                          │
│              mansio_client.MansioClient                     │
│  （有状态封装：身份、游标、channel 命名、业务语义 API；        │
│    通过 HTTP 与 Frontend 通信）                              │
├─────────────────────────────────────────────────────────────┤
│                    Frontend Layer                            │
│              HttpFrontend │ IrcFrontend                      │
│  （网络服务层：REST + SSE，挂载到 Bus）                     │
├─────────────────────────────────────────────────────────────┤
│                       Bus Layer                             │
│                         Bus                                 │
│  （编排层：包装 Backend，生成消息 ID，提供进程内 pub/sub      │
│    与压缩策略）                                              │
├─────────────────────────────────────────────────────────────┤
│                     Backend Layer                           │
│      SQLite │ Memory │ NATS JetStream │ Maildir             │
│  （消息存储与投递，通过 Backend 抽象基类统一）                  │
└─────────────────────────────────────────────────────────────┘
```

Frontend 层定义了 `Frontend` 协议，包含 `attach(bus)` / `serve_forever()` / `shutdown()` 方法和 `address` 属性。`HttpFrontend` 提供 REST 端点和 SSE 实时流；`IrcFrontend` 把 channel 桥接到 IRC 服务器。`MansioServer` 是将 Bus 绑定到一个或多个 Frontend 的编排器。

每一层只依赖其下一层的接口，不依赖具体实现。

### 2.2 组件关系

两个包沿网络边界划分。`mansio`（服务端）包含 Backend、Bus 和 Frontend；`mansio-client`（Agent SDK）包含 `MansioClient` 及其 HTTP 传输层。`mansio-client` 不导入 `mansio` 中的任何内容。

```
mansio_client.MansioClient("http://host:8742", user_id, token=…)
  │
  └── HttpTransport → HttpFrontend → Bus → Backend
        （REST + SSE）  （通过 MansioServer 或 `mansio serve`）
```

HTTP 是 Agent 唯一可用的传输方式：不存在进程内客户端。若某个进程想省掉这次网络跳转，可以直接内嵌一个 `Bus` 并直接调用它，而不经过 Client SDK（见 §6）。

---

## 3. 核心组件

### 3.1 消息模型（Message）

消息是系统中的基本数据单元，所有通信都通过消息完成。

```python
@dataclass(frozen=True)
class Message:
    id: str              # UUID v7（时间有序），用作 cursor
    channel: str         # channel 名称
    sender: str          # 发送者 user_id
    msg_type: str        # 应用层消息类型
    payload: str         # 消息内容（JSON 字符串或纯文本）
    timestamp: str       # ISO 8601 时间戳
    metadata: dict | None  # 可选扩展字段
    parent_id: str | None  # 被回复消息的 ID
    thread_id: str | None  # 线程根消息 ID，用于扁平化线程查询
    intent: str | None     # 语义意图标签（见 §3.10）
```

**设计决策**：

- Message 是不可变的（frozen dataclass）
- `id` 使用 UUID v7 保证时间有序，作为 poll 的 cursor
- `msg_type` 是自由字符串，语义由 Client SDK 层定义
- `metadata` 用于携带结构化扩展信息（如 display_name、tags 等）
- `parent_id` / `thread_id` 支持回复链和扁平化线程查询（§3.2）
- `intent` 是自由字符串，用于语义层面的轮次协调提示（§3.10）

### 3.2 Backend 层

Backend 是消息的存储与投递引擎。所有 Backend 都继承同一个抽象基类，系统不假设底层是关系数据库、消息队列、邮箱目录还是内存结构。

#### Backend 抽象基类

`Backend`（位于 `protocols.py`）把接口分为两部分：每个 backend 必须实现的抽象方法，以及已有默认实现、backend 可按需覆写以提升效率的模板方法：

```python
class Backend(ABC):
    # 抽象方法 —— 必须实现
    def store(self, message: Message) -> None: ...
    def store_queue(self, message: Message) -> None: ...
    def query(self, channel, after=None, limit=100, msg_type=None,
              order="oldest", thread_id=None, intent=None,
              offset=0) -> list[Message]: ...
    def get_message(self, message_id: str) -> Message | None: ...
    def list_channels(self) -> list[str]: ...
    def queue_claim(self, channel, claimed_by, *,
                    lease_seconds=300) -> ClaimResult | None: ...
    def queue_ack(self, message_id, claimed_by) -> ClaimResult | None: ...
    def queue_status(self, message_id: str) -> dict | None: ...

    # 模板方法 —— 已提供默认实现
    def close(self) -> None: ...
    def search(...) -> list[Message]: ...
    def message_count(self, channel=None) -> int: ...
    def stats(self) -> dict: ...
    def queue_stats(self, channel=None) -> dict: ...
    def queue_retire(self, max_age_seconds=86400, max_per_channel=1000) -> int: ...
    def recent_timestamps(self, seconds=60) -> list[str]: ...
    def info(self) -> dict: ...
    def list_channels_detail(self) -> list[dict]: ...
```

#### 可选能力协议

并非所有存储都能支持的能力，以独立的 `runtime_checkable` 协议表达，而不是放进抽象方法。Bus 用 `isinstance` 检查这些协议，当 backend 缺少所需能力时抛出 `NotImplementedError`：

| 协议 | 方法 | 用途 |
|------|------|------|
| `ChannelStore` | `create_channel`、`get_channel`、`list_channels_meta`、`update_channel`、`delete_channel_meta`、`set_acl`、`get_acl`、`add_acl_entry`、`remove_acl_entry`、`check_access` | 显式的 channel 元数据（owner、可见性）与按 Agent 的 ACL |
| `Deletable` | `delete_channel`、`delete_message` | 删除 channel 与单条消息 |
| `Presenceable` | `heartbeat`、`users`、`user_status` | Agent 在线状态跟踪 |
| `Compactable` | `compact` | Channel 压缩（按条数裁剪、按 sender 去重） |

> **注**：`subscribe`/`unsubscribe` 在 Bus 层以 in-process observer 模式实现，作为所有 backend 的通用基线。Backend 接口本身不含订阅方法。

#### 可用 Backend 实现

| Backend | 构造方式 | 可选协议 | 适用场景 |
|---------|----------|----------|----------|
| `SQLiteBackend` | `SQLiteBackend("mansio.db")` 或 `":memory:"` | ChannelStore、Deletable、Presenceable、Compactable | 开发、测试、单机部署、零外部依赖 |
| `MemoryBackend` | `MemoryBackend()` | ChannelStore、Deletable、Presenceable、Compactable | 单元测试、临时场景 |
| `NATSBackend` | `NATSBackend("nats://host:4222")` | Presenceable、Compactable | 基于 NATS JetStream 的分布式部署（可选 `nats` 依赖） |
| `MaildirBackend` | `MaildirBackend("/var/mansio")` | Deletable、Presenceable、Compactable | 文件系统原生存储，可用普通邮件工具查看 |

> **选择建议**：Backend 之间没有优先级顺序。根据部署场景选择：单元测试用 Memory，开发与单机生产用 SQLite（零依赖），多实例部署用 NATS JetStream，需要让既有邮件工具直接读取消息时用 Maildir。不支持 `ChannelStore` 的 backend 无法提供 channel 元数据与 ACL 相关端点。

Redis Streams 与 AMQP backend 仍在路线图上（§9），目前没有任何实现。

#### 扩展新 Backend

继承 `Backend`，实现其抽象方法，再按存储自身能力加上相应的可选协议 —— 这些协议是结构化的，不要求显式继承（已有的 backend 仍把它们写在基类列表里，作为文档）：

```python
from mansio.protocols import Backend, Deletable

class MyBackend(Backend, Deletable):
    def __init__(self, connection_url: str): ...

    # 必须实现
    def store(self, message: Message) -> None: ...
    def store_queue(self, message: Message) -> None: ...
    def query(self, channel, after=None, limit=100, msg_type=None,
              order="oldest", thread_id=None, intent=None, offset=0): ...
    def get_message(self, message_id: str) -> Message | None: ...
    def list_channels(self) -> list[str]: ...
    def queue_claim(self, channel, claimed_by, *, lease_seconds=300): ...
    def queue_ack(self, message_id, claimed_by): ...
    def queue_status(self, message_id: str) -> dict | None: ...

    # 来自 Deletable
    def delete_channel(self, channel: str) -> int: ...
    def delete_message(self, message_id: str) -> bool: ...

# 使用
bus = Bus(backend=MyBackend("custom://..."))
```

### 3.3 Metadata 编码

`Message.metadata` 是可选的 `dict`。各 backend 自行决定如何在其存储格式中编码 —— SQLite 存为 JSON 字符串列，Maildir 存为 JSON 格式的 `X-Mansio-Metadata` 邮件头，NATS 存为 JSON 消息体中的一个字段 —— 并在读取时解码回 `dict`。系统没有可插拔的 serializer 抽象；需要其他编码方式的 backend 在内部自行选择。

### 3.4 Bus 层

Bus 是编排层，包装一个 Backend，提供统一的消息发布/查询接口。

```python
class Bus:
    def __init__(
        self,
        backend: Backend | None = None,   # 默认 SQLiteBackend(":memory:")
        *,
        compaction_policy: CompactionPolicy | None = None,
    ): ...

    # 核心操作
    def publish(self, channel, sender, msg_type, payload, metadata=None,
                *, queue=False, parent_id=None, intent=None,
                enforce_acl=False) -> str
    def query(self, channel, after=None, limit=100, ...) -> list[Message]
    def subscribe(self, channel, callback) -> str
    def unsubscribe(self, subscription_id) -> None
    def channels(self, *, detail=False) -> list[str] | list[dict]

    # 能力受限操作（委托给 backend 的可选协议）
    def create_channel / get_channel_meta / check_access / get_acl / set_acl / …
    def delete_channel / delete_message
    def heartbeat / users / user_status
    def compact
    def queue_claim / queue_ack / queue_status

    # 生命周期
    def close(self) -> None
    def __enter__ / __exit__  # context manager

    # 属性
    @property backend -> Backend
    @property metrics -> MetricsCollector
```

`SQLiteBus(db_path)` 是便捷子类，等价于 `Bus(backend=SQLiteBackend(db_path))`。

**Bus 层的职责边界**：

- ✅ 消息 ID 生成（UUID v7）
- ✅ 时间戳生成
- ✅ 将消息路由到 Backend
- ✅ In-process pub/sub（作为通用基线）
- ✅ 吞吐量指标采集
- ✅ 发布后对 system channel 执行压缩策略
- ✅ 可选的发布期 ACL 检查（`enforce_acl=True`），委托给 backend 的 `ChannelStore`
- ❌ 不做认证（由 Frontend 通过 `TokenStore` 负责）
- ❌ 不做 channel 命名验证（由 Frontend 负责）
- ❌ 不管理 agent 身份（由 Client SDK 负责）
- ❌ 不追踪 cursor 状态（由 Client SDK 负责）

### 3.5 Client SDK 层（MansioClient）

`mansio_client.MansioClient` 是面向 agent/LLM 的核心接口，提供有状态的消息操作封装。

#### 3.5.1 连接模型

构造函数接受服务端 URL 和 Agent 身份，没有本地模式：

```python
from mansio_client import MansioClient

client = MansioClient(
    "http://mansio:8742",   # 服务端 URL（http:// 或 https://）
    "coder-1",              # user_id
    token="mst-xxx",        # bearer token；仅在 --no-auth 服务端可省略
    display_name="Code Bot",
)
```

客户端内部持有一个 `HttpTransport`，与 Frontend 的 REST + SSE API 通信。Transport 是纯内部抽象，用户不直接接触。

构造时客户端会先发布一条上线通告（向 `_system:agents` 写一条 `presence` 消息），再恢复已保存的 cursor；两步都是尽力而为，若服务端拒绝则静默跳过。

#### 3.5.2 身份与认证

##### 身份模型

```
user_id       唯一 Agent 标识，用户自选，格式约束
              ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$（3-64 字符，小写字母、
              数字与连字符；客户端与服务端都会校验）
token         服务端签发的 bearer 凭证，`mst-` + 48 位十六进制字符，
              服务端以 SHA256 哈希存储
display_name  可选显示名，可重复，默认等于 user_id
```

类比微信：user_id ≈ 微信号（唯一），display_name ≈ 昵称（可重复）。

##### 令牌签发

令牌保存在 `TokenStore`（服务端持有的一张 SQLite 表）中，而不是某个消息 channel 里。运维人员通过管理面板 UI 或其 REST API 创建 Agent 及其首个令牌：

```bash
curl -X POST http://localhost:8741/api/users \
     -H 'Content-Type: application/json' \
     -d '{"user_id": "coder-1", "label": "initial token"}'
# → {"ok": true, "user_id": "coder-1", "token": {"token": "mst-…", …}}
```

其余端点覆盖列举（`GET /api/users`、`GET /api/tokens`）、为同一 Agent 追加令牌（`POST /api/users/<user_id>/tokens`）、轮换（`POST /api/tokens/<token_id>/rotate`）与吊销（`DELETE`）。令牌明文只在创建时返回一次。

##### 强制执行

认证由 Frontend 负责，而非 Bus。`HttpFrontend` 用 `TokenStore` 校验 `Authorization: Bearer` 头；当服务端以 `--no-auth` 启动时，它在构造时不带 token store，所有请求一律放行。

绑定了 `user_id` 的令牌只能以该 Agent 身份操作。未绑定 `user_id` 的令牌是 **supertoken**：可以以任意 Agent 身份操作、写入 `broadcast:*` channel，并绕过 `_system:*` 的写入限制。管理 API 要求必须提供 `user_id`，因此 supertoken 需要用代码创建：`TokenStore.create_token(user_id=None)`（[#299](https://github.com/Oaklight/mansio/issues/299)）。

冒号是保留字符，不能出现在用户自建的 channel 名中。

#### 3.5.3 Channel 类型与命名

Channel 命名规则在 **Frontend（服务端）层强制执行**。Client SDK 只在本地校验 `user_id`，channel 名由服务端检查，格式非法时返回 **400 Bad Request**。Bus 层保持通用，不校验 channel 名。

##### 命名规则

- 长度：3–64 字符
- 必须以字母开头
- 必须以字母或数字结尾（不能以下划线、连字符或点号结尾）
- 不允许大写字母
- 不允许冒号（保留给系统前缀如 `_system:`）
- 不允许连续特殊字符（如 `..`、`--`、`__`）
- 正则表达式：`^(?=[^\W\d_])[\w.-]{1,63}[^\W_]$`

> **注：** 带保留前缀的频道（如 `_system:cursors:coder-1`、`notebook:coder-1`）由 Client SDK 自动构造，不受上述通用规则约束。通用命名规则适用于用户自定义的频道名（如普通频道名、broadcast 的 topic 部分）。

| Channel 类型 | 命名模式 | 用途 | 访问控制 |
|-------------|----------|------|----------|
| 普通频道 | `general`、`build-status` 等 | 公开群聊，默认情形 | 任意已认证 agent 可读写 |
| Notebook | `notebook:{user_id}` | 思考过程、临时笔记（含 Thought） | **私有 — 仅所属 agent 可写入**，跨 agent 写入返回 403 |
| Memory | `memory:{user_id}` | 长期记忆（语义记忆） | **私有 — 仅所属 agent 可写入**，跨 agent 写入返回 403 |
| Broadcast | `broadcast:{topic}` | 公告、任务列表、成员列表 | **仅 supertoken 可写入**，普通 agent 只读 |
| DM | `dm:{user_a}:{user_b}` | 私聊（双方 ID 按字典序排列） | 双方可读写 |
| System | `_system:{purpose}` | 内部管理（在线状态、cursor、通知） | **受限写入**（见下文） |
| 自定义前缀 | `{prefix}:{name}` | 应用自定义命名空间，如 `group:` | 与普通频道相同，但该前缀须先在管理面板注册（`/api/prefixes`） |

冒号是保留字符：含冒号的 channel 名会被拒绝，除非其前缀是上表中的保留前缀，或已注册为自定义前缀。

##### System Channel 鉴权

`_system:*` 频道对普通 agent 有写入限制，以防止滥用系统频道：

| 允许的 System Channel | 写入者 | 用途 |
|----------------------|--------|------|
| `_system:agents`（仅限 `msg_type="presence"`） | 任意 agent | Presence（在线状态） |
| `_system:cursors:{自己的 user_id}` | 对应 agent | Cursor 快照持久化 |

- 其他 `_system:*` 频道的写入请求返回 **403 Forbidden**
- Supertoken 不受此限制，可写入任意 `_system:*` 频道

##### 私有频道所有权

- `notebook:X` 和 `memory:X` 频道只能由 agent X 写入
- 其他 agent 尝试写入将返回 **403 Forbidden**

##### Broadcast 频道写入限制

- `broadcast:*` 频道只有持有 supertoken 的客户端可以写入
- 普通 agent 写入将返回 **403 Forbidden**

##### 通道 ACL 条目

上述规则都是结构性的：它们由通道名和令牌的作用域推导而来，也是 `HttpFrontend` 在发布、查询、订阅和删除时实际执行的规则。通过 `/v1/channels/<channel>/acl` 管理的按通道 ACL 条目由 backend 的 `ChannelStore` 存储，并只在 ACL 相关端点上生效 —— Agent 需要对某个通道拥有 `admin` 权限，才能读取或修改该通道的 ACL。消息路由调用 `Bus.publish` 和 `Bus.query` 时并未传入触发 ACL 检查的参数，因此目前 ACL 条目既不会授予也不会收回对消息的访问权限。也正因如此，无法用这种方式授予 `broadcast:*` 的写入权限（[#298](https://github.com/Oaklight/mansio/issues/298)）。

##### Notebook vs Memory（认知心理学视角）

```
┌──────────────────────────────────────────────────────────┐
│                  Agent 认知系统                            │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  ┌─────────────────────┐    ┌─────────────────────┐      │
│  │  Notebook Channel   │    │   Memory Channel    │      │
│  │  (情景/工作记忆)     │    │   (语义/长期记忆)    │      │
│  ├─────────────────────┤    ├─────────────────────┤      │
│  │ • Note (普通笔记)    │    │ • Memory (知识/事实) │      │
│  │ • Thought (思考过程) │───▶│   - fact             │      │
│  │   - reasoning       │提炼 │   - experience       │      │
│  │   - planning        │    │   - decision         │      │
│  │   - reflection      │    │   - preference       │      │
│  │   - brainstorming   │    │                      │      │
│  ├─────────────────────┤    ├─────────────────────┤      │
│  │ 性质：过程性、临时   │    │ 性质：结果性、持久    │      │
│  │ 类比：草稿纸        │    │ 类比：知识库          │      │
│  └─────────────────────┘    └─────────────────────┘      │
│                                                           │
└──────────────────────────────────────────────────────────┘
```

| 维度 | Notebook (Episodic) | Memory (Semantic) |
|------|---------------------|-------------------|
| **记忆类型** | 情景记忆 / 工作记忆 | 语义记忆 / 长期记忆 |
| **内容** | 思考过程、临时笔记、草稿 | 提炼的结论、事实、知识 |
| **结构** | 可以是杂乱的思维流 | 应该是结构化、简洁的 |
| **时效性** | 可丢弃、可自动过期 | 持久保留 |

#### 3.5.4 API 设计

MansioClient 采用 **资源\_动作** 命名风格（`resource_action`），兼顾 SDK 调用的可读性和作为 MCP/CLI tool 暴露时的直观性。

##### 核心 API：Channel 操作

所有通信的基础，直接操作 channel：

```python
# 发送消息到指定 channel
channel_send(channel: str, content: str, msg_type: str = "chat",
             metadata: dict | None = None, parent_id: str | None = None,
             intent: str | None = None) -> str

# 读取 channel 消息（随机访问，不推进 cursor）
channel_read(channel: str, limit: int = 10, after: str | None = None,
             order: Literal["oldest", "newest"] = "newest",
             thread_id: str | None = None) -> list[Message]

# 增量轮询（cursor 自动推进，用于持续跟踪新消息）
channel_poll(channel: str) -> list[Message]

# 列出所有 channel；detail=True 时返回带元数据的字典列表
channel_list(*, detail: bool = False) -> list[str] | list[dict]
```

##### 语义 API：高层业务操作

以下方法是 channel 操作的语义封装（syntactic sugar），自动路由到对应 channel 并设置正确的 `msg_type`：

```python
# ── Notebook（写入 notebook:{user_id}）──
note_write(content: str, tags: list[str] | None = None) -> str
note_read(tags: list[str] | None = None, limit: int = 10) -> list[Message]

# ── Thought（写入 notebook:{user_id}，msg_type="thought"）──
thought_record(
    thought_process: str,
    *,
    thinking_mode: str | None = None,  # reasoning | planning | reflection |
                                       # recalling | brainstorming | exploring
    focus_area: str | None = None,
) -> str
thought_read(limit: int = 10) -> list[Message]

# ── Memory（写入 memory:{user_id}）──
memory_store(content: str, memory_type: str = "general") -> str
memory_recall(query: str, limit: int = 5) -> list[Message]
# memory_recall 的语义搜索能力由外部组件提供（如 mem0），
# Client SDK 层仅提供接口定义，默认实现为按时间倒序返回。

# ── DM（写入 dm:{sorted_pair}）──
dm_send(to_user: str, content: str) -> str
dm_read(with_user: str, limit: int = 10) -> list[Message]

# ── Broadcast（读取 broadcast:{topic}）──
broadcast_list() -> list[str]
broadcast_read(topic: str, limit: int = 10) -> list[Message]

# ── Notification（轮询 _system:notifications:{user_id}）──
notification_check() -> list[Message]
```

群聊不需要专门的方法：一个群就是一个普通 channel，通过 `channel_send` / `channel_poll` 使用。

除语义封装外，客户端还提供工作队列（`queue_publish`、`queue_claim`、`queue_ack`、`queue_status`）、在线状态（`heartbeat`、`users`、`user_status`）、频道管理（`channel_create`、`channel_delete`、`message_delete`、`acl_get` / `acl_set` / `acl_add` / `acl_remove`、`registry_lookup`）以及实时投递（`subscribe`、`unsubscribe`、`listen`）。

##### 语义 API 与 Channel 操作的映射关系

```
note_write(content, tags)
  → channel_send(f"notebook:{self.user_id}", content, msg_type="note",
                  metadata={"tags": tags})

thought_record(process, thinking_mode=mode, focus_area=focus)
  → channel_send(f"notebook:{self.user_id}", process, msg_type="thought",
                  metadata={"thinking_mode": mode, "focus_area": focus})

memory_store(content, memory_type)
  → channel_send(f"memory:{self.user_id}", content, msg_type="memory",
                  metadata={"memory_type": memory_type})

dm_send(to_user, content)
  → channel_send(f"dm:{sorted_pair}", content, msg_type="chat")
```

#### 3.5.5 Cursor 管理

MansioClient 维护 per-channel cursor，支持增量消息读取。

##### 两种读取模式

| 方法 | Cursor | 场景 |
|------|--------|------|
| `channel_poll(channel)` | ✅ 自动推进 | 持续跟踪新消息（主要使用方式） |
| `channel_read(channel, ...)` | ❌ 不推进 | 随机访问、查看历史、按条件检索 |

##### Cursor 持久化

Cursor 状态存储在 `_system:cursors:{user_id}` channel 中，实现 cross-session 恢复：

```python
# close() 时把 cursor 映射作为一条消息持久化
channel_send(
    f"_system:cursors:{self.user_id}",
    json.dumps(self._cursors),  # {"channel_a": "last_msg_id", ...}
    msg_type="cursor_snapshot",
)

# 构造时从 channel 读取最新快照恢复
```

**Cross-session 恢复流程**：

```
Agent 死亡
  → 重新 spawn
  → 用同一个 user_id + token 创建 MansioClient
  → _announce() 向 _system:agents 写入一条 presence 消息
  → _restore_cursors() 从 _system:cursors:{user_id} 读取最新快照
  → channel_poll() 从断点继续
```

快照会在 cursor channel 中累积；Bus 的压缩策略只保留每个 Agent 最新的一条。

### 3.6 Frontend 层与 MansioServer

Frontend 层提供挂载到 Bus 的网络服务。

#### Frontend 协议

```python
class Frontend(Protocol):
    def attach(self, bus: Bus) -> None:
        """将此 Frontend 绑定到 Bus 实例。"""
        ...

    def serve_forever(self) -> None:
        """启动服务（阻塞）。"""
        ...

    def shutdown(self) -> None:
        """停止接受连接并释放资源。"""
        ...

    @property
    def address(self) -> tuple[str, int]:
        """当前监听的 (host, port)。"""
        ...
```

#### HttpFrontend

主要的 Frontend 实现，也是 Agent 唯一连接的入口，提供：
- **REST API**（`/v1/` 前缀）— 发布、查询、列出通道、通道元数据与 ACL、删除消息与通道、工作队列的 claim/ack/status、在线状态、注册表查询、管理端清理与压缩
- **SSE（Server-Sent Events）**— 通过 `/v1/subscribe` 与 `/v1/channels/<channel>/subscribe` 实现实时消息流
- **认证** — 基于 `TokenStore` 的 bearer token 校验，以及 §3.5.3 描述的通道名、payload 与访问控制检查
- `/health` — 无需认证的存活探针

```python
HttpFrontend(host="127.0.0.1", port=8742, cors_origin="*",
             max_body_bytes=1_048_576, max_query_limit=10_000,
             token_store=None)
```

#### IrcFrontend

把 mansio channel 桥接到 IRC 服务器，便于人类用普通 IRC 客户端旁观或参与 Agent 之间的对话。需要可选的 `irc` 依赖。

#### MansioServer

将 Bus 绑定到一个或多个 Frontend 的编排器。只有一个 Frontend 时在调用线程中提供服务；有多个时，每个 Frontend 各占一个线程：

```python
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="0.0.0.0", port=8742))
server.serve_forever()
```

`mansio serve` 命令正是对这段代码的封装，另外负责构造 backend、token store 和管理面板。

#### HttpTransport

HttpFrontend 的客户端对应物，由 `MansioClient` 内部使用：

```python
client = MansioClient("http://mansio:8742", "agent-1", token="mst-xxx")
# → 内部使用 HttpTransport
```

### 3.7 管理面板

管理面板提供用于 Bus 检查、监控和令牌管理的 HTTP 仪表板与 REST API。它运行在独立端口（默认 8741）上，与面向 Agent 的 HTTP 前端分开：

```
admin/
├── server.py      # AdminServer —— 路由、生命周期、REST API
├── auth.py        # 密码登录与会话 cookie
├── metrics.py     # 进程内吞吐量采集
├── static.py      # 静态资源服务
└── admin.html     # 单页仪表板 UI
```

路由分组：会话（`/api/login`、`/api/logout`、`/api/auth-check`）、检查（`/api/stats`、`/api/stats/throughput`、`/api/channels`、`/api/messages`、`/api/subscriptions`、`/api/system`）、身份（`/api/users`、`/api/tokens`、`/api/prefixes`）。

### 3.8 Delivery 层

Delivery 层将 Client SDK 的能力暴露给外部消费者。

```
┌────────────────────────────────────────────────────┐
│              mansio_client.MansioClient            │
├─────────────────┬─────────────────┬────────────────┤
│   mansio-mcp    │  mansio-client  │  mansio client │
│   MCP 服务器    │      CLI        │  CLI（服务端   │
│                 │                 │  包内）        │
│  LLM 通过 MCP   │  LLM 或人类     │  运维人员做    │
│  工具调用       │  通过 shell     │  连通性验证    │
└─────────────────┴─────────────────┴────────────────┘
```

#### 命令入口

| 命令 | 所属包 | 目标用户 | 用途 |
|------|--------|---------|------|
| `mansio serve` | `mansio` | 运维人员 | 启动 Bus + 管理面板（可选 HTTP/IRC 前端） |
| `mansio client send/poll/channels/dm` | `mansio` | 运维人员 | 最小客户端，用于验证服务端是否正常 |
| `mansio-client` | `mansio-client` | 通过 bash 工具的 LLM、人类 | 完整的 SDK-over-CLI：消息、笔记、记忆、通道与 ACL 管理 |
| `mansio-mcp` | `mansio-client` | 通过 MCP 的 LLM | 把客户端操作暴露为 MCP 工具（JSON-RPC stdio） |

两个客户端入口都先读命令行参数，再回退到环境变量。`mansio-client` 使用 `--server` / `--user-id` / `--token`，其中 `-a` / `--agent` 作为 `--user-id` 的已弃用写法保留，使用时总会发出 `DeprecationWarning`；`mansio-mcp` 使用 `--url` / `--user-id` / `--token` / `--display-name`。共用的环境变量是 `MANSIO_URL`、`MANSIO_USER_ID`、`MANSIO_TOKEN`，以及仅 `mansio-mcp` 使用的 `MANSIO_DISPLAY_NAME`。`MANSIO_AGENT_ID` 和 `PIAZZA_*` 仍作为已弃用的别名被接受，并会发出 `DeprecationWarning`。

### 3.9 推送集成（多 Agent 消息感知）

为了让 Agent 及时感知其他 Agent 的新消息，Mansio 提供三层递进的集成方案：

| 层级 | 阶段 | 机制 | 可靠性 | 适用框架 |
|------|------|------|--------|----------|
| 1 | MCP 工具 | Agent 通过 MCP 调用 `mansio_poll` | 依赖 Agent | 所有 MCP 兼容框架 |
| 2 | 框架适配器 | 框架级 hook 自动触发轮询 | 自动化 | Claude Code、Codex CLI、OpenClaw、opencode、Hermes、Pi |
| 3 | 指令驱动轮询 | AGENTS.md / 系统提示词指令 | 尽力而为 | 任意 LLM Agent |

**第 1 层**提供能力（MCP 工具：`mansio_poll`、`mansio_read`、`mansio_send`）。
**第 2 层**增加框架特定的自动化（会话启动 hook、定时任务、定期轮询）。
**第 3 层**是通用兜底方案：通过系统提示词指令让 Agent 在会话开始和任务间隙主动轮询。

推荐做法：将第 1 层与第 2 层（自动化）或第 3 层（指令驱动）结合使用，确保可靠的消息感知。

详见 `examples/instructions/` 中的各框架轮询模板。

### 3.10 意图标头（语义竞态缓解）

当多个 Agent 通过共享频道通信时，**语义竞态条件**会因时序不匹配而出现：
串话（Agent 回复了过时消息）、雪崩效应（一条消息触发所有 Agent 同时响应）、
以及上下文漂移（Agent 陷入礼貌循环或循环纠正）。

`Message` 上的 `intent` 字段提供轻量级的轮次协调提示，
Agent 和编排器可以用它来减少这些问题。

**建议值**（自由字符串，不强制）：

| Intent | 含义 |
|---|---|
| `REQUIRES_RESPONSE` | 发送者期望接收者回复 |
| `DIRECT_QUESTION` | 消息是针对特定 Agent 的提问 |
| `FYI_ONLY` | 仅通知，不需要回复 |
| `PASS_FLOOR` | 发送者主动让出发言权 |

**用法**：

- 通过 `Bus.publish()` 和 HTTP `/v1/publish` 端点的 `intent` 参数设置
- 通过 `Bus.query()` 和 `/v1/query?intent=...` 的 `intent` 参数过滤查询
- 作为一等字段存储，在 SQLite 中建索引以支持高效过滤
- 所有后端均支持 intent 的存储和查询

**设计决策**：

- intent 是自由字符串而非枚举，以便应用可以定义领域特定的值而无需修改协议
- 字段可选，默认为 `None` —— 现有客户端不受影响
- intent 是*提示*而非强制机制；编排器和 Agent 可以自行决定是否遵守

这是 [GitHub issue #102](https://github.com/Oaklight/mansio/issues/102) 中语义竞态缓解设计的
第一阶段。后续阶段可能增加发言权控制（turn locking）、延迟批量投递和循环检测。

### 3.11 联邦（实验性）

`FederationLink`（位于 `mansio-client`）连接两个 mansio 实例，提供频道复制和按需路由功能。它是**纯客户端组件** — 不需要任何服务端变更。

**能力：**

- **复制** — 通过 SSE 订阅实现两个实例间的持续双向、仅拉取或仅推送频道同步。
- **联邦路由** — 无状态的 `route_read` / `route_send` 代理到远程实例。

**已知限制（Phase 1）：**

- 仅支持两实例桥接。不支持多跳 mesh（A → B → C）；布尔型 `bridged` 元数据标志可防止两个实例间的无限循环，但会有意阻止消息继续转发。Mesh 拓扑需要 `visited_instances` 列表（推迟到 Phase 2）。
- 循环防护是客户端元数据约定，非服务端强制执行。
- 服务端无感知 — 服务端不知道消息是本地产生还是从其他实例桥接而来。

此组件为实验性质，API 可能在无预告的情况下变更。

---

## 4. 通信模式

### 4.1 同步 vs 异步

| 场景 | 模式 | 说明 |
|------|------|------|
| 发消息给别人 | 异步 fire-and-forget | 像发 Slack/Email |
| 查询自己的 memory/notebook | 同步查询 | 读操作，不是消息传递 |
| 等待别人回复 | 异步 + 轮询/通知 | 提供 `notification_check()` |

**核心原则**：消息发送是异步的，数据查询是同步的。

### 通知机制

- **当前**：`notification_check()` 主动轮询，或用 `subscribe()` 走 SSE 推送
- **未来**：返回时附带通知（需要 Agent SDK 层支持）

### Broadcast Channel 管理

**当前**：Broadcast 由持有 supertoken 的一方（管理员 / API）发布。

**未来**：引入 Moderator Agent 机制 — Agent 提交到 `broadcast:submissions`，Moderator 审核后发布到对应 broadcast channel。

---

## 5. 消息类型

`msg_type` 是自由字符串，以下为约定的标准类型：

| 类型 | 说明 | 典型 channel |
|------|------|-------------|
| `chat` | 聊天消息 | 普通频道, dm:\* |
| `note` | 笔记/备忘 | notebook:\* |
| `thought` | 认知过程记录 | notebook:\* |
| `memory` | 记忆条目 | memory:\* |
| `broadcast` | 广播消息 | broadcast:\* |
| `task_request` | 任务请求 | 普通频道, dm:\* |
| `task_result` | 任务结果 | 普通频道, dm:\* |
| `notification` | 通知 | _system:notifications:\* |
| `presence` | 在线状态通告 | _system:agents |
| `cursor_snapshot` | Cursor 快照 | _system:cursors:\* |

### Thought 类型设计（借鉴 ThinkTool）

**设计理念**：让 agent 的思考过程从"黑盒"变为"白盒"。

```python
# 通过 thought_record() 写入
thought_record(
    "考虑了三种方案...",
    thinking_mode="reasoning",    # reasoning | planning | reflection | ...
    focus_area="API 设计选型",
)

# 底层存储为 Message:
# channel = "notebook:{user_id}"
# msg_type = "thought"
# payload = thought_process
# metadata = {"thinking_mode": "reasoning", "focus_area": "API 设计选型"}
```

---

## 6. 部署模式

每种部署形态都是同一个结构 —— 一个 Bus 加一个或多个 Frontend —— 区别只在于服务端跑在哪里、用哪个 backend。

### 6.1 本地开发

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741 --no-auth
```

- 一条命令，无需任何外部服务
- `--no-auth` 省去签发令牌；若管理面板监听地址超出 localhost 则拒绝启动
- 同机 Agent 连接 `http://localhost:8742`

### 6.2 共享服务

```bash
mansio serve --db /var/lib/mansio/mansio.db --http 0.0.0.0:8742 --remote
```

- 必须使用令牌；在管理面板为每个 Agent 各签发一个
- `--remote` 把管理面板绑定到公开地址并自动生成密码
- 任意机器上的 Agent 带令牌连接 `http://<host>:8742`

### 6.3 内嵌服务端

拥有总线的进程可以用代码构建服务端，而不必另起进程：

```python
bus = Bus(backend=SQLiteBackend("data.db"))
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="127.0.0.1", port=8742))
server.serve_forever()
```

该进程内的代码可以直接调用 `bus`，没有 HTTP 跳转，`subscribe` 回调同步触发；其他机器上的 Agent 仍通过 HTTP 接入。注意直接访问 Bus 会绕过 Frontend，连同它的所有通道校验与访问控制一起绕过。

### 6.4 分布式后端

若要让多个服务端实例共享同一份消息存储，给每个实例加上 `--nats nats://<host>:4222`。SQLite 的 WAL 模式支持单机多读多写，但受支持的配置是每个数据库文件只跑一个服务端进程。

---

## 7. 配置

### 7.1 客户端配置

Agent 的配置就是服务端 URL、user_id 和令牌 —— 可以写在代码里，也可以用命令行参数或环境变量：

```python
MansioClient("http://host:8742", user_id, token="mst-xxx")
```

```bash
export MANSIO_URL=http://host:8742
export MANSIO_USER_ID=coder-1
export MANSIO_TOKEN=mst-xxx
```

### 7.2 服务端配置

服务端完全通过 `mansio serve` 的命令行参数配置：后端选择（`--db` / `--maildir` / `--nats`）、前端（`--http`、`--irc` 及其选项）、管理面板（`--admin-port`、`--host`、`--admin-password`、`--remote`、`--no-ui`）、认证（`--no-auth`）以及 `--log-level`。

### 7.3 配置文件（未来）

服务端部署的 YAML/TOML 配置文件是未来可能的补充；目前每一项设置都是命令行参数。

---

## 8. 错误处理

### 8.1 输入验证

Frontend 层对所有写入操作执行以下验证，不合规的请求返回 HTTP 400：

| 验证规则 | 拒绝条件 |
|----------|----------|
| Payload 不得为空 | 空字符串或纯空白 payload 被拒绝 |
| Payload 必须是字符串 | 非字符串类型的 payload 被拒绝 |
| Limit 参数校验 | 查询时 `limit` 必须 >= 1 |
| Channel 名称校验 | 不符合命名规则的 channel 名被拒绝（见 §3.5.3） |

### 8.2 错误恢复策略

**当前策略**：客户端抛出 `MansioAPIError`，其中带有 HTTP 状态码和服务端返回的错误信息，由 agent 自行决定处理方式。普通请求不做重试。SSE 订阅是例外：其读取线程会无限重连，并从最后一个事件 ID 继续。

**未来扩展**：
- 死信队列（DLQ）
- 可配置重试策略
- 消息投递确认

---

## 9. 扩展路线

| 功能 | 说明 | 依赖 |
|------|------|------|
| Redis Streams / AMQP backend | 更多分布式存储 | Backend |
| 可插拔 serializer | 非 JSON 的 metadata 编码 | Backend |
| 消息 TTL | 按 channel 类型设置过期策略 | Backend |
| 消息追踪 | 分布式追踪 ID | Message metadata |
| 优先级队列 | 紧急消息插队 | Backend |
| 延迟消息 | 定时投递 | Backend |
| Moderator Agent | Broadcast 审核机制 | Client SDK |
| Async API | asyncio 支持 | 全栈 |
| 消息打断 | interrupt:{user_id} channel + 优先级 | Agent SDK 层 |
| Per-channel 别名 | 类似微信群名片 | Client SDK |

---

## 10. 决策记录

### D1: Backend 与 Storage 合并

**决策**：Backend = transport + persistence 一体，不单独抽象 Storage 层。

**理由**：已实现的每个 backend（SQLite、Memory、NATS JetStream、Maildir）都天然包含持久化，单独抽象一层 Storage 没有任何实现会用到。若未来出现纯传输型 backend（如 MQTT）需要独立存储，可在该 backend 内部组合，不影响 Backend 接口。

**演进路径**：当确实需要 transport 与 persistence 分离时（如 MQTT + PostgreSQL），可引入独立的 Storage Protocol，Backend 内部组合使用。当前 Backend 接口无需变更。

### D2: Channel 命名在 Frontend 层强制执行

**决策**：Channel 命名规则由 Frontend（服务端）层校验，Bus 层接受调用方传入的任意 channel 名。

**理由**：服务端校验能挡住任何客户端提交的非法频道名 —— 只写在 SDK 里的规则，只对守规矩的客户端有效。Bus 层因此保持通用，不嵌入业务语义。

### D3: 身份认证采用 user_id + 服务端签发令牌

**决策**：user_id 用户自选（格式约束），令牌由服务端生成并以哈希形式存于 `TokenStore`，认证在 Frontend 层强制执行。

**理由**：bearer token 是 HTTP API 的标准凭证，可按 Agent 单独吊销与轮换而不影响其身份，同时支持 cross-session 恢复（同 user_id + token 重连）。`--no-auth` 模式降低开发/测试门槛。

### D4: 注册表存储在 TokenStore 表中

**决策**：Agent 注册信息存放在服务端持有的 SQLite 表中，而非某个消息 channel。

**理由**：注册信息是凭证而非消息：它必须只能由运维人员写入、每次请求都按精确匹配查询、吊销时可删除。把令牌哈希当作消息存储，会让每个 Agent 都能读取并追加这份凭证存储。

### D5: Cursor 持久化在 _system channel

**决策**：Cursor 快照存储在 `_system:cursors:{user_id}` channel 中。

**理由**：复用消息存储机制，cross-session 恢复时从 channel 读取最新快照。无需额外的状态存储基础设施。

### D6: HTTP 是客户端唯一的传输方式

**决策**：`MansioClient` 只接受服务端 URL。没有进程内客户端，客户端一侧也不选择 backend；`mansio-client` 不依赖 `mansio`。

**理由**：只有一种传输方式，就只有一个地方运行校验、认证与访问控制，Agent 无法绕开它们触达 Bus。这也让 Agent SDK 可以在不安装服务端存储代码的情况下使用，并让本地与远程部署行为一致。确实需要进程内速度的进程可以内嵌 Bus 直接调用（§6.3），代价是绕过上述检查。

### D7: API 采用 resource_action 命名

**决策**：SDK 方法名采用 `resource_action` 风格（如 `channel_send`、`note_write`），同时作为 MCP/CLI tool 名暴露。

**理由**：资源+动作的命名在 LLM tool calling 中语义最清晰，且扁平命名适合作为 CLI 子命令和 MCP tool name。

### D8: 语义 API 是 Channel 操作的 Sugar

**决策**：`note_write`、`thought_record`、`memory_store` 等语义方法底层映射为 `channel_send` + 特定 channel + msg_type。

**理由**：保持系统核心极简（一切皆消息），高层语义由 Client SDK 提供便利封装。用户也可直接使用 channel 操作实现自定义逻辑。
