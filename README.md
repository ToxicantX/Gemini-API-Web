# Gemini API Web

基于 `gemini_webapi` 的 Docker 化 Gemini Web API 服务，提供 OpenAI 兼容接口、Gemini 原生能力接口、多账号 Cookie 池、自动轮换、网页登录授权和网页管理端。

本项目适合长期运行在服务器或 NAS 上，用多个 Gemini 账号 Cookie 分担请求，并通过管理端查看调用情况、调整轮换策略、管理 Gems 和 Deep Research 任务。

## 功能

- Docker 一键部署，数据持久化到 SQLite。
- 多账号 Cookie 池，支持导入、手动添加、网页登录授权保存。
- 支持按调用次数轮换、按错误次数轮换、手动切换账号。
- OpenAI 兼容接口：`/v1/chat/completions`、`/v1/models`。
- Gemini 原生接口：生成、流式生成、Gems、Deep Research、文件上传、媒体结果索引。
- 管理端控制台：请求看板、账户设置、授权登录、Gems、Deep Research、媒体生成和媒体结果。
- 服务器部署可开启管理员登录，保护网页控制台和管理接口；外部调用继续使用 API Key。
- 媒体生成支持 `image`、`video`、`audio` 模式；生成结果会保存索引，并尽量缓存到本地，避免 Gemini 原始链接过期后无法查看。
- 自用保护：图片/视频/音频生成会记录尝试和冷却状态，媒体额度错误或上游 2xx 但没有产出媒体时默认冷却 5 小时，避免额度异常时反复请求。

## 快速开始

克隆项目后启动：

```sh
docker compose up -d --build
```

访问管理端：

```text
http://localhost:7860
```

授权浏览器通过管理端的“网页授权”按钮打开。noVNC/websockify 会在授权会话启动后临时运行，管理端会返回可访问的授权链接，通常是：

```text
http://localhost:7860/novnc/vnc.html?autoconnect=true&resize=scale&path=websockify
```

默认数据目录是本机 `./data`，容器内映射为 `/app/data`。SQLite 数据库默认保存到：

```text
data/app.db
```

媒体缓存默认保存到：

```text
data/media-cache/
```

真实数据库、Cookie 文件和本地状态不会提交到 Git。

## 更新 Docker 镜像

本项目默认使用本地源码构建镜像。更新代码后重新构建并替换容器：

```sh
git pull
docker compose --progress plain build
docker compose up -d
```

如果只是修改了 `docker-compose.yml` 或环境变量，也可以直接执行：

```sh
docker compose up -d --build
```

运行数据保存在 `data/`，重建镜像不会清空 SQLite、媒体缓存或已保存账号。

## 添加账号

推荐使用管理端的“网页授权”：

1. 打开 `http://localhost:7860`
2. 进入“账户设置”
3. 点击“网页授权”
4. 在弹出的 noVNC 浏览器中登录 Google/Gemini
5. 回到管理端点击“保存授权 Cookie”

也可以复制示例文件后导入：

```sh
copy data\accounts.example.json data\accounts.json
```

Linux/macOS：

```sh
cp data/accounts.example.json data/accounts.json
```

示例结构：

```json
{
  "accounts": [
    {
      "name": "account-1",
      "__Secure-1PSID": "COOKIE VALUE HERE",
      "__Secure-1PSIDTS": "COOKIE VALUE HERE",
      "enabled": true
    }
  ]
}
```

不要把真实 Cookie 提交到仓库。

## 配置

`docker-compose.yml` 中可调整：

```yaml
environment:
  SWITCH_ON_USES: "40"
  FAILURE_THRESHOLD: "3"
  IMMEDIATE_SWITCH_STATUS_CODES: "429,503"
  REQUEST_TIMEOUT: "300"
  GEMINI_AUTO_REFRESH: "true"
  GEMINI_AUTH_HEADLESS: "false"
  ADMIN_PASSWORD: ""
  ADMIN_SESSION_SECRET: ""
  API_KEYS: ""
  CORS_ALLOW_ORIGINS: "*"
```

含义：

- `SWITCH_ON_USES`：单个账号调用多少次后切换到下一个账号。
- `FAILURE_THRESHOLD`：单个账号连续失败多少次后切换。
- `IMMEDIATE_SWITCH_STATUS_CODES`：遇到这些 HTTP 状态码时立即切换。
- `REQUEST_TIMEOUT`：请求超时时间，单位秒。
- `GEMINI_AUTO_REFRESH`：是否启用 Cookie 自动刷新。
- `GEMINI_AUTH_HEADLESS`：授权浏览器是否无头运行。需要 noVNC 登录时保持 `false`。
- `ADMIN_PASSWORD`：管理员密码。为空时不启用管理端登录，适合本地自用；服务器部署建议设置。
- `ADMIN_SESSION_SECRET`：管理员会话签名密钥。服务器部署建议设置为一段随机长字符串。
- `API_KEYS`：外部调用鉴权密钥，多个值可用英文逗号分隔；也可以在管理端“系统设置”里生成和管理。
- `CORS_ALLOW_ORIGINS`：允许浏览器跨域调用的来源，默认 `*`。公网部署时建议改成你的面板域名，多个来源用英文逗号分隔。

## 管理员登录与外部鉴权

如果要部署到服务器，建议至少配置 `ADMIN_PASSWORD`：

```sh
ADMIN_PASSWORD=your-admin-password docker compose up -d --build
```

也可以同时配置固定会话密钥和外部 API Key：

```sh
ADMIN_PASSWORD=your-admin-password ADMIN_SESSION_SECRET=change-me-to-a-random-secret API_KEYS=sk-your-external-key CORS_ALLOW_ORIGINS=https://your-panel.example.com docker compose up -d --build
```

启用后：

- `http://localhost:7860` 会显示管理员登录页。
- 控制台和管理接口需要管理员 Cookie。
- `/v1/models`、`/v1/chat/completions`、`/v1/gemini/generate` 等外部接口不使用管理员登录鉴权，而是使用 `Authorization: Bearer <API_KEY>`。
- 浏览器环境跨域调用会返回 CORS 头；服务器公网部署时建议把 `CORS_ALLOW_ORIGINS` 收紧为可信域名。
- 未配置任何 `API_KEYS` 且管理端系统设置中没有 API Key 时，外部接口保持无密钥模式，便于本地调试；服务器部署建议生成或配置 API Key。

## OpenAI 兼容接口

列出模型：

```sh
curl http://localhost:7860/v1/models
```

查询单个模型：

```sh
curl http://localhost:7860/v1/models/gemini-3.1-pro
```

聊天补全：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "messages": [
      { "role": "user", "content": "只回复 OK" }
    ]
  }'
```

流式调用：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "stream": true,
    "messages": [
      { "role": "user", "content": "按行输出 1 和 2" }
    ]
  }'
```

多模态消息中的 `image_url` 会被保留为图片链接提示，适合外部 OpenAI 兼容客户端传入图片 URL：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "messages": [
      {
        "role": "user",
        "content": [
          { "type": "text", "text": "请分析这张图片" },
          { "type": "image_url", "image_url": { "url": "https://example.com/image.png" } }
        ]
      }
    ]
  }'
```

说明：OpenAI 兼容接口会把图片 URL 转成 Gemini 可读的文本引用，不会在服务端下载或转存图片；需要上传附件时可以使用 OpenAI 兼容 `/v1/files`，返回的 `file-...` 也可继续用于 Gemini 原生接口。

OpenAI 兼容文件接口：

```sh
curl http://localhost:7860/v1/files \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "purpose=assistants" \
  -F "file=@./sample.pdf"
```

```sh
curl http://localhost:7860/v1/files
curl http://localhost:7860/v1/files/file-xxxx
curl http://localhost:7860/v1/files/file-xxxx/content
curl -X DELETE http://localhost:7860/v1/files/file-xxxx
```

`/v1/files`、`/v1/files/{file_id}`、`/v1/files/{file_id}/content` 和 `DELETE /v1/files/{file_id}` 使用 OpenAI 常见的文件对象结构；文件内容保存在本地 `data/uploads/`，不会自动上传到 Gemini，实际调用时请在 Gemini 原生接口的 `file_ids` 中引用。

Responses API：

```sh
curl http://localhost:7860/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "input": "只回复 OK"
  }'
```

`/v1/responses` 支持 `input` 字符串或 Responses 风格消息数组，并返回 `output_text`。设置 `stream=true` 时会返回 Responses 风格 SSE 事件，包括 `response.output_text.delta` 和 `response.completed`：

```sh
curl http://localhost:7860/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "stream": true,
    "input": "按行输出 1 和 2"
  }'
```

OpenAI 兼容图片生成：

```sh
curl http://localhost:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "prompt": "生成一张赛博朋克风格的猫"
  }'
```

`/v1/images/generations` 会调用 Gemini 图片生成模式，并返回本服务的媒体代理链接；当前仅支持 URL 返回，不支持 `response_format=b64_json`。返回的图片 URL 是随机 token 的内容下载地址，外部客户端展示图片时无需再次携带 API Key。

OpenAI 兼容图片编辑：

```sh
curl http://localhost:7860/v1/images/edits \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "model=gemini" \
  -F "prompt=把图片调整成电影海报风格" \
  -F "image=@./source.png"
```

`/v1/images/edits` 会把上传的 `image` 和可选 `mask` 临时传给 Gemini 图片模式，返回格式与 `/v1/images/generations` 一致；当前同样只支持 URL 返回。

常用模型：

- `gemini`：默认映射到 Gemini 3.1 Pro。
- `gemini-3.1-pro`：真实模型名，原样传给 Gemini。
- `gemini-3.5-flash`：快速模型。
- `gemini-3.1-flash-lite`：轻量快速模型。

旧模型名和未知模型名会被拒绝，例如 `gemini-3-pro`、`gemini-3-flash`、`gemini-3-flash-thinking` 不再透传到底层。

## Gemini 原生接口

原生生成：

```sh
curl http://localhost:7860/v1/gemini/generate \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "prompt": "生成一段简短介绍"
  }'
```

指定媒体生成模式：

```sh
curl http://localhost:7860/v1/gemini/generate \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "mode": "image",
    "prompt": "生成一张赛博朋克风格的猫"
  }'
```

`mode` 可选：

- `image`：图片生成。
- `video`：视频生成。视频任务未确认提交时会返回 409，不计入本次视频尝试。
- `audio`：音频/音乐生成。

响应会返回分类输出：

```json
{
  "ok": true,
  "account": 1,
  "model": "gemini",
  "metadata": [],
  "output": {
    "text": "...",
    "thoughts": null,
    "images": [],
    "videos": [],
    "media": [],
    "web_images": [],
    "deep_research_plan": null
  },
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

媒体结果会写入 `media_outputs` 表。接口返回的媒体项包含：

- `url`：Gemini 返回的原始地址，可能短期有效或需要账号 Cookie。
- `content_url`：本服务提供的缓存/代理访问地址，管理端优先使用它预览。
- `cached`：是否已经缓存到本地 `data/media-cache/`。

`/v1/gemini/media` 媒体历史列表仍受 API Key 或管理员会话保护；只有 `/v1/gemini/media/{media_token}/content` 内容下载链接会按随机 token 公开访问，便于外部 OpenAI 兼容客户端直接预览图片或视频。

查看媒体历史：

```sh
curl "http://localhost:7860/v1/gemini/media?limit=20"
```

访问媒体内容：

```text
http://localhost:7860/v1/gemini/media/{media_token}/content
```

说明：

- 新生成媒体会尽量立刻下载到本地缓存。
- 如果没有缓存，本服务会使用生成账号的 Cookie 代理原始链接。
- 如果 Gemini 返回登录页或 HTML 中间页，接口会返回 502，而不会把 HTML 当成图片/视频返回。
- 单个媒体代理/缓存大小上限为 100MB。
- 图片、视频、音频触发额度错误后会按账号和媒体类型写入冷却状态，默认 5 小时后恢复尝试。
- 明确指定 `mode=image|video|audio` 时，如果上游返回 2xx 但没有对应媒体结果，也会写入该账号该媒体类型的 5 小时冷却。
- 可通过 `GET /v1/media-cooldowns` 查看全局媒体冷却汇总，判断当前账号池是否还能继续生成图片、视频或音频。
- 确认额度已恢复时，可通过 `POST /v1/media-cooldowns/clear` 清除全局媒体冷却；也可以在看板媒体冷却卡片上按类型清除。

媒体冷却汇总示例：

```sh
curl http://localhost:7860/v1/media-cooldowns
```

```json
{
  "ok": true,
  "active_account_count": 2,
  "summary": [
    {
      "kind": "video",
      "label": "视频",
      "total": 2,
      "blocked": 1,
      "available": 1,
      "next": {
        "account_id": 1,
        "account_name": "main",
        "remaining_seconds": 7200
      }
    }
  ]
}
```

清除某一类媒体冷却：

```sh
curl http://localhost:7860/v1/media-cooldowns/clear \
  -H "Content-Type: application/json" \
  -d '{"kind":"video"}'
```

`kind` 可选 `image`、`video`、`audio`；不传 `kind` 时会清除全部媒体冷却。

原生流式接口：

```sh
curl http://localhost:7860/v1/gemini/stream \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini",
    "prompt": "按行输出 1 和 2"
  }'
```

更多原生接口：

- `GET /v1/gemini/gems`
- `POST /v1/gemini/gems`
- `PATCH /v1/gemini/gems/{gem_id}`
- `DELETE /v1/gemini/gems/{gem_id}`
- `POST /v1/gemini/deep-research/plan`
- `POST /v1/gemini/deep-research/start`
- `GET /v1/gemini/deep-research/{job_id}/status`
- `POST /v1/gemini/deep-research/wait`
- `POST /v1/gemini/files`
- `GET /v1/gemini/files`
- `GET /v1/gemini/media`
- `GET /v1/gemini/media/{media_token}/content`
- `GET /v1/gemini/jobs`

## 管理端页面

访问 `http://localhost:7860` 后可以使用以下页面：

- `请求看板`：查看账号、请求数、失败率、模型使用情况、媒体冷却概览和请求日志。
- `原生生成`：测试 Gemini 原生输出，支持文本、分类输出、附件和 Gems。
- `Gems`：查看、创建、更新、删除自定义 system prompt。
- `Deep Research`：创建研究计划、启动、轮询状态和查看结果。
- `媒体结果`：上方生成图片/视频/音频，下方查看媒体历史和缓存/代理链接。
- `账户设置`：调整轮换策略、授权操作、导入/导出/验证/切换账号，并可手动解除账号媒体冷却。

请求日志会记录输出类型、任务/请求 id、媒体数量和错误信息；媒体生成完成后会回填实际 `media_count`，方便在看板里判断图片、视频或音频是否真正产出。早期没有 `job_id/request_id` 的历史日志无法可靠关联媒体结果，会保留原始计数。

## 管理接口

常用状态和管理接口：

- `GET /health`
- `GET /v1/status`
- `GET /v1/media-cooldowns`
- `POST /v1/media-cooldowns/clear`
- `GET /v1/request-logs`
- `GET /v1/settings`
- `PATCH /v1/settings`
- `GET /v1/accounts`
- `POST /v1/accounts`
- `PATCH /v1/accounts/{account_id}`
- `DELETE /v1/accounts/{account_id}`
- `POST /v1/accounts/import`
- `GET /v1/accounts/export`
- `POST /v1/accounts/switch`
- `POST /v1/accounts/validate`
- `POST /v1/accounts/validate-all`
- `POST /v1/accounts/{account_id}/validate`
- `POST /v1/accounts/{account_id}/media-cooldowns/clear`
- `POST /v1/auth/session`
- `POST /v1/auth/save`

账号列表和当前账号状态通过 `GET /v1/status` 返回。

## 持久化数据

SQLite 表包括：

- `accounts`：账号 Cookie 和启用状态。
- `runtime_state`：当前账号、轮换策略。
- `request_logs`：请求日志。
- `jobs`：Deep Research 和长任务状态。
- `media_outputs`：图片、视频、音频等媒体结果索引、本地缓存路径和代理 token。
- `media_cooldowns`：媒体生成冷却状态，额度错误默认 5 小时后恢复尝试，避免额度不足时持续请求。
- `gems_cache`：Gems 缓存。
- `gemini_files`：上传文件记录。

默认至少备份 `data/app.db`。如果希望保留已生成媒体，还需要备份 `data/media-cache/`。如果使用 `data/accounts.json` 导入账号，也请自行安全保存。

## 开发

安装服务端依赖：

```sh
pip install -e ".[server]"
```

运行测试：

```sh
python -m unittest discover -s tests
```

本地启动服务：

```sh
gemini-webapi-server
```

## 安全说明

- 不要提交 `data/app.db`、`data/accounts.json`、`cookies.json` 或任何真实 Cookie。
- 本项目通过 Gemini Web 的 Cookie 工作，不是 Google 官方 API Key 接口。
- Google 可能调整 Gemini Web 页面结构，某些原生能力可能会受账号权限、地区、订阅状态或上游 SDK 适配影响。
- 建议只在可信网络中暴露管理端，公网部署请自行加反向代理鉴权。

## 上游项目

本项目基于 [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) 改造，保留原 SDK 能力，并新增 Docker 服务、多账号轮换和 Web 管理端。

## License

遵循原项目许可证。详见 [LICENSE](LICENSE)。
