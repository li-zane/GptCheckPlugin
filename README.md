# sub2api AT 刷新机

一个用于 sub2api 的本地外挂管理服务：定时读取 sub2api 里导入的 GPT 账号，发现错误账号后用 Playwright 登录 ChatGPT，读取 `https://chatgpt.com/api/auth/session` 中的 `accessToken`，再把新的 AT 写回 sub2api 对应账号。

## 当前结构

- `backend/`: FastAPI、SQLite、sub2api 客户端、刷新任务、邮箱取件适配器、Playwright 登录流程。
- `frontend/`: Vite + React 管理面板，包含登录、概览、账号、邮箱、历史视图。
- `.env.example`: 本地配置模板。

## 后端启动

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
python -m playwright install chromium
npm run backend:dev
```

生产环境请把 `.env` 里的 `APP_ADMIN_KEY`、`APP_SESSION_SECRET`、`APP_ENCRYPTION_KEY` 换成长随机值，并把 `COOKIE_SECURE=true` 放在 HTTPS 反代之后使用。

## 前端启动

```powershell
cd frontend
npm install
npm run dev
```

默认访问 `http://127.0.0.1:5173`，前端会把 `/api` 代理到 `http://127.0.0.1:8000`。

## sub2api 配置

默认按当前 sub2api 公开前端接口形态配置：

```env
SUB2API_BASE_URL=http://localhost:8080/api/v1
SUB2API_ACCOUNTS_PATH=/admin/accounts
SUB2API_ACCESS_TOKEN_PATH=credentials.access_token
```

如果你的 sub2api 管理接口鉴权不是 `Authorization: Bearer <token>`，可以调整：

```env
SUB2API_AUTH_HEADER=Authorization
SUB2API_AUTH_SCHEME=Bearer
SUB2API_AUTH_TOKEN=your-sub2api-admin-token
```

## 邮箱导入格式

面板支持批量导入，一行一个；空行和 `#` 开头的行会被忽略。每行支持 `----`、`|`、`,` 三种分隔符。默认会按取件邮箱后缀自动识别 `outlook` / `hotmail` / `gmail`。推荐格式：

```text
gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token
other@example.com----other@outlook.com----mail_password----client_id----refresh_token
```

也支持 GPT 邮箱和取件邮箱相同的简写：

```text
mail@example.com----mail_password----client_id----refresh_token
```

`hotmail.*` 会识别为 `hotmail`；`outlook.com`、`outlook.com.cn`、`live.com`、`live.cn`、`msn.com` 等会识别为 `outlook`。两者会优先尝试 Microsoft Graph `Mail.Read`，失败后自动尝试 O2/IMAP (`IMAP.AccessAsUser.All` / `wl.imap`)。取件失败会写入邮箱记录的 `last_error`，不会让刷新任务崩溃。

### Cloudflare 转发到 Gmail 的自定义域名邮箱

像 `edu.rainynight.me` 这种通过 Cloudflare Email Routing 转发到 Google 邮箱的账号，可以直接导入成 `gmail` 类型。推荐 3 列格式：

```text
custom@edu.rainynight.me----yourname@gmail.com----gmail_app_password
```

含义是：

- 第 1 列：GPT / 原始收件邮箱，也就是域名邮箱地址。
- 第 2 列：实际取件邮箱，也就是接收转发邮件的 Gmail 或 Google Workspace 邮箱。
- 第 3 列：该 Gmail 邮箱的 IMAP App Password。

如果转发目标不是 `@gmail.com`，而是 Google Workspace 自定义域名邮箱，也可以在面板里把识别方式手动选成 `Gmail / Google Workspace` 后导入同样的 3 列格式。

后端会登录这个 Gmail 收件箱，并从邮件头里的 `Delivered-To`、`X-Original-To`、`Original-Recipient`、`X-Forwarded-To`、`To` 等字段中提取原始收件人，只返回真正发给该域名邮箱地址的邮件。这样即使同一个 Gmail 里混有多个转发别名，也不会把别人的验证码串进来。

`gmail` 类型支持读取收件箱和垃圾邮件；验证码轮询也会同时检查这两个文件夹。使用前请确认目标 Google 邮箱已开启 IMAP，并使用 App Password，而不是账号登录密码。

如果你本机同时部署了 `mail-manager`，还可以把它的路由配置文件接进来，让当前项目在账号同步时自动为命中域名的新 GPT 账号创建 Gmail mailbox 绑定：

```env
MAIL_MANAGER_ROUTE_CONFIG_PATH=/root/apps/mail-console-x1/config/mail-routing.json
```

当前自动绑定逻辑会读取 `mail_route_domains` 和其首个可用的 `imap` 路由账号；如果该账号是 Gmail IMAP 路由，就会自动把命中域名的新账号绑定为 `gmail` provider，并同步复用它的 App Password 和代理地址。

## 自定义取件接口

导入时把 provider 写成 `custom`，第七列写取件 URL：

```text
gpt@example.com----mail@example.com----password----client_id----refresh_token----custom----https://your-mail-api.example.com/code
```

后端会 `POST`：

```json
{
  "gpt_email": "gpt@example.com",
  "mailbox_email": "mail@example.com",
  "after": "2026-05-23T12:00:00+08:00"
}
```

自定义接口返回：

```json
{ "code": "123456" }
```

或：

```json
{ "status": "failed", "error": "refresh token expired" }
```

管理面板里点击邮箱行的邮件图标时，`outlook` / `hotmail` 会直接读取 Microsoft Graph 的收件箱和垃圾箱摘要。`custom` 邮箱会继续请求同一个 `custom_fetch_url`，但请求体会带上 `action=list_messages`：

```json
{
  "action": "list_messages",
  "folder": "inbox",
  "limit": 20,
  "gpt_email": "gpt@example.com",
  "mailbox_email": "mail@example.com"
}
```

`custom` 邮箱的所有邮件默认显示在收件箱。自定义接口可返回：

```json
{
  "messages": [
    {
      "id": "message-id",
      "subject": "Your verification code",
      "sender": { "name": "OpenAI", "address": "noreply@tm.openai.com" },
      "body_preview": "Your code is 123456",
      "received_at": "2026-05-23T12:00:00+08:00"
    }
  ]
}
```

## 刷新流程

1. 后端定时调用 sub2api 账号列表接口。
2. 以账号邮箱作为 ID，更新本地账号快照。
3. 对 `status` 包含 `error/fail/invalid/expired/disabled` 或 `schedulable=false` 的 GPT 账号创建刷新任务。
4. 如果账号已有 `credentials.refresh_token` / `refreshToken` / `rt`，后端先调用 sub2api 的账号刷新接口，尽量直接用 RT 刷新 AT。
5. 如果缺少可用 RT 或 RT 刷新失败，后端会创建 OpenAI OAuth PKCE 流程，用邮箱验证码登录并用 `code + code_verifier` 换取 `access_token`、`refresh_token`、`id_token`。
6. 默认先用 `curl_cffi` 协议流程尝试完成 OpenAI OAuth，协议失败后再用 Playwright 浏览器完成 consent/callback；遇到 `add phone` / `phone number required` 会直接失败，不接码。
7. 如果 OAuth 仍失败，再回退到旧的 ChatGPT session 刷 AT 路径。
8. 取件适配器轮询最新邮箱验证码邮件。
9. 登录或 OAuth 成功后，后端把新 AT/RT/ID token 和账号信息写回 sub2api，然后尝试调用 clear-error 和 recover-state。
10. 如果页面或 session 结果出现 `account_deactive`，本地标记为 deactive，后续不再重复刷新。

OAuth 写回会标准保存 `credentials.refresh_token`。如果原账号使用 `rt` 字段，或 sub2api 的隐藏敏感字段状态表明现有 RT 可能不是标准 `refresh_token` 字段，也会同步写入 `credentials.rt` 兼容旧数据。

协议刷新并发和浏览器登录并发可以分开设置：`PROTOCOL_REFRESH_MAX_CONCURRENCY` 控制协议路径，`BROWSER_REFRESH_MAX_CONCURRENCY` 控制 Playwright 回退，旧的 `REFRESH_MAX_CONCURRENCY` 会作为协议并发的兼容默认值。

OpenAI OAuth 默认使用 Codex CLI 客户端配置：`OPENAI_OAUTH_CLIENT_ID=app_EMoamEEZ73f0CkXaXp7hrann`，回调地址为 `http://localhost:1455/auth/callback`。后端会先用 `curl_cffi` 协议流程尝试获取 callback `code`，失败后再回退到浏览器流程；浏览器路径优先使用 Camoufox Firefox 持久化上下文，并复用 `data/browser-profiles/<email>/` 下的账号 profile，在进入 OAuth 页后先等待 Cloudflare challenge 清掉，再继续邮箱验证码与 consent 流程；若 Camoufox 当前不可用，再回退到普通 Playwright Chromium。浏览器路径会直接截获回调 URL 中的 `code`，不需要本机真的监听 `1455` 端口。

管理面板保存运行设置时，会同步写入项目根目录 `.env`。这包括 sub2api 地址、x-api key、账号恢复任务总开关、刷新成功后的 sub2api 状态恢复开关、自动任务开关、并发数、浏览器内存阈值、显示时区和站点名；`.env` 已在 `.gitignore` 中，生产部署时建议把项目根目录或 `.env` 作为持久化配置挂载。

## 安全边界

- 管理面板使用管理密钥登录，登录态存放在 httpOnly、SameSite=Strict Cookie。
- 邮箱密码、client id、refresh token 会用 `APP_ENCRYPTION_KEY` 派生密钥后加密存入 SQLite。
- 本地账号快照会脱敏 token/password/secret 字段。
- 后端不会在日志或前端响应中返回新的 ChatGPT access token，只保存末尾 8 位用于任务核对。

ChatGPT 登录页和 session 响应属于非稳定网页流程，生产环境建议先用少量账号灰度运行。
