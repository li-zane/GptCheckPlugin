# Sub2API AT Guardian

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

面板支持批量导入，一行一个；空行和 `#` 开头的行会被忽略。每行支持 `----`、`|`、`,` 三种分隔符。默认会按取件邮箱后缀自动识别 `outlook` / `hotmail`。推荐格式：

```text
gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token
other@example.com----other@outlook.com----mail_password----client_id----refresh_token
```

也支持 GPT 邮箱和取件邮箱相同的简写：

```text
mail@example.com----mail_password----client_id----refresh_token
```

`hotmail.*` 会识别为 `hotmail`；`outlook.com`、`outlook.com.cn`、`live.com`、`live.cn`、`msn.com` 等会识别为 `outlook`。两者会优先尝试 Microsoft Graph `Mail.Read`，失败后自动尝试 O2/IMAP (`IMAP.AccessAsUser.All` / `wl.imap`)。取件失败会写入邮箱记录的 `last_error`，不会让刷新任务崩溃。

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
4. Playwright 打开 ChatGPT 登录页，输入邮箱，等待邮箱验证码。
5. 取件适配器轮询最新验证码邮件。
6. 登录成功后访问 session 接口并提取 `accessToken`。
7. 后端把新 AT 写回 sub2api，然后尝试调用 clear-error 和 recover-state。
8. 如果页面或 session 结果出现 `account_deactive`，本地标记为 deactive，后续不再重复刷新。

## 安全边界

- 管理面板使用管理密钥登录，登录态存放在 httpOnly、SameSite=Strict Cookie。
- 邮箱密码、client id、refresh token 会用 `APP_ENCRYPTION_KEY` 派生密钥后加密存入 SQLite。
- 本地账号快照会脱敏 token/password/secret 字段。
- 后端不会在日志或前端响应中返回新的 ChatGPT access token，只保存末尾 8 位用于任务核对。

ChatGPT 登录页和 session 响应属于非稳定网页流程，生产环境建议先用少量账号灰度运行。
