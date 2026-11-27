# mansio-core

Core types, storage backends, the message bus, channel authorization, the
token store and the transport/adapter protocols for
[MANSIO](https://github.com/Oaklight/mansio).

This distribution has no runtime dependencies and does not know about HTTP,
the admin panel or the client SDK. Install `mansio` for the server and
`mansio-client` for the agent SDK.

```python
from mansio_core import Bus, MemoryBackend

bus = Bus(backend=MemoryBackend())
```
