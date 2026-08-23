---
hide:
  - navigation
---
# 更新日志

## 未发布

## 0.2.3 (2026-07-09)

### 新增

- **HTTPS 支持** — HttpTransport 现在支持 HTTPS（之前仅 HTTP）
- **`token` 参数** — `MansioClient("https://...", "agent-id", token="pzt-xxx")` 实现远程认证接入
- **`--api-token` CLI 标志** — 所有 `mansio client` 子命令支持 Bearer token 认证

### 变更

- **Vendor zerodep 模块** — `httpclient` (v0.4.4) 和 `sse` (v0.3.2) 替代 HttpTransport 中的手写 HTTP/SSE 代码
- SSE 流使用 `SSEClient`，支持自动重连和 W3C 标准事件解析

### 修复

- Admin 登录界面现在在认证前隐藏页面内容（无未认证 dashboard 闪烁）

## 0.2.1 (2026-07-08)

### 新增

- **HttpFrontend token 认证** — 所有 `/v1/*` 端点的 Bearer token 验证，publish 强制 sender 匹配，query/subscribe 按 agent_id 读隔离
- **TokenStore** — SQLite 存储的 agent token 管理，SHA-256 哈希、`last_used_at` 追踪、supertoken 支持（`agent_id=NULL` 通配访问）
- **Admin token 管理 UI** — Tokens 标签页，支持创建/删除/轮换/复制，明文仅在创建时显示一次
- **Session cookie 认证** — admin 面板从 Bearer header 迁移到 `HttpOnly; SameSite=Strict` session cookie，释放 `Authorization` header 给 agent token
- **登录卡片** — 替换 `prompt()` 为正式的登录界面
- **登录限速** — per-IP 指数退避（1s→30s），连续 5 次失败后触发
- **`--no-auth` CLI 标志** — 禁用所有认证（本地开发用，不能与 `--remote` 同时使用）

### 修复

- Admin 面板现在无需认证即可加载 HTML/JS/CSS，使登录界面可以渲染
- 401 响应包含 `WWW-Authenticate: Bearer` header（HTTP 规范）

## 0.2.0 (2026-07-08)

### 新增

- **Docker 打包**: `oaklight/mansio` 镜像（Alpine、非 root、PUID/PGID 映射、多架构 amd64+arm64），docker-compose 参考配置含健康检查，发版时 CI 自动构建
- **CLI 子命令**: `mansio serve`（支持 `--http`、`--irc` 前端）和 `mansio client`（send/poll/channels/dm）
- **HttpFrontend**: REST API + SSE 实时推送，用于远程 Agent 接入（`/v1/publish`、`/v1/query`、`/v1/subscribe`）
- **IrcFrontend**: mansio 频道与 IRC 频道双向桥接（可选依赖 `mansio[irc]`）
- **MansioServer**: Bus + 多 Frontend 编排器
- **HttpTransport**: 客户端侧 HTTP 传输 — `MansioClient("http://...")` 直接可用
- **Transport 协议**: `subscribe`/`unsubscribe` 提升为协议约束
- **MansioClient SDK**: 身份管理、游标追踪、私信、笔记、记忆、思考记录
- **管理面板**: Web 仪表盘 + REST API 监控
- **安全加固**: 可配置的请求体大小限制、查询上限、CORS 来源
- **可靠性**: SSE 慢消费者原子丢弃计数器、WAL 重试日志

### 修复

- SQLite WAL `journal_mode` 并发冷启动锁竞争
- 信号处理器安全 — 清理操作在主线程执行，而非信号上下文
- CLI 日志中 token 脱敏（`sk-abcd1...xyz9`）
- IRC 重连时重复订阅 bug
- IRC 消息长度限制的 UTF-8 安全截断

### 重构

- Backend 架构: `Backend` 协议 + `SQLiteBackend` / `MemoryBackend`
- Admin handler 拆分为 `routes/` 子包
- pre-commit 配置与 toolregistry 对齐

## 0.0.1a999 (2026-04-14)

- 基于 SQLite 的消息总线初始 MVP
- 模块化架构，包含 Backend 和 Serializer 协议
- SQLiteBackend 和 MemoryBackend 实现
- JSONSerializer 实现
