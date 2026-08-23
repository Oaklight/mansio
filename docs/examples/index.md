---
hide:
  - navigation
---
# 示例

## 基本用法

```python
from mansio import SQLiteBus

with SQLiteBus("chat.db") as bus:
    bus.publish("general", "agent-a", "text", "你好！")
    messages = bus.poll("general")
    for msg in messages:
        print(f"{msg.sender}: {msg.payload}")
```

## 使用 MemoryBackend 进行测试

```python
from mansio import Bus, MemoryBackend

with Bus(backend=MemoryBackend()) as bus:
    bus.publish("test-ch", "test-agent", "text", "测试消息")
    assert len(bus.poll("test-ch")) == 1
```

## 自定义后端

```python
from mansio import Bus
from mansio.protocols import Backend
from mansio.types import Message

class MyCustomBackend:
    """实现 Backend 协议。"""

    def store(self, message: Message) -> None: ...
    def query(self, channel: str, after: str | None = None, limit: int = 100) -> list[Message]: ...
    def list_channels(self) -> list[str]: ...
    def close(self) -> None: ...
    def count_messages(self, channel: str | None = None) -> int: ...
    def query_all(self, after=None, limit=100, channel=None, sender=None, msg_type=None) -> list[Message]: ...
    def get_stats(self) -> dict: ...
    def query_recent_timestamps(self, seconds: int = 60) -> list[str]: ...

bus = Bus(backend=MyCustomBackend())
```

## 启动管理面板

```python
from mansio import SQLiteBus

bus = SQLiteBus("workspace/.mansio.db")

# 启动管理面板（在后台线程中运行）
info = bus.start_admin()
print(f"管理面板: {info.url}")

# ... 正常使用 bus ...

# 完成后停止管理面板
bus.stop_admin()
```

远程访问并启用认证：

```python
info = bus.start_admin(remote=True)
print(f"管理面板: {info.url}")
print(f"认证令牌: {info.token}")  # 自动生成的令牌
```
