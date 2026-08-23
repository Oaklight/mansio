# Agent Messaging Hub — 设计文档

## 1. 项目概述

Mansio 是一个面向 LLM/Agent 的消息中枢系统，为多智能体协作提供统一的通信基础设施。本项目是博士论文 *"Enabling Agentic AI at Scale through Decoupled Abstractions"* 中 Messaging 组件（第 9 章）的参考实现。

### 核心能力

- Agent 间通信（群聊 / 私聊）
- 笔记 / 备忘录（Notebook / Scratch Pad）
- 历史记录追踪
- 记忆存储（Memory）
- 认知过程记录（Thought）
- 广播 / 公告

### 设计原则

| 原则 | 说明 |
|------|------|
| **解耦抽象** | 所有组件通过 Protocol 接口定义，不与具体实现绑定 |
| **分层职责** | 每层有明确边界：Backend 管存储与投递，Bus 管编排，Client SDK 管业务语义 |
| **连接字符串驱动** | 部署决策（用哪种 backend）与架构设计正交，通过 connection string 在运行时选择 |
| **渐进增强** | 核心功能最小化，高级能力通过可选接口渐进引入 |

---

## 2. 系统架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Delivery Layer                          │
│            MCP / REST API / CLI / OpenAPI                   │
│  （将 Client SDK 能力暴露给外部消费者：LLM、人类、脚本）       │
├─────────────────────────────────────────────────────────────┤
│                    Client SDK Layer                          │
│                     MansioClient                            │
│  （有状态封装：身份、游标、channel 命名、业务语义 API）         │
├─────────────────────────────────────────────────────────────┤
│                    Frontend Layer  🔄                        │
│            HttpFrontend / （未来：IRC、WS）                    │
│  （网络服务层：REST + SSE，挂载到 Bus）                     │
├─────────────────────────────────────────────────────────────┤
│                       Bus Layer                             │
│                         Bus                                 │
│  （编排层：组合 Backend + Serializer，提供 pub/sub）           │
├─────────────────────────────────────────────────────────────┤
│                     Backend Layer                           │
│          SQLite │ Redis │ RabbitMQ │ ...                     │
│  （消息存储与投递，通过 Protocol 接口统一抽象）                 │
└─────────────────────────────────────────────────────────────┘
```

> **🔄 开发中（`dev/agent-bus`）：** Frontend 层引入了 `Frontend` 协议，提供 `attach(bus)` / `serve_forever()` / `shutdown()` 方法。`HttpFrontend` 是首个实现，提供 REST 端点和 SSE 实时流。`MansioServer` 是将 Bus 绑定到一个或多个 Frontend 的编排器。

每一层只依赖其下一层的 Protocol 接口，不依赖具体实现。

### 2.2 组件关系

```
MansioClient(target)
  │
  ├── target = Bus 对象 → Bus → Backend
  ├── target = "mansio.db"  → 自建 Bus(SQLiteBackend)
  ├── target = "redis://..." → 自建 Bus(RedisBackend)
  └── target = "http://..."  → HttpTransport → HttpFrontend → Bus → Backend
                                                (通过 MansioServer)
```

Client SDK 通过 Transport 抽象屏蔽本地/远程差异，对上层完全透明。

> **注：** `Transport` 协议现已包含 `subscribe(channel, callback)` 和 `unsubscribe(subscription_id)` 方法，支持本地和远程传输的实时事件投递。

---

## 3. 核心组件

### 3.1 消息模型（Message）

消息是系统中的基本数据单元，所有通信都通过消息完成。

```python
@dataclass(frozen=True)
class Message:
    id: str              # UUID v7（时间有序），用作 cursor
    channel: str         # channel 名称
    sender: str          # 发送者 agent_id
    msg_type: str        # 应用层消息类型
    payload: str         # 消息内容（JSON 字符串或纯文本）
    timestamp: str       # ISO 8601 时间戳
    metadata: dict | None  # 可选扩展字段
```

**设计决策**：

- Message 是不可变的（frozen dataclass）
- `id` 使用 UUID v7 保证时间有序，作为 poll 的 cursor
- `msg_type` 是自由字符串，语义由 Client SDK 层定义
- `metadata` 用于携带结构化扩展信息（如 display_name、tags 等）

### 3.2 Backend 层

Backend 是消息的存储与投递引擎。所有 Backend 通过统一的 Protocol 接口对接，系统不假设底层是关系数据库、消息队列还是内存结构。

#### Backend Protocol

```python
class Backend(Protocol):
    """消息后端的统一接口。"""

    def store(self, message: Message) -> None:
        """持久化一条消息。"""
        ...

    def query(
        self, channel: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """按 channel 查询消息，支持基于 ID 的 cursor 分页。"""
        ...

    def channels(self) -> list[str]:
        """列出所有有消息的 channel。"""
        ...

    def subscribe(
        self, channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """注册消息投递回调，返回订阅 ID。

        不同 backend 按自身特性实现：
        - 轮询型 backend（SQLite）：Bus 层提供 in-process observer
        - 原生 pub/sub backend（Redis）：利用原生订阅机制
        - 消息队列 backend（RabbitMQ）：利用 consumer 机制
        """
        ...

    def unsubscribe(self, subscription_id: str) -> None:
        """取消订阅。"""
        ...

    def close(self) -> None:
        """释放资源。"""
        ...
```

> **注**：`subscribe`/`unsubscribe` 当前在 Bus 层以 in-process observer 模式实现，作为所有 backend 的通用基线。当 backend 具备原生推送能力时，Bus 层可将订阅委托给 backend，实现更高效的消息投递。

#### 可用 Backend 实现

| Backend | Connection String | 适用场景 |
|---------|------------------|----------|
| SQLiteBackend | `mansio.db` 或 `:memory:` | 开发、测试、单机部署、零外部依赖 |
| MemoryBackend | `:memory:`（通过 Bus 对象传入） | 单元测试、临时场景 |
| RedisBackend | `redis://host:port` | 多实例部署、需要原生 pub/sub |
| RabbitMQBackend | `amqp://host:port` | 企业级、复杂路由、持久队列 |
| *自定义* | *自定义 URL scheme* | 按需扩展 |

> **选择建议**：Backend 之间没有优先级顺序。根据部署场景选择：开发测试用 SQLite/Memory（零依赖），生产单机用 SQLite，需要跨实例通信用 Redis，需要企业级消息保证用 RabbitMQ。

#### 扩展新 Backend

实现 `Backend` protocol 即可接入：

```python
class MyBackend:
    def __init__(self, connection_url: str): ...
    def store(self, message: Message) -> None: ...
    def query(self, channel, after=None, limit=100) -> list[Message]: ...
    def channels(self) -> list[str]: ...
    def close(self) -> None: ...

# 使用
bus = Bus(backend=MyBackend("custom://..."))
```

### 3.3 Serializer

Serializer 负责 metadata 字典的编解码。

```python
class Serializer(Protocol):
    def encode(self, obj: dict) -> str: ...
    def decode(self, data: str) -> dict: ...
```

| Serializer | 特点 | 适用场景 |
|-----------|------|----------|
| JSONSerializer | 人类可读，调试友好 | 默认，开发与生产通用 |
| MessagePackSerializer | 紧凑高效 | 高吞吐场景 |
| *自定义* | 按需 | 特殊协议要求 |

### 3.4 Bus 层

Bus 是编排层，组合 Backend 和 Serializer，提供统一的消息发布/查询接口。

```python
class Bus:
    def __init__(
        self,
        backend: Backend | None = None,      # 默认 SQLiteBackend(:memory:)
        serializer: Serializer | None = None, # 默认 JSONSerializer
        require_auth: bool = False,           # 认证模式开关
    ): ...

    # 核心操作
    def publish(self, channel, sender, msg_type, payload, metadata=None) -> str
    def poll(self, channel, after=None, limit=100) -> list[Message]
    def subscribe(self, channel, callback) -> str
    def unsubscribe(self, subscription_id) -> None
    def channels(self) -> list[str]

    # 生命周期
    def close(self) -> None
    def __enter__ / __exit__  # context manager

    # 属性
    @property backend -> Backend
    @property serializer -> Serializer
```

**Bus 层的职责边界**：

- ✅ 消息 ID 生成（UUID v7）
- ✅ 时间戳生成
- ✅ 将消息路由到 Backend
- ✅ In-process pub/sub（作为通用基线）
- ✅ 认证模式控制
- ✅ Channel 命名验证（服务端强制执行，见 §3.5.3）
- ✅ 输入验证（空/非字符串 payload 拒绝，limit 参数校验）
- ✅ System channel 写入权限控制（见 §3.5.3）
- ✅ 私有频道所有权检查（notebook/memory，见 §3.5.3）
- ✅ Broadcast 频道写入限制（仅 supertoken，见 §3.5.3）
- ❌ 不管理 agent 身份（由 Client SDK 负责）
- ❌ 不追踪 cursor 状态（由 Client SDK 负责）

### 3.5 Client SDK 层（MansioClient）

MansioClient 是面向 agent/LLM 的核心接口，提供有状态的消息操作封装。

#### 3.5.1 连接模型

MansioClient 的构造函数接受 `Bus` 对象或 connection string，自动选择 Transport：

```python
# 方式 1：传入 Bus 对象（orchestrator 编排模式）
bus = Bus(backend=SQLiteBackend("data.db"))
client = MansioClient(bus, "coder-1")

# 方式 2：传入 connection string（自动创建 Bus）
client = MansioClient("mansio.db", "coder-1")
client = MansioClient(":memory:", "coder-1")
client = MansioClient("redis://localhost:6379", "coder-1")
client = MansioClient("amqp://localhost", "coder-1")

# 方式 3：连接远程 MansioServer
client = MansioClient("http://mansio:8741", "coder-1", secret="sk-xxx")
```

内部通过 Transport 抽象屏蔽差异：

```
target 类型                → Transport         → Bus 生命周期
───────────────────────────────────────────────────────────────
Bus 对象                   → Bus            → 调用方管理
文件路径 / :memory:        → Bus            → Client 自建自管
redis:// / amqp://         → Bus            → Client 自建自管
http:// / https://         → RemoteTransport   → 远程 Server 管理
```

Transport 是纯内部抽象，用户不直接接触。

#### 3.5.2 身份与认证

##### 身份模型

```
agent_id      唯一系统标识，用户自选，格式约束（小写字母+数字+连字符，3-64 字符）
secret        Mansio 生成的凭证，SHA256 哈希存储
display_name  可选显示名，可重复，默认等于 agent_id
```

类比微信：agent_id ≈ 微信号（唯一），display_name ≈ 昵称（可重复）。

##### 注册与连接

```python
# 首次注册
client, secret = MansioClient.register(target, "coder-1", display_name="Code Bot")
# → 生成 secret，写入 _system:registry channel
# → 调用方自行保存 secret（env var / config）

# 凭 secret 重连（cross-session 恢复）
client = MansioClient(target, "coder-1", secret="sk-xxx")
# → 验证 secret → 恢复 cursor → 继续工作

# 无认证模式（Bus require_auth=False 时）
client = MansioClient(target, "coder-1")
# → 跳过认证，直接使用
```

##### 认证模式

通过 Bus 启动配置控制：

```python
# 开发/测试：无认证（默认）
bus = Bus(require_auth=False)

# 生产/共享服务：强制认证
bus = Bus(require_auth=True)
```

##### 注册表存储

Agent 注册信息存储在 `_system:registry` channel 中，遵循"一切皆消息"原则：

```python
# 注册时写入的消息
channel = "_system:registry"
sender = agent_id
msg_type = "register"
metadata = {
    "display_name": "Code Bot",
    "secret_hash": "sha256:...",
    "action": "register",  # register | deregister | update
}
```

Client SDK 读取该 channel 构建当前 agent 状态映射。

##### Secret 管理

```python
# 预留接口（MVP 未实现）
client.rotate_secret() -> str      # raises NotImplementedError
client.revoke() -> None            # raises NotImplementedError
```

#### 3.5.3 Channel 类型与命名

Channel 命名规则在**服务端（Bus/Frontend 层）强制执行**，Client SDK 层同样做校验以提供更好的错误提示。

##### 命名规则

- 长度：3–64 字符
- 必须以字母开头
- 必须以字母或数字结尾（不能以下划线、连字符或点号结尾）
- 不允许大写字母
- 不允许冒号（保留给系统前缀如 `_system:`）
- 不允许连续特殊字符（如 `..`、`--`、`__`）
- 正则表达式：`^(?=[^\W\d_])[\w.-]{1,63}[^\W_]$`

> **注：** 带系统前缀的频道（如 `_system:registry`、`notebook:agent-1`）由 Client SDK 自动构造，不受上述通用规则约束。通用命名规则适用于用户自定义的频道名（如 group、broadcast 的 topic 部分）。

| Channel 类型 | 命名模式 | 用途 | 访问控制 |
|-------------|----------|------|----------|
| History | `history:{agent_id}` | 对话/工作记录 | 私有，SDK 自动写入 |
| Notebook | `notebook:{agent_id}` | 思考过程、临时笔记（含 Thought） | **私有 — 仅所属 agent 可写入**，跨 agent 写入返回 403 |
| Memory | `memory:{agent_id}` | 长期记忆（语义记忆） | **私有 — 仅所属 agent 可写入**，跨 agent 写入返回 403 |
| Broadcast | `broadcast:{topic}` | 公告、任务列表、成员列表 | **仅 supertoken 可写入**，普通 agent 只读 |
| Group | `group:{group_id}` | 群聊 | 成员可读写 |
| DM | `dm:{agent_a}:{agent_b}` | 私聊（双方 ID 按字典序排列） | 双方可读写 |
| System | `_system:{purpose}` | 内部管理（注册表、cursor 等） | **受限写入**（见下文） |

##### System Channel 鉴权

`_system:*` 频道对普通 agent 有写入限制，以防止滥用系统频道：

| 允许的 System Channel | 写入者 | 用途 |
|----------------------|--------|------|
| `_system:agents` | 任意 agent | Presence（在线状态） |
| `_system:cursors:{自己的 agent_id}` | 对应 agent | Cursor 快照持久化 |
| `_system:registry` | 任意 agent | 自注册 |

- 其他 `_system:*` 频道的写入请求返回 **403 Forbidden**
- Supertoken 不受此限制，可写入任意 `_system:*` 频道

##### 私有频道所有权

- `notebook:X` 和 `memory:X` 频道只能由 agent X 写入
- 其他 agent 尝试写入将返回 **403 Forbidden**

##### Broadcast 频道写入限制

- `broadcast:*` 频道只有持有 supertoken 的客户端可以写入
- 普通 agent 写入将返回 **403 Forbidden**

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
             metadata: dict | None = None) -> str

# 读取 channel 消息（随机访问，不推进 cursor）
channel_read(channel: str, limit: int = 10,
             after: str | None = None) -> list[Message]

# 增量轮询（cursor 自动推进，用于持续跟踪新消息）
channel_poll(channel: str) -> list[Message]

# 列出所有 channel
channel_list() -> list[str]
```

##### 语义 API：高层业务操作

以下方法是 channel 操作的语义封装（syntactic sugar），自动路由到对应 channel 并设置正确的 `msg_type`：

```python
# ── Notebook（写入 notebook:{agent_id}）──
note_write(content: str, tags: list[str] | None = None) -> str
note_read(tags: list[str] | None = None, limit: int = 10) -> list[Message]

# ── Thought（写入 notebook:{agent_id}，msg_type="thought"）──
thought_record(
    thinking_mode: str,    # reasoning | planning | reflection | recalling | brainstorming | exploring
    focus_area: str,
    thought_process: str,
) -> str
thought_read(limit: int = 10) -> list[Message]

# ── Memory（写入 memory:{agent_id}）──
memory_store(content: str, memory_type: str = "general") -> str
memory_recall(query: str, limit: int = 5) -> list[Message]
# memory_recall 的语义搜索能力由外部组件提供（如 mem0），
# Client SDK 层仅提供接口定义，默认实现为按时间倒序返回。

# ── DM（写入 dm:{sorted_pair}）──
dm_send(to_agent: str, content: str) -> str
dm_read(with_agent: str, limit: int = 10) -> list[Message]

# ── Group ──
group_create(name: str, members: list[str]) -> str
group_send(group_id: str, content: str) -> str
group_read(group_id: str, limit: int = 10) -> list[Message]

# ── Broadcast ──
broadcast_list() -> list[str]
broadcast_read(topic: str, limit: int = 10) -> list[Message]

# ── Notification ──
notification_check() -> list[Message]
```

##### 语义 API 与 Channel 操作的映射关系

```
note_write(content, tags)
  → channel_send(f"notebook:{self.agent_id}", content, msg_type="note",
                  metadata={"tags": tags})

thought_record(mode, focus, process)
  → channel_send(f"notebook:{self.agent_id}", process, msg_type="thought",
                  metadata={"thinking_mode": mode, "focus_area": focus})

memory_store(content, memory_type)
  → channel_send(f"memory:{self.agent_id}", content, msg_type="memory",
                  metadata={"memory_type": memory_type})

dm_send(to_agent, content)
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

Cursor 状态存储在 `_system:cursors:{agent_id}` channel 中，实现 cross-session 恢复：

```python
# Client SDK 定期或在关键节点持久化 cursor
channel_send(
    f"_system:cursors:{self.agent_id}",
    json.dumps(self._cursors),  # {"channel_a": "last_msg_id", ...}
    msg_type="cursor_snapshot",
)

# 重连时从 channel 读取最新快照恢复
```

**Cross-session 恢复流程**：

```
Agent 死亡
  → 重新 spawn
  → 用同一个 agent_id + secret 创建 MansioClient
  → _announce() 写入新的 register 消息
  → _restore_cursors() 从 _system:cursors:{agent_id} 读取最新快照
  → channel_poll() 从断点继续
```

### 3.6 Frontend 层与 MansioServer

> 🔄 **开发中** — 已在 `dev/agent-bus` 分支实现，待合入 master。

Frontend 层通过提供网络服务挂载到 Bus，实现 Hub-Server 部署模式。

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
        """优雅关闭。"""
        ...
```

#### HttpFrontend

首个 Frontend 实现，提供：
- **REST API** — 发布、轮询、列出通道
- **SSE（Server-Sent Events）** — 通过 `subscribe` 实现实时消息流

#### MansioServer

将 Bus 绑定到一个或多个 Frontend 的编排器：

```python
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="0.0.0.0", port=8741))
server.serve_forever()
```

#### HttpTransport

HttpFrontend 的客户端对应物，通过 HTTP 实现 Transport 协议：

```python
# 客户端连接远程 MansioServer
client = MansioClient("http://mansio:8741", "agent-1", secret="sk-xxx")
# → 内部使用 HttpTransport
```

### 3.7 管理面板

管理面板提供用于 Bus 检查和监控的 HTTP 仪表板。管理处理程序采用模块化的 `admin/routes/` 子包结构，使用字典分发：

```
admin/
├── server.py          # AdminServer（HTTP 服务器生命周期）
├── auth.py            # 认证工具
├── handlers.py        # 顶层处理器 + 字典分发
├── static.py          # 静态资源服务
└── routes/
    ├── __init__.py    # ROUTE_TABLE（路径 → 处理器映射）
    ├── _shared.py     # 共享工具（JSON 响应、错误处理）
    ├── channels.py    # /api/channels、/api/channels/{name}
    ├── dashboard.py   # /api/dashboard（统计 + 吞吐量）
    ├── messages.py    # /api/messages/{channel}
    ├── subscriptions.py  # /api/subscriptions
    └── ui.py          # /（HTML 仪表板）
```

### 3.8 Delivery 层

Delivery 层将 Client SDK 的能力暴露给外部消费者。

```
┌────────────────────────────────────────────────────┐
│                 MansioClient SDK                   │
├───────────┬───────────┬───────────┬────────────────┤
│    MCP    │ REST API  │   CLI     │   OpenAPI      │
│  Server   │  Server   │ (Tier 2)  │   Schema       │
│           │           │           │                │
│  LLM via  │  HTTP     │  LLM via  │  Swagger /     │
│  MCP tool │  clients  │  bash tool│  code-gen      │
└───────────┴───────────┴───────────┴────────────────┘
```

#### CLI 两层设计

| 层级 | 目标用户 | 功能 |
|------|---------|------|
| **Tier 1：运维管理** | 运维人员 | `mansio serve`, `mansio status`, `mansio admin` |
| **Tier 2：SDK-over-CLI** | LLM（通过 bash tool） | 将 SDK 方法映射为 CLI 命令，如 `mansio channel send ...` |

#### Delivery 通道

MansioClient 的方法可以通过 toolregistry-server 统一暴露为 MCP tool、REST API 和 CLI 命令，无需为每种协议单独编写适配代码。

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

- **MVP**：`notification_check()` 主动轮询
- **未来**：主动轮询 + 返回时附带通知（需要 Agent SDK 层支持）

### Broadcast Channel 管理

**MVP**：Broadcast 由管理员 / API 直接发布。

**未来**：引入 Moderator Agent 机制 — Agent 提交到 `broadcast:submissions`，Moderator 审核后发布到对应 broadcast channel。

---

## 5. 消息类型

`msg_type` 是自由字符串，以下为约定的标准类型：

| 类型 | 说明 | 典型 channel |
|------|------|-------------|
| `chat` | 聊天消息 | group:\*, dm:\* |
| `note` | 笔记/备忘 | notebook:\* |
| `thought` | 认知过程记录 | notebook:\* |
| `memory` | 记忆条目 | memory:\* |
| `broadcast` | 广播消息 | broadcast:\* |
| `task_request` | 任务请求 | group:\*, dm:\* |
| `task_result` | 任务结果 | group:\*, dm:\* |
| `notification` | 通知 | _system:\* |
| `heartbeat` | 心跳 | _system:\* |
| `register` | Agent 注册 | _system:registry |
| `cursor_snapshot` | Cursor 快照 | _system:cursors:\* |

### Thought 类型设计（借鉴 ThinkTool）

**设计理念**：让 agent 的思考过程从"黑盒"变为"白盒"。

```python
# 通过 thought_record() 写入
thought_record(
    thinking_mode="reasoning",    # reasoning | planning | reflection | ...
    focus_area="API 设计选型",
    thought_process="考虑了三种方案...",
)

# 底层存储为 Message:
# channel = "notebook:{agent_id}"
# msg_type = "thought"
# payload = thought_process
# metadata = {"thinking_mode": "reasoning", "focus_area": "API 设计选型"}
```

---

## 6. 部署模式

### 6.1 嵌入式（In-process）

所有 agent 在同一进程内，共享 Bus 对象。

```python
bus = Bus(backend=SQLiteBackend("data.db"))
client_a = MansioClient(bus, "coder-1")
client_b = MansioClient(bus, "reviewer-1")
```

- 最简单，零网络开销
- subscribe 回调在进程内同步触发
- 适合单机多 agent 编排

### 6.2 多进程共享存储

各进程独立创建 Client，指向同一存储。

```python
# 进程 A
client_a = MansioClient("shared/mansio.db", "coder-1")

# 进程 B
client_b = MansioClient("shared/mansio.db", "reviewer-1")
```

- 通过 SQLite WAL 模式支持并发读写
- subscribe 只在进程内生效，跨进程用 `channel_poll()`
- 适合单机多进程场景

### 6.3 持久服务（MansioServer）

中心化服务，Client 通过网络 API 接入。

```python
# Server 端
bus = Bus(backend=SQLiteBackend("data.db"), require_auth=True)
server = MansioServer(bus, host="0.0.0.0", port=8741)

# Client 端（任意机器）
client = MansioClient("http://mansio:8741", "coder-1", secret="sk-xxx")
```

- 适合多机部署、云环境
- 身份认证强制启用
- 可配合 Redis/RabbitMQ backend 实现高可用

---

## 7. 配置

### 7.1 Connection String（当前）

Client SDK 通过 connection string 选择 backend，配置与代码融为一体：

```python
MansioClient("mansio.db", agent_id)           # SQLite
MansioClient(":memory:", agent_id)             # 内存
MansioClient("redis://host:6379", agent_id)    # Redis
MansioClient("amqp://host:5672", agent_id)     # RabbitMQ
MansioClient("http://host:8741", agent_id)     # 远程服务
```

### 7.2 配置文件（未来，MansioServer 部署时）

服务端部署时将支持 YAML/TOML 配置文件：

```yaml
# mansio.yaml（预留设计，尚未实现）
server:
  host: 0.0.0.0
  port: 8741
  require_auth: true

backend:
  url: redis://localhost:6379
  # url 格式遵循 connection string 约定

serializer:
  type: json  # json | msgpack

logging:
  level: info
```

配置文件最终也解析为 connection string + 构造函数参数，两种配置方式等价。

---

## 8. 错误处理

### 8.1 输入验证

Bus 层对所有写入操作执行以下验证，不合规的请求返回 HTTP 400：

| 验证规则 | 拒绝条件 |
|----------|----------|
| Payload 不得为空 | 空字符串或纯空白 payload 被拒绝 |
| Payload 必须是字符串 | 非字符串类型的 payload 被拒绝 |
| Limit 参数校验 | 查询时 `limit` 必须 >= 1 |
| Channel 名称校验 | 不符合命名规则的 channel 名被拒绝（见 §3.5.3） |

### 8.2 错误恢复策略

**MVP 策略**：简单重试 + 返回错误给 agent，由 agent 自行决定处理方式。

**未来扩展**：
- 死信队列（DLQ）
- 可配置重试策略
- 消息投递确认

---

## 9. 扩展路线

| 功能 | 说明 | 依赖 |
|------|------|------|
| Secret 轮换 | rotate_secret() / revoke() | Client SDK |
| 消息 TTL | 按 channel 类型设置过期策略 | Backend |
| 消息追踪 | 分布式追踪 ID | Message metadata |
| 优先级队列 | 紧急消息插队 | Backend |
| 延迟消息 | 定时投递 | Backend |
| Moderator Agent | Broadcast 审核机制 | Client SDK |
| Async API | asyncio 支持 | 全栈 |
| 消息打断 | interrupt:{agent_id} channel + 优先级 | Agent SDK 层 |
| Per-channel 别名 | 类似微信群名片 | Client SDK |
| Agent 心跳 | 活跃度检测与过期回收 | Client SDK |

---

## 10. 决策记录

### D1: Backend 与 Storage 合并

**决策**：Backend = transport + persistence 一体，不单独抽象 Storage 层。

**理由**：当前所有 backend（SQLite、Redis Streams、RabbitMQ）都天然包含持久化。若未来出现纯传输型 backend（如 MQTT）需要独立存储，可在该 backend 内部组合，不影响 Protocol 接口。

**演进路径**：当确实需要 transport 与 persistence 分离时（如 MQTT + PostgreSQL），可引入独立的 Storage Protocol，Backend 内部组合使用。当前 Protocol 接口无需变更。

### D2: Channel 命名在服务端强制执行

**决策**：Channel 命名规则在服务端（Bus/Frontend 层）强制执行，Client SDK 层同样做前置校验。

**理由**：仅在 Client SDK 层校验无法防止恶意或绕过 SDK 的客户端提交非法频道名。服务端验证确保数据完整性，同时 Client SDK 的前置校验提供更好的开发体验和错误提示。

**变更历史**：早期版本中，Bus 层接受任意 channel 名，命名规则仅由 Client SDK 层校验。随着安全需求和输入验证的加强，验证逻辑已提升到服务端执行。

### D3: 身份认证采用 agent_id + secret

**决策**：agent_id 用户自选（格式约束），secret 由 Mansio 生成，通过 Bus 配置控制是否强制认证。

**理由**：简单成熟的凭证模式，支持 cross-session 恢复（同 agent_id + secret 重连）。无认证模式降低开发/测试门槛。

### D4: 注册表存储在 _system:registry channel

**决策**：Agent 注册信息作为消息存储在系统 channel 中，而非 Backend 层新增表。

**理由**：遵循"一切皆消息"原则，不修改 Backend Protocol 接口。Client SDK 读取 channel 重建 agent 状态。

### D5: Cursor 持久化在 _system channel

**决策**：Cursor 快照存储在 `_system:cursors:{agent_id}` channel 中。

**理由**：复用消息存储机制，cross-session 恢复时从 channel 读取最新快照。无需额外的状态存储基础设施。

### D6: Connection String 驱动部署

**决策**：MansioClient 构造函数接受 `Bus | str`，通过 URL scheme 自动选择 backend 和 transport。

**理由**：将部署决策（用什么 backend、本地还是远程）与架构设计解耦。同一份 Client 代码无需修改即可适配不同部署环境。

### D7: API 采用 resource_action 命名

**决策**：SDK 方法名采用 `resource_action` 风格（如 `channel_send`、`note_write`），同时作为 MCP/CLI tool 名暴露。

**理由**：资源+动作的命名在 LLM tool calling 中语义最清晰，且扁平命名适合作为 CLI 子命令和 MCP tool name。

### D8: 语义 API 是 Channel 操作的 Sugar

**决策**：`note_write`、`thought_record`、`memory_store` 等语义方法底层映射为 `channel_send` + 特定 channel + msg_type。

**理由**：保持系统核心极简（一切皆消息），高层语义由 Client SDK 提供便利封装。用户也可直接使用 channel 操作实现自定义逻辑。
