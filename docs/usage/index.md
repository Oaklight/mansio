# 使用指南

了解如何使用 mansio 进行多智能体消息通信。

## 架构

mansio 采用可插拔的后端架构，通过 `mansio.protocols` 中的协议定义：

- **`Backend`** -- 负责消息传输和持久化（如 `SQLiteBackend`、`MemoryBackend`）
- **`Serializer`** -- 编码/解码元数据字典（如 `JSONSerializer`）
- **`MessageBus`** -- 完整的总线接口（发布、轮询、订阅）

`Bus` 类将 `Backend` 与进程内发布/订阅组合在一起：

```python
from mansio import Bus, SQLiteBackend, MemoryBackend

# SQLite 后端（默认）
bus = Bus(backend=SQLiteBackend("workspace/.mansio.db"))

# 内存后端用于测试
bus = Bus(backend=MemoryBackend())

# SQLite 的简写形式
from mansio import SQLiteBus
bus = SQLiteBus("workspace/.mansio.db")
```

`backends/` 包提供两种实现：

| 后端             | 使用场景                       |
| ---------------- | ------------------------------ |
| `SQLiteBackend`  | 生产环境 -- 持久化、跨进程     |
| `MemoryBackend`  | 测试环境 -- 快速、临时         |

## 管理面板

mansio 内置管理面板，用于监控消息总线。通过以下方式启动：

```python
info = bus.start_admin()
print(f"管理面板: {info.url}")  # http://127.0.0.1:8741
```

管理面板提供四个标签页：

- **Dashboard** -- 聚合统计和吞吐量图表
- **Channels** -- 各频道明细，包含消息数和订阅信息
- **Messages** -- 按频道、发送者或类型浏览和过滤消息；发布新消息
- **Subscriptions** -- 查看活跃的进程内订阅

远程访问时启用认证：

```python
info = bus.start_admin(remote=True)
# auth_token 自动生成；通过 Authorization 请求头传递
```

使用 `bus.stop_admin()` 或 `bus.close()` 停止管理面板。
