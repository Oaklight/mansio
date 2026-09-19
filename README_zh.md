# Mansio

[![CI](https://github.com/Oaklight/mansio/actions/workflows/ci.yml/badge.svg)](https://github.com/Oaklight/mansio/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mansio?color=%23800020&label=PyPI)](https://pypi.org/project/mansio/)
[![Release](https://img.shields.io/github/v/release/Oaklight/mansio?color=%23800020&label=Release)](https://github.com/Oaklight/mansio/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

[English Version](README_en.md) | 中文版

一个轻量级的多智能体 AI 协作消息总线 —— 智能体们的驿站。

## 概览

Mansio 为 AI 智能体提供结构化、可持久化的通信通道。智能体通过命名通道（channel）进行交互，支持发布/订阅语义、基于游标的轮询和内置身份管理，而非点对点 RPC 或共享内存。

Mansio 由两个包组成：

| 包名 | 导入名 | 职责 |
|------|--------|------|
| `mansio` | `mansio` | 服务端：存储后端、Bus、HTTP/IRC 前端、管理面板、`mansio` 命令行 |
| `mansio-client` | `mansio_client` | 智能体 SDK：`MansioClient`、`mansio-client` 命令行、`mansio-mcp` MCP 服务器 |

智能体始终通过 HTTP 连接到运行中的服务端：

```
Backend（存储）   →   Bus（路由）    →   Frontend（网络）   →   mansio-client
 SQLite / Memory /     发布/订阅、         HTTP（REST+SSE）、     身份、游标、
 NATS / Maildir        通道                IRC                    私信、笔记、记忆
```

## 特性

- **基于通道的消息传递** — 命名通道，支持发布/订阅、游标跟踪轮询，通过时间有序 UUID（UUID v7）保证消息有序
- **服务端验证** — 通道名称、消息 payload 和查询参数均在 Frontend 层校验，非法输入在到达 Bus 之前被拒绝
- **访问控制** — 系统通道（`_system:*`）、私有通道（`notebook:X`、`memory:X`）和广播通道按智能体强制写入限制；supertoken 拥有更高权限
- **可插拔存储** — `SQLiteBackend`（持久化，WAL 模式）、`MemoryBackend`（临时，测试用）、`NATSBackend`（JetStream）、`MaildirBackend`（文件系统）；基于协议，易于扩展
- **Client SDK** — `mansio_client.MansioClient` 提供智能体身份标识、跨会话游标持久化和 Bearer Token 认证
- **语义化 API** — 私信、广播通道、笔记（支持标签）、思维记录（思维链日志）、记忆（存储/检索）、通知
- **管理面板** — 内置 HTTP 仪表板与 REST API，可查看统计数据、浏览通道、检查消息、监控吞吐量、管理智能体与令牌
- **MCP 服务器** — `mansio-mcp` 将客户端操作暴露为 MCP 工具，通过 JSON-RPC stdio 通信，支持任何 MCP 兼容的智能体
- **实时订阅** — 基于 SSE 的 `subscribe(channel, callback)`，支持推送式消息投递
- **消息线程** — `parent_id` 和 `thread_id` 字段支持回复链和对话上下文
- **工作队列** — `queue_publish`、`queue_claim`、`queue_ack` 模式，支持基于租约的任务分发
- **智能体在线状态** — `heartbeat()`、`users()`、`user_status()` 用于上下线检测
- **推送集成** — 三层方案（MCP 工具 → 框架适配器 → 提示指令），`examples/` 中提供各框架示例
- **零运行时依赖** — 纯 Python，仅使用标准库（可选依赖会引入 `irc` 和 `nats-py`）

## 快速开始

启动服务端（开发模式，关闭认证）：

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741 --no-auth
```

然后让智能体连接：

```python
from mansio_client import MansioClient

URL = "http://localhost:8742"

with MansioClient(URL, "agent-alice") as alice:
    alice.channel_send("general", "hello everyone!")
    alice.note_write("remember to check logs", tags=["ops"])
    alice.thought_record(
        "need to coordinate with bob",
        thinking_mode="planning",
        focus_area="next steps",
    )
    alice.dm_send("agent-bob", "PR is ready for review")

with MansioClient(URL, "agent-bob") as bob:
    for msg in bob.channel_poll("general"):
        print(f"{msg.sender}: {msg.payload}")
    for msg in bob.dm_read("agent-alice"):
        print(f"DM from {msg.sender}: {msg.payload}")
```

```
agent-alice: hello everyone!
DM from agent-alice: PR is ready for review
```

同样的操作也可以在命令行完成：

```bash
export MANSIO_URL=http://localhost:8742
export MANSIO_USER_ID=agent-alice

mansio-client send -c general "hello from the CLI"
mansio-client read -c general -n 5
mansio-client dm --to agent-bob "hey!"
```

## 架构

Mansio 采用分层架构，灵感来自消息中间件，针对 AI 智能体工作流进行了适配：

| 层级 | 组件 | 职责 |
|------|------|------|
| **存储层** | `Backend` 抽象基类 | 持久化或临时消息存储（`SQLiteBackend`、`MemoryBackend`、`NATSBackend`、`MaildirBackend`） |
| **路由层** | `Bus` | 通道管理、发布/订阅分发、UUID 生成、压缩策略 |
| **前端层** | `Frontend` 协议 | 挂载到 Bus 的网络服务（`HttpFrontend` 提供 REST + SSE，`IrcFrontend`） |
| **编排层** | `MansioServer` | 将一个 Bus 绑定到一个或多个 Frontend |
| **管理层** | `AdminServer` | HTTP 仪表板 + REST API，用于监控与令牌管理 |
| **智能体 API** | `mansio_client.MansioClient` | 身份、游标、认证、基于 HTTP 的语义化消息 API |

详细设计理念请参阅 [DESIGN.md](docs/DESIGN.md)。

## 安装

需要 **Python >= 3.10**。

```bash
pip install mansio         # 服务端
pip install mansio-client  # 智能体 SDK、命令行与 MCP 服务器
```

或从源码安装 —— 两个包分别位于子目录中，需要同时安装：

```bash
git clone https://github.com/Oaklight/mansio.git
cd mansio
pip install -e "mansio[dev,test]" -e mansio-client/
```

`mansio` 包提供以下可选依赖：`dev`（代码检查、类型检查、构建）、`test`（pytest）、`irc`（IRC 前端）、`nats`（NATS JetStream 后端）。

## 运行服务端

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741
```

| 参数 | 含义 |
|------|------|
| `--db PATH` | SQLite 数据库路径（默认：`mansio.db`） |
| `--maildir PATH` / `--nats URL` | 改用 Maildir 或 NATS 后端 |
| `--http [HOST:]PORT` | 启用 HTTP 前端（不加此参数智能体无法连接） |
| `--admin-port PORT` | 管理面板端口（默认：8741） |
| `--host ADDR` | 管理面板监听地址（默认：127.0.0.1） |
| `--remote` | 将管理面板绑定到 0.0.0.0 并自动生成管理密码 |
| `--no-auth` | 关闭 API 令牌认证与管理面板认证 —— 仅限开发环境 |
| `--irc HOST:PORT` | 将通道桥接到 IRC 服务器（需要 `irc` 可选依赖） |

不使用 `--no-auth` 时，HTTP API 要求 Bearer Token，未认证请求会返回 `401`。

## 认证

智能体使用服务端签发的 Bearer Token（`mst-...`）认证。令牌可在管理面板 UI 中管理，也可通过其 REST API 签发：

```bash
curl -X POST http://localhost:8741/api/users \
     -H 'Content-Type: application/json' \
     -d '{"user_id": "agent-alice", "label": "quickstart"}'
```

```json
{"ok": true, "user_id": "agent-alice", "token": {"id": "584484b2", "token": "mst-7c3a...", "user_id": "agent-alice", "label": "quickstart"}}
```

把令牌传给客户端：

```python
from mansio_client import MansioClient

with MansioClient("http://localhost:8742", "agent-alice", token="mst-...") as alice:
    alice.channel_send("general", "authenticated hello")
```

未绑定 `user_id` 的令牌称为 **supertoken**：它可以以任意智能体身份操作，并可写入 `broadcast:*` 与 `_system:*` 通道。管理 API 要求必须提供 `user_id`，因此 supertoken 需要通过代码创建（[#299](https://github.com/Oaklight/mansio/issues/299)）：

```python
from mansio.token_store import TokenStore

print(TokenStore("mansio.db").create_token(user_id=None, label="admin")["token"])
```

写入 `broadcast:*` 无条件只允许 supertoken —— 单通道的 ACL 条目无法授予该权限（[#298](https://github.com/Oaklight/mansio/issues/298)）。

## 管理面板

`mansio serve` 会在启动总线的同时启动管理面板。访问 `http://localhost:8741` 即可打开 Web UI，其中包含总线统计、通道与消息浏览、实时吞吐量以及智能体/令牌管理。相同的数据也以 JSON 形式提供在 `/api/` 下（`/api/stats`、`/api/stats/throughput`、`/api/channels`、`/api/messages`、`/api/subscriptions`、`/api/users`、`/api/tokens` 等）。

管理面板默认绑定 `127.0.0.1` 且不做认证；若要对外暴露，请配合 `--host`/`--remote` 与 `--admin-password` 使用。

## Client SDK API

以下方法均属于 `mansio_client.MansioClient(url, user_id, *, token=None, display_name=None)`。

### 核心操作

| 方法 | 描述 |
|------|------|
| `channel_send(channel, content)` | 向通道发送消息 |
| `channel_read(channel)` | 读取消息（不推进游标）；支持 `order`（"oldest"/"newest"）和 `thread_id` 参数 |
| `channel_poll(channel)` | 轮询新消息（推进游标） |
| `channel_list()` | 列出所有通道；支持 `detail=True` 获取元数据 |

### 语义化 API

| 方法 | 描述 |
|------|------|
| `dm_send(to_user, content)` | 发送私信 |
| `dm_read(with_user)` | 读取私信对话 |
| `note_write(content, tags=)` | 写笔记，可选标签 |
| `note_read(tags=)` | 读取笔记，可按标签过滤 |
| `thought_record(thought_process, thinking_mode=, focus_area=)` | 记录思维链 |
| `thought_read()` | 读取思维历史 |
| `memory_store(content, memory_type=)` | 存储记忆 |
| `memory_recall(query)` | 按关键词检索记忆 |
| `broadcast_list()` / `broadcast_read(topic)` | 浏览广播通道 |
| `notification_check()` | 轮询通知 |

### 实时订阅

| 方法 | 描述 |
|------|------|
| `subscribe(channel, callback)` | 通过 SSE 订阅实时消息 |
| `unsubscribe(subscription_id)` | 取消订阅 |
| `listen(channels, injector)` | 订阅多个通道，并将消息交给[注入器](docs/injectors.md)处理 |

### 工作队列

| 方法 | 描述 |
|------|------|
| `queue_publish(channel, content)` | 向工作队列发布任务 |
| `queue_claim(channel)` | 认领下一个可用任务（带租约） |
| `queue_ack(message_id)` | 确认任务完成 |
| `queue_status(message_id)` | 查询任务认领状态 |

### 在线状态

| 方法 | 描述 |
|------|------|
| `heartbeat()` | 发送在线心跳 |
| `users()` | 列出智能体及其在线状态 |
| `user_status(user_id)` | 查询指定智能体的在线状态 |

### 通道管理

| 方法 | 描述 |
|------|------|
| `channel_create(name, visibility=)` | 创建带元数据的通道 |
| `channel_delete(name)` | 删除通道及其消息 |
| `message_delete(message_id)` | 删除单条消息 |
| `acl_get` / `acl_set` / `acl_add` / `acl_remove` | 管理通道的访问控制条目（这些条目只对 ACL 端点本身生效；消息访问走结构性通道规则，详见 [DESIGN.md](docs/DESIGN.md) §3.5.3） |
| `registry_lookup(user_id)` | 检查某个智能体是否已注册 |

## MCP 服务器

```bash
mansio-mcp --url http://localhost:8742 --user-id my-agent --token mst-xxx
```

随 `mansio-client` 一起发布。每个参数都会回退到对应的环境变量（`MANSIO_URL`、`MANSIO_USER_ID`、`MANSIO_TOKEN`、`MANSIO_DISPLAY_NAME`），因此已设置好环境变量的框架可以不带任何参数直接启动它。它将客户端操作暴露为 MCP 工具，通过 JSON-RPC stdio 通信。兼容 Claude Code、Codex 及任何 MCP 兼容的智能体框架。详见 `examples/adapters/` 中的各框架配置指南。

## 路线图

### 已发布

- [x] **HTTP 前端** — `HttpFrontend`、`MansioServer`、`HttpTransport`
- [x] **IRC 前端** — 可选依赖 `irc`
- [x] **通道访问控制** — 系统、私有与广播通道的写入限制在服务端强制执行
- [x] **消息线程** — `parent_id` / `thread_id` 嵌套回复支持
- [x] **消息删除** — 单条和按通道删除，管理员批量清理
- [x] **分页** — 基于 offset 的分页，返回 `total`、`has_more`、`offset` 元数据
- [x] **MCP 服务器** — Model Context Protocol 集成（`mansio-client` 中的 `mansio-mcp`）
- [x] **在线状态与心跳** — 智能体上下线状态及实时订阅
- [x] **NATS 后端** — 基于 JetStream 的持久化存储
- [x] **Maildir 后端** — 基于文件系统的存储
- [x] **压缩** — 注册表和游标压缩，适用于长期运行实例
- [x] **远程传输可靠性** — SSE 重连（Last-Event-ID）、WAL 重试日志、慢消费者丢弃通知
- [x] **工作队列** — publish/claim/ack，基于租约的任务分发
- [x] **推送集成** — MCP 工具 + 框架适配器 + 轮询模板
- [x] **客户端注入** — 各框架的消息注入适配器
- [x] **联邦** — 通过 `FederationLink` 实现显式双实例复制与路由；支持双向/拉取/推送通道同步及按需远程读写（[#4](https://github.com/Oaklight/mansio/issues/4)）

### 计划中

- [ ] **消息 TTL** — 自动过期与清理
- [ ] **异步 API** — 原生 async/await 支持
- [ ] **语义化记忆检索** — 向量嵌入搜索
- [ ] **Redis/AMQP 后端** — 分布式存储
- [ ] **联邦 v2** — `@instance` 寻址、多跳网格路由、实例发现

## 学术背景

Mansio 是一篇博士论文第九章的参考实现，该章节探讨通过解耦抽象实现大规模智能体 AI。设计强调基于协议的接口、可插拔组件，以及传输、存储和智能体级语义之间的清晰分离。

## 许可证

MIT — 详情请参阅 [LICENSE](LICENSE)。
