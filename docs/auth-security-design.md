# 催化智构 CataMind · 登录安全校验体系设计（任务一）

> **实现变更提示（2026-09-07 更新）**：本文保留早期方案供设计追溯。当前主通道已统一为本地账号密码，东翁（唯一超级管理员·所有者）、掌书（一般管理员）、清客（普通用户）均由本地服务器创建；微信确认器代码仅兼容保留，并由 `CATMAT_WECHAT_CHALLENGE_ENABLED=0` 关闭。现行接口、配置与权限矩阵以 `docs/admin-manual.md` 第 5.5 节及 `docs/role-naming-rules.md` 为准。

| 项目 | 内容 |
|---|---|
| **文档版本** | v1.0 |
| **适用平台版本** | v2.4.1（历史设计基线；现行实现见管理员手册） |
| **更新日期** | 2026-09-04 |
| **设计参照** | access + refresh 双令牌、会话撤销与失效重登 |
| **实现状态** | 本地账号密码、JWT、会话撤销与角色授权均已实现；本文接口名可能为历史名称 |

---

## 1. 设计目标与总体机制

在保留平台现有"局域网单机、零登录"使用方式的前提下，引入一套**可平滑启用的双路径鉴权**：

```
                 ┌─ 管理员路径：admin_token（安全令牌）─┐
浏览器 ─ 登录 ──┤                                        ├─ 后端签发 会话令牌
                 └─ 用户路径：username + password_digest ┘        (access + refresh)
                                                                     │
   统一携带  Authorization: Bearer <access_token>  ────────────────┤
                                                                     │
   401 / TOKEN_EXPIRED ──► 单飞刷新（refresh_token 换新）──► 重放原请求
                                                                     │
                           刷新失败 ──► 清空本地凭据 ──► 弹登录界面（过期跳转）
```

- **启用开关**：环境变量 `CATMAT_AUTH_ENABLED=1`。未设置（默认）时后端 `/api/auth/status` 返回 `enabled:false`，前端**无缝跳过登录**，平台行为与现状完全一致 —— 因此该改动对现有用户零打扰。
- **默认关闭的意义**：后端实现完成前，误开也不会锁死平台；前端在 status 探测到 `enabled:true` 但登录端点为 501 时给出明确提示，不会静默绕过。

---

## 2. 双路径鉴权设计

### 2.1 管理员路径（安全令牌）

| 概念 | 说明 |
|---|---|
| **管理令牌（admin_token）** | 长期静态密钥，等价于微信小程序的 AppSecret。由服务器端预置：环境变量 `CATMAT_ADMIN_TOKEN`；未设置时建议**首次启动随机生成并仅打印一次到日志**（TODO 后端）。**不得入库、不得下发到浏览器** |
| **用途** | 仅用于 `/api/auth/admin/login` 换取会话令牌；不直接作为接口鉴权凭据 |
| **会话令牌（access_token）** | JWT（HS256，建议 TTL 30 分钟），签发密钥建议与 admin_token 分离（`CATMAT_JWT_SECRET`），包含 `{mode:'admin', uid, exp, iat}` |
| **刷新令牌（refresh_token）** | 建议 TTL 7 天，**落库并绑定设备/会话 ID**，支持注销（黑名单）与轮换防重放 |
| **过期与刷新** | access_token 过期 → 前端带 refresh_token 调 `admin/refresh` → 返回新 access_token + 轮换后的 refresh_token → 前端重放原请求 |

### 2.2 用户路径（账号 + 密码）

| 概念 | 说明 |
|---|---|
| **账号** | 由管理员预置（TODO 后端：`users` 表或配置文件 seed；本期前端不提供注册） |
| **密码传输加密** | 前端在发送前计算 `password_digest = SHA-256(username + ":" + password)`（Web Crypto），网络层叠加 **HTTPS/TLS 强制**。注：客户端摘要属传输加固，**真正的密码安全依赖服务端加盐慢哈希**（建议 scrypt/bcrypt，TODO 后端对 digest 再哈希存储与校验） |
| **会话令牌** | 同管理员：`{mode:'user', uid, roles, exp}`，roles 预留给前端做权限分级 |
| **失败处理** | `INVALID_CREDENTIALS` → 401；连续失败应限流/锁定（TODO 后端，建议基于 IP + 账号滑动窗口） |

### 2.3 令牌安全存储与携带（前端已实现）

- **存储**：`localStorage` 键 `catamind_auth_v1`，存 `{access_token, refresh_token, expires_at, mode, display_name, username}`。
- **说明与取舍**：单文件 SPA 无第三方脚本、无 iframe 注入面，localStorage 可接受；同时代码中 access_token 常驻内存态（`_authToken`），仅用于每次请求统一注入请求头。
- **更严格选项（后端 TODO 可切换）**：access_token 改 HttpOnly + Secure Cookie（`SameSite=Lax`），refresh_token 走 Cookie 或独立存储；前端鉴权模块已通过"内存态 + 请求头"抽象，切换成本低。
- **携带方式**：除登录/刷新/注销外的所有请求统一 `Authorization: Bearer <access_token>`（由 `api()` 统一注入，业务代码零改动）。
- **注销**：点击右上角身份徽标 → 确认 → 调 `/api/auth/logout`（带 refresh_token）→ 本地清空凭据 → 弹回登录（若 enabled）。后端将 refresh_token 加入黑名单（幂等）。

### 2.4 鉴权失败与过期跳转（前端已实现）

- 请求返回 **401** → 进入"单飞刷新"（并发多个 401 只发起一次 refresh）→ 成功则**自动重放原请求**（用户无感续期）。
- 刷新失败（refresh 过期/被吊销）→ `authClear()` → 弹出登录遮罩并提示"登录已过期，请重新登录"。
- **401 判定携带错误码**：`UNAUTHORIZED`（无/无效 token）、`TOKEN_EXPIRED`（过期，可刷新）、`REFRESH_REVOKED`（refresh 被吊销，须重登）——前端据此区分"静默刷新"与"强制重登"。

---

## 3. 前端 UI 说明

| 元素 | 说明 |
|---|---|
| `#auth-overlay` | 全屏登录遮罩（`z-index:900`），含两个 Tab：**管理员 · 安全令牌** / **用户 · 账号密码** |
| `#auth-chip` | 顶部右侧身份徽标（`🛡 管理员` / `👤 用户名`），点击触发注销确认；未登录/未启用时隐藏 |
| 预览通道 | 访问 `?preview_auth=1` 可在后端未启用时预览登录界面并"跳过进入"（仅供前端联调，非线上功能） |

---

## 4. 前后端接口契约（与后端 TODO 桩一致）

> 基础 URL：`/api`。错误统一返回 `{"ok": false, "error": "...", "code": "..."}`；成功返回 `{"ok": true, "data": {...}}`。
> 当前后端状态：`status` ✅ 已实现；其余 ⏳ 返回 `501 + code:"NOT_IMPLEMENTED"` + 上述 TODO 说明文本。

### 4.1 状态探测（已实现）

| 项 | 内容 |
|---|---|
| 端点 | `GET /api/auth/status` |
| 出参 | `{enabled: bool, modes: {admin: bool, user: bool}, version: 1, note: str}` |
| 用途 | 前端启动时探测：`enabled=false` → 跳过登录，行为与现状一致 |

### 4.2 管理员登录 / 刷新

| 项 | 内容 |
|---|---|
| 端点 | `POST /api/auth/admin/login` |
| 入参 | `{admin_token: str}` |
| 出参 | `{access_token, refresh_token, expires_in(秒), token_type:"Bearer", mode:"admin", display_name:"管理员"}` |
| 失败 | `401 {code:"INVALID_ADMIN_TOKEN"}` |
| 端点 | `POST /api/auth/admin/refresh` |
| 入参 | `{refresh_token: str}` |
| 出参 | 同 login（含轮换后的新 refresh_token） |
| 失败 | `401 {code:"INVALID_REFRESH_TOKEN"}` / `403 {code:"REFRESH_REVOKED"}` |

### 4.3 用户登录 / 刷新

| 项 | 内容 |
|---|---|
| 端点 | `POST /api/auth/user/login` |
| 入参 | `{username: str, password_digest: str}`（digest = 客户端 SHA-256(username:password)，hex 小写） |
| 出参 | `{access_token, refresh_token, expires_in, token_type:"Bearer", mode:"user", display_name, roles: [str]}` |
| 失败 | `401 {code:"INVALID_CREDENTIALS"}`；连续失败限流（后端 TODO） |
| 端点 | `POST /api/auth/user/refresh` ｜ 入参 `{refresh_token}` ｜ 出参同 login ｜ 失败码同 admin |

### 4.4 注销 / 当前会话

| 项 | 内容 |
|---|---|
| 端点 | `POST /api/auth/logout`（幂等）｜ 入参 `{refresh_token}` ｜ 出参 `{ok:true}`（无/无效 refresh 也返回 ok，便于前端无条件清理） |
| 端点 | `GET /api/auth/me` ｜ 出参 `{mode, display_name, roles, exp}` ｜ 失败 `401 {code:"UNAUTHORIZED"/"TOKEN_EXPIRED"}` |

### 4.5 受保护资源约定

| 项 | 内容 |
|---|---|
| 携带 | 所有业务请求（除 auth/* 内 login/refresh/logout/status）统一 `Authorization: Bearer <access_token>` |
| 校验中间件 | 后端应新增统一装饰器/过滤器：解析 JWT → 过期返回 `401 TOKEN_EXPIRED`（前端触发静默刷新）；无/无效返回 `401 UNAUTHORIZED` |
| 双路径差异 | 本期仅区分 admin/user 身份；如需"用户只能访问部分模块"，在 access_token 的 `roles` 基础上做模块级鉴权（TODO 后端，前端已预留 chip 身份展示） |

---

## 5. 新增与修改文件清单

| 文件 | 变更类型 | 内容 |
|---|---|---|
| `server.py` | 修改 | 新增 `AUTH_ENABLED`（读 `CATMAT_AUTH_ENABLED`）；`_dispatch` 增加 `root=="auth"`；新增 `Handler._auth()`——`/api/auth/status` 已实现，`admin/login`、`admin/refresh`、`user/login`、`user/refresh`、`logout`、`me` 为 **501 契约桩**，TODO 文案写明预期逻辑与入参出参 |
| `index.html` | 修改 | ① 新增登录遮罩 DOM（`#auth-overlay` 含双 Tab 表单）与顶部身份徽标 `#auth-chip`；② 新增 CSS（`#auth-overlay`/`.auth-card`/`.auth-tabs` 等）；③ 新增鉴权 JS 模块（状态持久化/`sha256Hex`/请求头注入/401 单飞刷新与重放/过期跳转/登录注销）；④ `api()` 统一注入 `Authorization` 并透传 `code`；⑤ 初始化改为 `authBootstrap()`（status 探测 → 门禁/跳过） |
| `docs/auth-security-design.md` | 新增 | 本文档 |
| `docs/admin-manual.md` | 修改 | 增加「登录与安全」章节（TODO 状态 + 启用步骤 + 运维说明） |
| `README.md` | 修改 | 数据/接口摘要补充 auth 端点与开关说明 |

---

## 6. 后端无缝对接指引（给后续实现者的 TODO 清单）

1. **令牌设施**：新增 JWT 签发/校验（可用标准库 `hmac`+`hashlib` 自实现 HS256，零依赖；或引入 `PyJWT` —— 注意平台零依赖原则，建议自实现 ≤40 行）。
2. **配置**：读 `CATMAT_ADMIN_TOKEN`（缺失则随机生成并 `LOG.warning` 打印一次）、`CATMAT_JWT_SECRET`、`CATMAT_AUTH_ENABLED`。
3. **令牌表**：`CREATE TABLE IF NOT EXISTS auth_sessions (id, mode, uid, refresh_token_hash, expires_at, revoked)`；refresh 用哈希入库，logout/轮换即更新该表。
4. **用户表**：`users(id, username, password_digest_scrypt, roles, disabled)` + 管理员预置脚本/端点（TODO：seed 或 `/api/auth/admin/users` 受 admin 令牌保护）。
5. **密码策略**：服务端对客户端摘要再走 scrypt 加盐；登录失败限流（IP+账号）。
6. **统一鉴权中间件**：在 `_dispatch` 顶层对所有 `root != "auth"` 的请求做 Bearer 校验（enabled 时），过期/无效按 4.5 返回。
7. **开关切换**：全部就绪后设 `CATMAT_AUTH_ENABLED=1` 并重启；前端无需改动即进入登录门禁（status.enabled=true）。

---

## 6.5 网页端管理员令牌通道（与微信并存）

后端在微信登录之外提供 `POST /api/auth/admin/login`（网页科研访问通道）：恒定时间比对 `CATMAT_ADMIN_TOKEN` 后，在 `auth_users` 建立/复用保留账号 `openid=__admin_token__`（role=admin），签发与微信相同的双令牌会话——门禁、refresh 轮换、logout 吊销、审计（含 `admin_login_fail` 误试记录）全部复用。

- 不要求微信 AppID：仅 `CATMAT_ADMIN_TOKEN` + `CATMAT_JWT_SECRET` 即可独立启用（`/api/auth/status.channels.admin=true`）。
- 网页登录框「管理员 · 安全令牌」即调用此端点；用户密码通道暂未开放（前端已标注）。

## 7. 风险与注意事项

- **localStorage 明文存储 refresh_token**：如平台暴露给公网且被 XSS，令牌可被窃取。缓解：无第三方脚本注入面；后端 refresh_token 绑定会话+轮换+黑名单（4.1-4.4）；更高要求切换 HttpOnly Cookie（2.3）。
- **公网暴露前必做**：实现后端鉴权并启用 + HTTPS + 安全组收紧；详见《腾讯云部署实施方案》（`docs/tencent-cloud-deploy.md`）。
- **SHA-256 摘要非最终安全屏障**：文档与代码注释均已明确"真正安全依赖 HTTPS + 服务端加盐慢哈希"。
- 本设计**未引入任何新第三方依赖**（前端 Web Crypto / 后端标准库即可落地）。
