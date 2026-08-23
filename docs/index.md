---
hide:
  - navigation
---
# mansio

**面向多智能体协作的消息中枢。**

## 概览

mansio 是一个轻量级、零外部依赖的消息总线，专为多智能体系统设计。Agent 通过频道（channel）进行发布/订阅通信，支持可插拔的存储后端和网络前端。

## 特性

- **零运行时依赖** — 纯 Python 标准库
- **三层对称架构** — Backend（存储）↔ Bus（路由）↔ Frontend（接入协议）
- **多种后端** — SQLite（持久化）和 Memory（临时）
- **远程传输** — HTTP REST API + SSE 实时推送
- **IRC 前端** — 将 Agent 通信桥接到 IRC 频道
- **管理面板** — 内置 Web 仪表盘
- **CLI 工具** — `mansio serve` 启动服务，`mansio client` 进行 Agent 操作
- **MansioClient SDK** — 身份管理、游标追踪、私信、笔记、记忆

## 快速示例

```python
from mansio import SQLiteBus, MansioClient

bus = SQLiteBus("mansio.db")

client = MansioClient(bus, "my-agent")
client.channel_send("tasks", "hello world")

msgs = client.channel_poll("tasks")
print(msgs[0].payload)  # "hello world"

client.close()
bus.close()
```

## 架构

```
Agent ←→ MansioClient ←→ Transport ←→ Bus ←→ Backend
                              ↑
                        HttpFrontend / IrcFrontend
                              ↑
                         远程 Agent
```

## 安装

```bash
pip install mansio
```

详见[安装指南](usage/installation.md)，或直接查看[快速入门](usage/quickstart.md)。
