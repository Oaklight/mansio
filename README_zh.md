# Mansio

[![CI](https://github.com/Oaklight/mansio/actions/workflows/ci.yml/badge.svg)](https://github.com/Oaklight/mansio/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mansio?color=%23800020&label=PyPI)](https://pypi.org/project/mansio/)
[![Release](https://img.shields.io/github/v/release/Oaklight/mansio?color=%23800020&label=Release)](https://github.com/Oaklight/mansio/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

[English Version](README_en.md) | 中文版

一个轻量级的多智能体 AI 协作消息总线 —— 智能体们的驿站。

## 概览

Mansio 为 AI 智能体提供结构化、可持久化的通信通道。智能体通过命名通道（channel）进行交互，支持发布/订阅语义、基于游标的轮询和内置身份管理，而非点对点 RPC 或共享内存。

```
Backend（存储）  →  Bus（路由）  →  Client SDK（智能体 API）
   SQLite / Memory      发布/订阅         身份、游标、
                         通道              私信、笔记、记忆
```

## 特性

- **基于通道的消息传递** — 命名通道，支持发布/订阅、游标跟踪轮询，通过单调 UUID 保证消息有序
- **可插拔存储** — `SQLiteBackend`（持久化，WAL 模式）和 `MemoryBackend`（临时，测试用）；基于 Protocol，易于扩展
- **Client SDK** — `MansioClient` 提供智能体身份标识、跨会话游标持久化、认证机制（注册/重连/密钥哈希）
- **语义化 API** — 私信、广播通道、笔记（支持标签）、思维记录（思维链日志）、记忆（存储/检索）、通知
- **服务端验证** — 频道名称、payload 和查询参数均在服务端校验；空/非字符串 payload 拒绝（400）；limit 参数必须 >= 1
- **访问控制** — `_system:*` 频道受限写入；`notebook:`/`memory:` 频道仅所属 agent 可写；`broadcast:*` 仅 supertoken 可写
- **管理面板** — 内置 HTTP 仪表板，REST API 可查看统计数据、浏览通道、检查消息、监控吞吐量；模块化 `admin/routes/` 子包，字典分发架构
- **灵活连接** — 通过 Bus 对象、文件路径（SQLite）或 `:memory:` 字符串连接；URL 协议（`http://`、`redis://`）预留给未来的传输层
- **MCP 服务器** — `mansio mcp-serve` 将所有客户端操作暴露为 MCP 工具，通过 JSON-RPC stdio 通信，支持任何 MCP 兼容的智能体
- **实时订阅** — 基于 SSE 的 `subscribe(channel, callback)`，支持推送式消息投递
- **消息线程** — `parent_id` 和 `thread_id` 字段支持回复链和对话上下文
- **工作队列** — `queue_publish`、`queue_claim`、`queue_ack` 模式，支持基于租约的任务分发
- **智能体在线状态** — `heartbeat()`、`agents()`、`user_status()` 用于上下线检测
- **推送集成** — 三层方案（MCP 工具 → 框架适配器 → 提示指令），`examples/` 中提供各框架示例
- **NATS 后端** — 通过 NATS JetStream 实现分布式消息传递（可选 `nats` 依赖）
- **零运行时依赖** — 纯 Python，仅使用标准库

## 快速开始

```python
from mansio import MansioClient

# 内存总线（测试用）
with MansioClient(":memory:", "agent-alpha") as alice:
    alice.channel_send("general", "大家好！")
    alice.note_write("记得检查日志", tags=["运维"])
    alice.thought_record("planning", "下一步", "需要和 bob 协调")

# SQLite 持久化
with MansioClient("/tmp/mansio.db", "agent-alpha") as alice:
    alice.dm_send("agent-beta", "准备好同步了吗？")

# 多智能体协作
from mansio import Bus, MemoryBackend

bus = Bus(backend=MemoryBackend())

alice = MansioClient(bus, "agent-alice")
bob = MansioClient(bus, "agent-bob")

alice.dm_send("agent-bob", "PR 已经准备好 review 了")
messages = bob.dm_read("agent-alice")  # ["PR 已经准备好 review 了"]

alice.close()
bob.close()
bus.close()
```

## 架构

Mansio 采用分层架构，灵感来自消息中间件，针对 AI 智能体工作流进行了适配：

| 层级 | 组件 | 职责 |
|------|------|------|
| **存储层** | `Backend` 协议 | 持久化或临时消息存储（`SQLiteBackend`、`MemoryBackend`） |
| **路由层** | `Bus` | 通道管理、发布/订阅分发、UUID 生成、输入验证、访问控制 |
| **传输层** | `Transport` 协议 | 本地 vs 远程总线访问的抽象（Bus 直接满足 Transport 协议） |
| **智能体 API** | `MansioClient` | 身份、游标、认证、语义化消息 API |
| **前端层** | `Frontend` 协议 | 网络服务层（REST + SSE），挂载到 Bus（`HttpFrontend`、`MansioServer`） |
| **管理层** | `AdminServer` | HTTP 仪表板 + REST API 监控 |

详细设计理念请参阅 [DESIGN.md](docs/DESIGN.md)。

## 安装

需要 **Python >= 3.10**。

```bash
pip install mansio
```

或从源码安装：

```bash
git clone https://github.com/Oaklight/mansio.git
cd mansio
pip install -e ".[dev]"
```

## Client SDK API

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
| `dm_send(target, content)` | 发送私信 |
| `dm_read(peer)` | 读取私信对话 |
| `note_write(content, tags=)` | 写笔记，可选标签 |
| `note_read(tags=)` | 读取笔记，可按标签过滤 |
| `thought_record(mode, focus, content)` | 记录思维链 |
| `thought_read()` | 读取思维历史 |
| `memory_store(content)` | 存储记忆 |
| `memory_recall(query)` | 按关键词检索记忆 |
| `broadcast_list()` / `broadcast_read(topic)` | 浏览广播通道 |
| `notification_check()` | 轮询通知 |

### 实时订阅

| 方法 | 描述 |
|------|------|
| `subscribe(channel, callback)` | 通过 SSE 订阅实时消息 |
| `unsubscribe(subscription_id)` | 取消订阅 |

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

### 认证

```python
# 注册新智能体（返回 client + 密钥）
client, secret = MansioClient.register(bus, "agent-id")

# 使用密钥重连
client = MansioClient(bus, "agent-id", secret=saved_secret)
```

### 管理面板

```python
from mansio import SQLiteBus

bus = SQLiteBus("mansio.db")
info = bus.start_admin(port=8741)
print(f"仪表板: {info.url}")
# 访问 http://localhost:8741 查看 Web UI
```

### MCP 服务器

```bash
mansio mcp-serve --url http://localhost:8742 --agent-id my-agent --token mst-xxx
```

将所有 `MansioClient` 操作暴露为 MCP 工具，通过 JSON-RPC stdio 通信。兼容 Claude Code、Codex 及任何 MCP 兼容的智能体框架。详见 `examples/adapters/` 中的各框架配置指南。

## 路线图

### 已发布

- [x] **RemoteTransport** — `HttpFrontend`、`MansioServer`、`HttpTransport`
- [x] **IRC Frontend** — 可选依赖 `irc`
- [x] **通道访问控制** — System/Notebook/Memory/Broadcast 频道写入权限强制执行
- [x] **消息线程** — `parent_id` / `thread_id` 嵌套回复支持
- [x] **消息删除** — 单条和按频道删除，管理员批量清理
- [x] **分页** — 基于 offset 的分页，返回 `total`、`has_more`、`offset` 元数据
- [x] **MCP 服务器** — Model Context Protocol 集成（`mansio[mcp]`）
- [x] **在线状态与心跳** — Agent 上下线状态及实时订阅
- [x] **NATS 后端** — 基于 JetStream 的持久化存储
- [x] **Maildir 后端** — 基于文件系统的存储
- [x] **压缩** — 注册表和游标压缩，适用于长期运行实例
- [x] **远程传输可靠性** — SSE 重连（Last-Event-ID）、WAL 重试日志、慢消费者丢弃通知
- [x] **工作队列** — publish/claim/ack，基于租约的任务分发
- [x] **推送集成** — MCP 工具 + 框架适配器 + 轮询模板

### 计划中

- [ ] **消息 TTL** — 自动过期与清理
- [ ] **异步 API** — 原生 async/await 支持
- [ ] **语义化记忆检索** — 向量嵌入搜索
- [ ] **Redis/AMQP 后端** — 分布式存储
- [ ] **联邦** — 跨实例通信（[#4](https://github.com/Oaklight/mansio/issues/4)）

## 学术背景

Mansio 是一篇博士论文第九章的参考实现，该章节探讨通过解耦抽象实现大规模智能体 AI。设计强调基于协议的接口、可插拔组件，以及传输、存储和智能体级语义之间的清晰分离。

## 许可证

MIT — 详情请参阅 [LICENSE](LICENSE)。
