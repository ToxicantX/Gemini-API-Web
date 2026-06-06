# Gemini API Web

基于 `gemini_webapi` 的 Docker 化 Gemini Web API 服务，提供 OpenAI 兼容接口、Gemini 原生能力接口、多账号 Cookie 池、自动轮换、网页登录授权和网页管理端。

本项目适合长期运行在服务器或 NAS 上，用多个 Gemini 账号 Cookie 分担请求，并通过管理端查看调用情况、调整轮换策略、管理 Gems 和 Deep Research 任务。

## 功能

- Docker 一键部署，数据持久化到 SQLite。
- 多账号 Cookie 池，支持导入、手动添加、网页登录授权保存。
- 支持按调用次数轮换、按错误次数轮换、手动切换账号。
- OpenAI 兼容接口：`/v1/chat/completions`、`/v1/completions`、`/v1/responses`、`/v1/models`。
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

服务器部署建议先复制环境变量模板并修改其中的管理员密码、会话密钥和外部 API Key：

```sh
cp .env.example .env
docker compose up -d --build
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
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

出于安全考虑，`docker-compose.yml` 默认只映射管理端 `7860`。noVNC 的 `6080` 端口只在容器内部使用，并通过 `/novnc` 同源代理访问；服务器部署时不要把 `6080` 直接暴露到公网，否则会绕过管理端登录保护。

默认数据目录是本机 `./data`，容器内映射为 `/app/data`。SQLite 数据库默认保存到：

```text
data/app.db
```

媒体缓存默认保存到：

```text
data/media-cache/
```

真实数据库、Cookie 文件和本地状态不会提交到 Git。

### 媒体对象存储

默认情况下，图片、视频、音频结果会缓存到本地 `data/media-cache/`，媒体历史返回同服务的代理链接。服务器部署后如果希望外部客户端拿到长期可访问的公开媒体地址，可以在 `.env` 中开启 S3 兼容对象存储：

```env
OBJECT_STORAGE_ENABLED=true
OBJECT_STORAGE_ENDPOINT=https://s3.example.com
OBJECT_STORAGE_REGION=auto
OBJECT_STORAGE_BUCKET=gemini-media
OBJECT_STORAGE_ACCESS_KEY_ID=your-access-key
OBJECT_STORAGE_SECRET_ACCESS_KEY=your-secret-key
OBJECT_STORAGE_PREFIX=gemini-web
OBJECT_STORAGE_PUBLIC_URL=https://cdn.example.com/gemini-media
OBJECT_STORAGE_FORCE_PATH_STYLE=true
```

这些环境变量会作为容器首次启动的系统设置默认值，也可以在管理端“系统设置”里修改；管理端保存后的 SQLite 设置优先于环境变量。只有请求显式启用媒体保存时才会上传对象存储，未配置或上传失败时仍会保留本地缓存/代理链接。

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

`docker-compose.yml` 默认使用 `restart: unless-stopped`，适合服务器长期运行；宿主机重启或进程异常退出后会自动拉起，手动执行 `docker compose stop` 时不会反复重启。

默认宿主机端口是 `7860`。如果服务器上端口冲突，可以在 `.env` 中修改 `HOST_PORT=17860`；容器内服务端口默认仍是 `PORT=7860`，通常不需要改。

镜像内置 Docker 健康检查，会定时访问容器内 `/health`。更新或重启后可以查看：

```sh
docker compose ps
curl http://localhost:7860/health
```

`/health` 不需要管理员登录或 API Key，只返回非敏感摘要，适合反向代理和监控系统探活。为兼容常见部署平台，也支持 `GET/HEAD /healthz`、`GET/HEAD /readyz` 和 `GET/HEAD /livez`。

如果部署时设置 `GIT_COMMIT`，`/health` 会在 `build.commit` 中返回该值，方便确认服务器当前运行的镜像或源码版本；未设置时返回空字符串。

部署完成后可以运行轻量 smoke 测试，确认探活、外部 API Key 鉴权和模型列表可用：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key
```

如果还没有生成 API Key，可以先不传 `--api-key`；脚本会确认外部 `/v1/*` 已被 401 正确保护。

如果外部客户端或 API 网关不是使用 `Authorization: Bearer`，可以通过 `--api-key-header` 指定 smoke 请求使用的鉴权头；需要一次性验证所有常见写法时，可以加 `--auth-header-probes`：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --api-key-header x-api-key
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --auth-header-probes
```

需要验证外部 SDK 或 API 网关常用的非消耗型探测端点时，可以加 `--probe-endpoints`。脚本会检查 `/v1`、`HEAD /v1/models`、`/v1/models/{model}`，以及根路径误填和旧版 SDK 常用的 `/models`、`/models/{model}`、`/v1/engines`、`/v1/engines/{model}`、`/engines`、`/engines/{model}` 只读别名：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --probe-endpoints --probe-model gemini-3.1-pro
```

需要验证外部客户端常见错误路径时，可以加 `--error-probes`。脚本会检查 `/v1/models`、`/models`、`/engines` 未授权 `401`，不存在接口和 rootless 模型/engine 详情的 `404`，以及错误方法的 `405` 是否都返回 OpenAI 兼容错误体，并带有 `X-Request-ID`，不会触发模型调用：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --error-probes
```

需要验证部署平台或反向代理常用的探活别名时，可以加 `--health-probes`。脚本会检查 `GET/HEAD /healthz`、`GET/HEAD /readyz` 和 `GET/HEAD /livez`：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --health-probes
```

需要验证浏览器跨域调用时，可以加 `--cors-probes`。脚本会向 `/v1/chat/completions` 发送 `OPTIONS` 预检，检查 `Authorization`、`X-API-Key`、`API-Key`、`OpenAI-API-Key`、`Content-Type` 和 `POST` 是否被允许；随后再带 `Origin` 请求 `/v1/models`，确认真实响应会暴露 `X-Request-ID`，方便前端和外部 SDK 排查线上问题：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --cors-probes --cors-origin https://your-panel.example.com
```

需要验证文件接口的 HEAD 探测时，可以加 `--file-probes`。脚本会检查 `HEAD /v1/files` 和 `HEAD /v1/gemini/files`，不会上传文件或触发模型调用：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --file-probes
```

需要验证 OpenAI 兼容文件接口的完整生命周期时，可以传入一个本地小文件。脚本会实际调用 `/v1/files` 上传、列表、详情、内容读取、`/v1/gemini/files` 原生列表和删除；不会触发模型调用：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --file-smoke-path ./sample.txt --file-smoke-purpose assistants
```

需要验证媒体历史和代理链接结构时，可以加 `--media-history`。这不会触发模型调用，只会检查 `/v1/gemini/media` 是否能返回媒体索引、原始 URL 和 `content_url`：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --media-history
```

需要验证 Gemini 原生只读入口时，可以加 `--native-probes`。这不会触发生成调用，只会检查 `/v1/gemini/gems` 和 `/v1/gemini/jobs` 是否能被 API Key 正确保护和访问：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --native-probes
```

需要给外部监控或调用方检查“当前是否具备真实生成条件”时，可以加 `--readiness-probes`。这不会触发模型调用，只会读取 `GET /v1/generation-readiness`，返回账号池是否就绪、不可用原因和媒体冷却摘要；没有可用账号时 `ready=false`，但 HTTP 仍为 `200`，便于监控区分“服务可访问”和“账号未授权/已失效”：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --readiness-probes
```

如果已经有媒体历史记录，还可以同时验证 `content_url` 的公开内容链接是否支持 `HEAD` 探测，并检查 `Content-Type` 是否与图片/视频/音频类型匹配、`Content-Length` 是否为合法非负数。这个检查适合发现反向代理、对象存储或 CDN 把媒体响应头弄丢，导致外部客户端无法预览的问题：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --media-history --media-content-probes --media-content-probe-limit 3
```

也可以同时验证管理员登录链路，确认登录 Cookie 能访问管理接口：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --admin-username admin --admin-password your-admin-password
```

如果要把服务交给外部客户端真实生成使用，建议先加 `--require-account` 验收账号池。这个检查只读取 `/health`，不会消耗模型请求；当没有启用且未过期的 Gemini 账号时会直接失败，避免接口层正常但真实调用不可用：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --require-account
```

需要确认外部客户端真实模型调用也可用时，可以显式传入 `--chat-prompt`。这会实际调用 `/v1/chat/completions` 并消耗一次账号请求：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --chat-prompt "用一句话回复 ok" --chat-model gemini-3.1-pro
```

如果外部客户端会使用流式响应，可以再加 `--chat-stream`，脚本会额外验证 `stream=true` 的 SSE chunk 和 `[DONE]` 结束标记：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --chat-prompt "用一句话回复 ok" --chat-stream
```

需要验证 OpenClaw 等工具型客户端能执行函数调用时，可以加 `--chat-tool-probe`。这会实际调用 `/v1/chat/completions`，要求返回 OpenAI 标准 `tool_calls`、`finish_reason=tool_calls`，并校验 `function.arguments` 是可解析的 JSON 字符串：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --chat-tool-probe
```

需要验证旧版 OpenAI Completions API 时，可以显式传入 `--completion-prompt`，脚本会实际调用 `/v1/completions` 并校验 `text_completion` 返回结构：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --completion-prompt "补全这句话：Gemini API Web" --completion-model gemini-3.1-pro
```

如果外部客户端会使用 Completions 流式响应，可以再加 `--completion-stream`，脚本会额外验证 `text_completion.chunk` 和 `[DONE]` 结束标记：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --completion-prompt "补全这句话：Gemini API Web" --completion-stream
```

需要验证 Gemini 原生生成接口时，可以显式传入 `--gemini-prompt`，脚本会实际调用 `/v1/gemini/generate` 并校验分类输出结构：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --gemini-prompt "用一句话回复 ok" --gemini-model gemini-3.1-pro
```

如果外部客户端会使用 Gemini 原生流式接口，可以再加 `--gemini-stream`，脚本会额外验证 `/v1/gemini/stream` 的 SSE final 包和 `[DONE]` 结束标记：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --gemini-prompt "用一句话回复 ok" --gemini-stream
```

需要验证新版 OpenAI Responses API 时，可以显式传入 `--responses-prompt`，脚本会实际调用 `/v1/responses` 并校验 `output_text` 和 `output` 结构：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --responses-prompt "用一句话回复 ok" --responses-model gemini-3.1-pro
```

如果外部客户端会使用 Responses 流式事件，可以再加 `--responses-stream`，脚本会额外验证 `response.completed` 事件和 `[DONE]` 结束标记：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --responses-prompt "用一句话回复 ok" --responses-stream
```

需要验证 OpenAI 图片端点时，可以显式传入 `--image-prompt`。这会实际调用 `/v1/images/generations` 并消耗一次图片生成次数：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --image-prompt "生成一张简单的蓝色图标" --image-model gpt-image-2 --image-response-format url
```

需要验证 OpenAI 图片编辑或图片变体端点时，可以传入本地图片文件。脚本会实际调用 `/v1/images/edits` 或 `/v1/images/variations`，并消耗一次图片生成次数：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --image-edit-file ./source.png --image-edit-prompt "把背景改成浅蓝色" --image-edit-mask-file ./mask.png
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --image-variation-file ./source.png
```

需要验证 OpenAI 音频转写或翻译端点时，可以传入本地音频文件。脚本会使用 multipart/form-data 实际调用 `/v1/audio/transcriptions` 或 `/v1/audio/translations`，并校验返回的 `text` 字段：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --audio-transcription-file ./sample.wav --audio-model gemini-3.1-pro
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --audio-translation-file ./sample.mp3 --audio-model gemini-3.1-pro
```

如果外部客户端使用非 JSON 音频响应，可以加 `--audio-response-format text|verbose_json|srt|vtt` 验证对应格式：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --audio-transcription-file ./sample.wav --audio-response-format srt
```

Smoke 测试会把 `/health` 返回的部署安全警告打印为 `health warning: ...`，例如仍在使用 Docker Compose 占位管理员密码或占位会话密钥。正式上服务器前建议开启严格模式，让这些警告直接导致测试失败：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --fail-on-warnings
```

Smoke 测试默认每个 HTTP 请求最多等待 120 秒。图片生成、Responses 流式或代理链路较慢时，可以按需调大：

```sh
python scripts/smoke_deploy.py --base-url http://localhost:7860 --api-key sk-your-external-key --chat-prompt "用一句话回复 ok" --timeout 300
```

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

`.env` 或 `docker-compose.yml` 中可调整：

```yaml
environment:
  SWITCH_ON_USES: "40"
  FAILURE_THRESHOLD: "3"
  IMMEDIATE_SWITCH_STATUS_CODES: "429,503"
  REQUEST_TIMEOUT: "300"
  GEMINI_AUTO_REFRESH: "true"
  GEMINI_AUTH_URL: "https://gemini.google.com/"
  GEMINI_AUTH_HEADLESS: "false"
  GEMINI_PROXY: ""
  ADMIN_USERNAME: "admin"
  ADMIN_PASSWORD: "change-this-admin-password"
  ADMIN_SESSION_SECRET: "change-this-to-a-long-random-string"
  ADMIN_COOKIE_SECURE: "false"
  REQUIRE_API_KEY: "true"
  API_KEYS: ""
  CORS_ALLOW_ORIGINS: "https://your-panel.example.com"
  HOST_PORT: "7860"
  HOST: "0.0.0.0"
  PORT: "7860"
```

含义：

- `SWITCH_ON_USES`：单个账号调用多少次后切换到下一个账号。
- `FAILURE_THRESHOLD`：单个账号连续失败多少次后切换。
- `IMMEDIATE_SWITCH_STATUS_CODES`：遇到这些 HTTP 状态码时立即切换。
- `REQUEST_TIMEOUT`：请求超时时间，单位秒。
- `GEMINI_AUTO_REFRESH`：是否启用 Cookie 自动刷新。
- `GEMINI_AUTH_URL`：网页登录授权浏览器的起始地址，默认打开 Gemini 官网。
- `GEMINI_AUTH_HEADLESS`：授权浏览器是否无头运行。需要 noVNC 登录时保持 `false`。
- `GEMINI_PROXY`：Gemini Web 请求代理。服务器无法直连 Google 时可填写 HTTP/HTTPS 代理地址；为空时会继续兼容读取系统的 `HTTPS_PROXY`、`HTTP_PROXY` 等变量。
- `ADMIN_USERNAME`：管理员账号。为空时管理端保持旧版“只输密码”模式；服务器部署建议设置。
- `ADMIN_PASSWORD`：管理员密码。为空时不启用管理端登录，只适合本地可信环境；Docker Compose 默认提供占位值，服务器部署必须修改。
- `ADMIN_SESSION_SECRET`：管理员会话签名密钥。Docker Compose 默认提供占位值，服务器部署必须改成一段随机长字符串。
- `ADMIN_COOKIE_SECURE`：管理员会话 Cookie 是否只允许 HTTPS 发送。直连本地 HTTP 保持 `false`；通过 HTTPS 域名反向代理部署时建议设为 `true`。
- `REQUIRE_API_KEY`：是否强制外部 `/v1/*` 接口必须使用 API Key。服务器部署建议设为 `true`；即使暂未配置或生成 API Key，也不会让外部接口无密钥开放。
- `API_KEYS`：外部调用鉴权密钥，多个值可用英文逗号分隔；也可以在管理端“系统设置”里生成和管理。
- `CORS_ALLOW_ORIGINS`：允许浏览器跨域调用的来源，Docker Compose 默认使用示例面板域名。公网部署时建议改成你的实际面板或调用方域名，多个来源用英文逗号分隔；本地调试需要任意来源时可手动设为 `*`。
- `HOST_PORT`：宿主机暴露端口，服务器端口冲突时优先修改这个值。
- `HOST`：容器内服务监听地址，Docker 部署通常保持 `0.0.0.0`。
- `PORT`：容器内服务监听端口，健康检查和端口映射会使用该值；通常保持 `7860`。

开启 `REQUIRE_API_KEY=true` 时，至少需要同时配置 `API_KEYS` 或 `ADMIN_PASSWORD` 之一：前者可直接给外部客户端调用，后者可先登录管理端再到“系统设置”生成第一个 API Key。若两者都为空，外部接口会被拒绝，`/health` 会返回配置告警。

如果仍使用 Docker Compose 默认的占位管理员密码或会话密钥，服务可以启动并完成首次引导，但 `/health` 会返回安全告警；服务器上线前必须改成自己的强密码和随机密钥。

## 管理员登录与外部鉴权

如果要部署到服务器，必须修改默认管理员密码和会话密钥，建议至少配置 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 和 `ADMIN_SESSION_SECRET`：

```sh
ADMIN_USERNAME=admin ADMIN_PASSWORD=your-admin-password ADMIN_SESSION_SECRET=change-me-to-a-random-secret docker compose up -d --build
```

也可以同时配置固定会话密钥、HTTPS 安全 Cookie 和外部 API Key：

```sh
ADMIN_USERNAME=admin ADMIN_PASSWORD=your-admin-password ADMIN_SESSION_SECRET=change-me-to-a-random-secret ADMIN_COOKIE_SECURE=true REQUIRE_API_KEY=true API_KEYS=sk-your-external-key CORS_ALLOW_ORIGINS=https://your-panel.example.com docker compose up -d --build
```

启用后：

- `http://localhost:7860` 会显示管理员登录页。
- 如果配置了 `ADMIN_USERNAME`，登录页会要求同时输入管理员账号和密码；未配置时只要求密码，兼容旧部署。
- 控制台和管理接口需要管理员 Cookie。
- 管理员登录连续输错会按客户端来源短时间限速，响应 `429` 和 `Retry-After`，避免公网部署时被简单爆破。
- 网页授权使用的 noVNC 页面和 WebSocket 通道也需要管理员 Cookie，避免服务器部署时暴露授权浏览器。
- `/v1/models`、`/v1/chat/completions`、`/v1/gemini/generate` 等外部接口不使用管理员登录鉴权，而是使用 `Authorization: Bearer <API_KEY>`。
- 只配置 `ADMIN_PASSWORD` 不会自动保护外部 `/v1/*` API；服务器部署请同时配置 `API_KEYS`，或设置 `REQUIRE_API_KEY=true` 后在管理端生成 API Key。
- 鉴权头兼容 `Authorization: Bearer <API_KEY>`、大小写不同的 `bearer`、`X-API-Key: <API_KEY>`、`API-Key: <API_KEY>`，以及部分客户端使用的 `OpenAI-API-Key: <API_KEY>`。
- Gemini 原生的 Gems、Deep Research、文件、媒体和 Jobs 接口也属于外部 API Key 调用面；账户池、授权浏览器、系统设置和请求日志仍属于管理端。
- 浏览器环境跨域调用会返回 CORS 头；服务器公网部署时建议把 `CORS_ALLOW_ORIGINS` 收紧为可信域名。
- 未配置任何 `API_KEYS` 且管理端系统设置中没有 API Key 时，默认外部接口保持无密钥模式，便于本地调试；服务器部署建议设置 `REQUIRE_API_KEY=true`。开启后如果还没有任何 API Key，外部接口会返回 401，管理员登录控制台后可在“系统设置”里生成第一个 API Key。

## OpenAI 兼容接口

以下示例按服务器部署推荐配置编写，统一带 `Authorization: Bearer sk-your-external-key`。如果本地调试未开启 `REQUIRE_API_KEY` 且没有配置任何 API Key，可以临时省略该请求头。

外部 OpenAI 兼容客户端的 `base_url` 推荐填写 `http://host:7860/v1`。如果客户端误填成根地址，本服务也提供只读兼容入口 `GET/HEAD /models` 和 `GET/HEAD /models/{model_id}`，但聊天、图片、文件等业务接口仍使用 `/v1/...` 路径。旧版 SDK 探测用的 `GET/HEAD /engines`、`/engines/{model_id}`、`/v1/engines` 和 `/v1/engines/{model_id}` 也可用，只作为当前真实模型列表的只读别名，不恢复旧模型名称映射。

所有外部接口都会返回 `X-Request-ID`。调用方可以主动传入同名请求头；服务端会把它写入响应头、请求日志和媒体结果索引，便于按同一个 id 排查聊天、图片、音频和原生 Gemini 调用。OpenAI 兼容错误响应也会在 JSON 正文的顶层 `request_id` 和 `error.request_id` 返回同一个值；流式失败时，SSE `event: error` 或 Responses API 的 `response.failed.error` 也会带上该值，方便只记录响应体或事件流的客户端排障。

列出模型：

```sh
curl http://localhost:7860/v1/models \
  -H "Authorization: Bearer sk-your-external-key"
```

查询单个模型：

```sh
curl http://localhost:7860/v1/models/gemini-3.1-pro \
  -H "Authorization: Bearer sk-your-external-key"
```

文本补全：

```sh
curl http://localhost:7860/v1/completions \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "prompt": "只回复 OK",
    "max_tokens": 32
  }'
```

`/v1/completions` 用于兼容旧 OpenAI 文本补全客户端，支持 `prompt`、`stream`、`stop`、`max_tokens`、`temperature`、`top_p`、`presence_penalty`、`frequency_penalty`、`seed` 和 `stream_options.include_usage`。多候选 `n>1` 暂不支持。

聊天补全：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "messages": [
      { "role": "user", "content": "只回复 OK" }
    ]
  }'
```

流式调用：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "stream": true,
    "messages": [
      { "role": "user", "content": "按行输出 1 和 2" }
    ]
  }'
```

JSON 模式：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "stop": "<END>",
    "max_completion_tokens": 256,
    "response_format": { "type": "json_object" },
    "messages": [
      { "role": "user", "content": "只返回 JSON：{\"ok\": true}" }
    ]
  }'
```

`response_format` 支持 OpenAI 常用的 `json_object` 和 `json_schema`。服务端会把 JSON 输出要求追加到 Gemini 提示词中，模型最终输出仍以原始文本返回给 OpenAI 兼容客户端。

Chat Completions 中的 `system` 和新式 `developer` 角色都会作为 Gemini 的系统指令写入提示词；`tool` 角色会作为工具结果写入上下文。`stop` 支持字符串或字符串数组，服务端会在返回给客户端前按最早匹配位置截断文本。`max_tokens` 和 `max_completion_tokens` 会作为长度约束提示传给 Gemini。`temperature`、`top_p`、`presence_penalty`、`frequency_penalty` 和 `seed` 会被显式接收，并作为调用方生成偏好追加到提示词中；Gemini Web 没有稳定公开的原生采样参数入口，因此这些参数不是底层强制采样配置。流式调用支持 `stream_options.include_usage=true`，结束前会额外返回一个 OpenAI 风格的 usage chunk；由于 Gemini Web 无稳定 token 计数，当前 usage 字段为 0 占位。

为兼容常见 OpenAI SDK 的追踪字段，`/v1/chat/completions`、`/v1/completions` 和 `/v1/responses` 会显式接收 `user`、`metadata` 和 `store`。这些字段仅作为请求兼容字段保留，不会触发 OpenAI 平台式训练、存储或审计语义；排障建议仍优先使用响应头 `X-Request-ID`。

工具调用支持新版 `tools` / `tool_choice`，也兼容旧版 `functions` / `function_call` 入参；模型需要调用工具时会返回 OpenAI Chat Completions 格式的 `tool_calls`。当请求使用旧版 `functions` 时，非流式响应会额外带上旧版 `message.function_call` 字段，方便旧客户端读取。

`/v1/responses` 也支持新版 `tools` / `tool_choice` / `parallel_tool_calls` 入参；如果触发工具，会在 `output` 中返回 `function_call` 项，包含 `call_id`、`name` 和 JSON 字符串形式的 `arguments`，方便外部 Responses API 客户端继续执行工具。流式模式下，工具 JSON 会先在服务端缓冲解析，结束时以 `response.output_item.added` / `response.output_item.done` 的 `function_call` 事件输出，避免客户端把工具调用误展示成普通文本。

外部客户端执行工具后，可以把 Responses API 的 `function_call_output` 放回 `input` 数组；服务端会把它转换为 Gemini 可见的工具结果上下文，继续完成后续回答。

外部客户端也可以把上一轮 Responses 的 `output_text` 内容块放回 `input`；服务端会保留为 assistant 上下文，不会静默丢弃。

多模态消息中的 `image_url` 会被保留为图片链接提示，适合外部 OpenAI 兼容客户端传入图片 URL：

```sh
curl http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
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

说明：OpenAI 兼容接口会把图片 URL、音频 URL 转成 Gemini 可读的文本引用，不会在服务端下载或转存远程 URL；需要上传附件时可以使用 OpenAI 兼容 `/v1/files`，返回的 `file-...` 也可继续用于 Gemini 原生接口。

OpenAI 兼容文件接口：

```sh
curl http://localhost:7860/v1/files \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "purpose=assistants" \
  -F "file=@./sample.pdf"
```

```sh
curl http://localhost:7860/v1/files \
  -H "Authorization: Bearer sk-your-external-key"
curl http://localhost:7860/v1/files/file-xxxx \
  -H "Authorization: Bearer sk-your-external-key"
curl http://localhost:7860/v1/files/file-xxxx/content \
  -H "Authorization: Bearer sk-your-external-key"
curl -X DELETE http://localhost:7860/v1/files/file-xxxx \
  -H "Authorization: Bearer sk-your-external-key"
```

`/v1/files`、`/v1/files/{file_id}`、`/v1/files/{file_id}/content` 和 `DELETE /v1/files/{file_id}` 使用 OpenAI 常见的文件对象结构；文件内容保存在本地 `data/uploads/`。为兼容外部 SDK 和网关的探测请求，`/v1/files`、`/v1/files/{file_id}`、`/v1/files/{file_id}/content` 以及 Gemini 原生 `/v1/gemini/files` 均支持 `HEAD`，只返回状态和头部，不返回正文。Chat Completions 和 Responses 可以在消息内容里通过 `input_file` 或 `input_audio.file_id` 引用 `file-...`，服务端会把对应本地文件传给 Gemini。

```json
{
  "role": "user",
  "content": [
    { "type": "input_text", "text": "总结这个文件" },
    { "type": "input_file", "file_id": "file-xxxx" }
  ]
}
```

音频可以引用已上传文件或远程 URL：

```json
{
  "role": "user",
  "content": [
    { "type": "input_text", "text": "转写并总结这段音频" },
    { "type": "input_audio", "input_audio": { "file_id": "file-audio-xxxx" } },
    { "type": "input_audio", "input_audio": { "url": "https://example.com/audio.mp3" } }
  ]
}
```

Responses API：

```sh
curl http://localhost:7860/v1/responses \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "instructions": "保持简洁，只输出 JSON",
    "text": { "format": { "type": "json_object" } },
    "input": "只回复 OK"
  }'
```

`/v1/responses` 支持 `input` 字符串或 Responses 风格消息数组，并返回 `output_text`；也支持常见的 `instructions`、`text.format`、`max_output_tokens`、`temperature`、`top_p`、`presence_penalty`、`frequency_penalty` 和 `seed`，其中 `text.format` 可使用 `json_object` 或 `json_schema`。为兼容部分旧客户端，`max_tokens` 也会作为 `max_output_tokens` 的别名处理。设置 `stream=true` 时会返回 Responses 风格 SSE 事件，包括 `response.output_text.delta` 和 `response.completed`：

```sh
curl http://localhost:7860/v1/responses \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "stream": true,
    "input": "按行输出 1 和 2"
  }'
```

OpenAI 兼容音频转写和翻译：

```sh
curl http://localhost:7860/v1/audio/transcriptions \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "model=gemini-3.5-flash" \
  -F "file=@./voice.mp3" \
  -F "language=zh" \
  -F "response_format=json"
```

```sh
curl http://localhost:7860/v1/audio/translations \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "model=gemini-3.1-pro" \
  -F "file=@./voice.mp3" \
  -F "response_format=text"
```

`/v1/audio/transcriptions` 和 `/v1/audio/translations` 会把上传音频作为一次性 Gemini 输入文件，不写入长期文件库；支持 `json`、`text`、`verbose_json`、`srt`、`vtt` 五种 `response_format`。翻译接口会要求 Gemini 输出英文翻译文本。由于 Gemini Web 没有 Whisper 风格的分段和时长元数据，`verbose_json` 中的 `segments` 暂为空数组，`srt`/`vtt` 会把整段文本放入零时间戳字幕块。

OpenAI 兼容图片生成：

```sh
curl http://localhost:7860/v1/images/generations \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "prompt": "生成一张赛博朋克风格的猫"
  }'
```

`/v1/images/generations` 会调用 Gemini 图片生成模式，并返回本服务的媒体代理链接；支持 `response_format=url` 和 `response_format=b64_json`。URL 返回值是随机 token 的内容下载地址，外部客户端展示图片时无需再次携带 API Key；`b64_json` 会在生成后读取图片内容并返回 base64。当前每次请求只支持生成 1 个结果，`n>1` 会返回 400，避免客户端误以为已经生成多张图片。

部分 OpenAI 图片客户端会固定传 `gpt-image-1`、`gpt-image-2`、`dall-e-2` 或 `dall-e-3`。这些名字只在 `/v1/images/*` 图片端点内作为兼容别名接受，底层仍会使用当前真实 Gemini 模型，不会出现在 `/v1/models` 列表里，也不会恢复旧模型映射。

OpenAI 兼容图片编辑：

```sh
curl http://localhost:7860/v1/images/edits \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "model=gemini-3.1-pro" \
  -F "prompt=把图片调整成电影海报风格" \
  -F "image=@./source.png"
```

`/v1/images/edits` 会把上传的 `image` 和可选 `mask` 临时传给 Gemini 图片模式，返回格式与 `/v1/images/generations` 一致；当前同样只支持 URL 返回。

OpenAI 兼容图片变体：

```sh
curl http://localhost:7860/v1/images/variations \
  -H "Authorization: Bearer sk-your-external-key" \
  -F "model=gemini-3.1-pro" \
  -F "image=@./source.png"
```

`/v1/images/variations` 会把上传图片作为 Gemini 图片模式输入，并使用默认变体提示生成新图，返回格式与 `/v1/images/generations` 一致。

常用模型：

- `gemini-3.1-pro`：真实模型名，原样传给 Gemini。
- `gemini-3.5-flash`：快速模型。
- `gemini-3.1-flash-lite`：轻量快速模型。

旧模型名和未知模型名会被拒绝，例如 `gemini-3-pro`、`gemini-3-flash`、`gemini-3-flash-thinking` 不再透传到底层。

## Gemini 原生接口

原生生成：

```sh
curl http://localhost:7860/v1/gemini/generate \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
    "prompt": "生成一段简短介绍"
  }'
```

指定媒体生成模式：

```sh
curl http://localhost:7860/v1/gemini/generate \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
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
  "model": "gemini-3.1-pro",
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

兼容旧脚本的 `/v1/generate` 也支持 `mode=image|video|audio`；指定媒体模式时同样会保存媒体结果索引，并在响应和请求日志中回填 `media_count`。

`/v1/gemini/media` 媒体历史列表仍受 API Key 或管理员会话保护；只有 `/v1/gemini/media/{media_token}/content` 内容下载链接会按随机 token 公开访问，便于外部 OpenAI 兼容客户端直接预览图片或视频。该内容链接支持 `GET` 下载和 `HEAD` 探测媒体类型。

查看媒体历史：

```sh
curl "http://localhost:7860/v1/gemini/media?limit=20" \
  -H "Authorization: Bearer sk-your-external-key"
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
- 确认额度已恢复时，可在管理端看板媒体冷却卡片上按类型清除，或使用管理员登录态调用 `POST /v1/media-cooldowns/clear`。清理冷却会改变额度保护状态，不允许仅凭外部 API Key 操作。

媒体冷却汇总示例：

```sh
curl http://localhost:7860/v1/media-cooldowns \
  -H "Authorization: Bearer sk-your-external-key"
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
  -b "gemini_admin_session=<admin-session-cookie>" \
  -d '{"kind":"video"}'
```

`kind` 可选 `image`、`video`、`audio`；不传 `kind` 时会清除全部媒体冷却。该接口属于管理操作，外部 API Key 只能读取 `GET /v1/media-cooldowns`，不能清除冷却。

原生流式接口：

```sh
curl http://localhost:7860/v1/gemini/stream \
  -H "Authorization: Bearer sk-your-external-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-pro",
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
- `GET/HEAD /v1/gemini/media/{media_token}/content`
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
- `GET/HEAD /healthz`
- `GET/HEAD /readyz`
- `GET/HEAD /livez`
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

除 `GET /health`、`/healthz`、`/readyz`、`/livez` 和只读的 `GET /v1/media-cooldowns` 外，上述接口属于管理端调用面。服务器部署并设置 `ADMIN_PASSWORD` 后，账号、授权、设置、日志和冷却清理等管理操作只接受管理员网页登录态，不接受外部 `API_KEYS` 直接调用；`API_KEYS` 只用于 OpenAI 兼容接口、Gemini 原生生成/媒体/文件等外部调用面，以及读取媒体冷却摘要。这样可以避免外部客户端拿到模型调用 Key 后顺手修改账号池、授权浏览器或系统设置。

账号列表和当前账号状态通过 `GET /v1/status` 返回。

`GET /health` 不需要管理员登录或 API Key，适合 Docker、反向代理和监控系统探活。它只返回非敏感摘要，包括服务版本、公开模型列表、账号总数/可用数、当前账号 id、是否启用管理员登录以及外部 API Key 是否必需，不会返回 Cookie 或 API Key 明文。`/healthz`、`/readyz`、`/livez` 是同样内容的兼容别名，并支持 `HEAD` 探测。

外部客户端或反向代理也可以使用 `HEAD /v1`、`HEAD /v1/models`、`HEAD /v1/models/{model_id}` 做轻量连通性探测；开启 API Key 后同样需要携带 `Authorization: Bearer <API_KEY>`。

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
- 服务器或公网部署必须修改 Docker Compose 默认的 `ADMIN_PASSWORD` 和 `ADMIN_SESSION_SECRET`，并建议设置 `ADMIN_USERNAME`；`/health` 会在未设置密码或仍使用占位值时返回告警。
- 服务器或公网部署必须启用外部 API Key 鉴权，建议保持 `REQUIRE_API_KEY=true`，并通过 `API_KEYS` 或管理端“系统设置”生成外部调用密钥。
- HTTPS 反向代理部署时建议设置 `ADMIN_COOKIE_SECURE=true`；若服务器访问 Gemini 需要代理，可以在 `.env` 中配置 `GEMINI_PROXY`。
- 建议只在可信网络中暴露管理端，公网部署请收紧 `CORS_ALLOW_ORIGINS`，并按需再加反向代理鉴权。

## 上游项目

本项目基于 [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) 改造，保留原 SDK 能力，并新增 Docker 服务、多账号轮换和 Web 管理端。

## License

遵循原项目许可证。详见 [LICENSE](LICENSE)。
