# Gemini API Web

基于 `gemini_webapi` 的 Docker 化 Gemini Web API 服务，提供 OpenAI 兼容接口、Gemini 原生能力接口、多账号 Cookie 池、自动轮换、网页登录授权和网页管理端。

本项目适合长期运行在服务器或 NAS 上，用多个 Gemini 账号 Cookie 分担请求，并通过管理端查看调用情况、调整轮换策略、管理 Gems 和 Deep Research 任务。

## 功能

- Docker 一键部署，数据持久化到 SQLite。
- 多账号 Cookie 池，支持导入、手动添加、网页登录授权保存。
- 支持按调用次数轮换、按错误次数轮换、手动切换账号。
- OpenAI 兼容接口：`/v1/chat/completions`、`/v1/models`。
- Gemini 原生接口：生成、流式生成、Gems、Deep Research、文件上传、媒体结果索引。
- 管理端看板：账户状态、请求统计、失败率、请求日志、媒体结果、任务状态。
- 媒体结果默认只保存 URL 和元信息，不自动下载文件，避免磁盘持续膨胀。

## 快速开始

克隆项目后启动：

```sh
docker compose up -d --build
```

访问管理端：

```text
http://localhost:7860
```

授权浏览器 noVNC：

```text
http://localhost:6080/vnc.html
```

默认数据目录是本机 `./data`，容器内映射为 `/app/data`。SQLite 数据库默认保存到：

```text
data/app.db
```

真实数据库、Cookie 文件和本地状态不会提交到 Git。

## 添加账号

推荐使用管理端的“网页授权”：

1. 打开 `http://localhost:7860`
2. 进入“账户设置”
3. 点击“网页授权”
4. 在弹出的 noVNC 浏览器中登录 Google/Gemini
5. 回到管理端点击“保存当前登录”

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
```

含义：

- `SWITCH_ON_USES`：单个账号调用多少次后切换到下一个账号。
- `FAILURE_THRESHOLD`：单个账号连续失败多少次后切换。
- `IMMEDIATE_SWITCH_STATUS_CODES`：遇到这些 HTTP 状态码时立即切换。
- `REQUEST_TIMEOUT`：请求超时时间，单位秒。
- `GEMINI_AUTO_REFRESH`：是否启用 Cookie 自动刷新。
- `GEMINI_AUTH_HEADLESS`：授权浏览器是否无头运行。需要 noVNC 登录时保持 `false`。

## OpenAI 兼容接口

列出模型：

```sh
curl http://localhost:7860/v1/models
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

常用模型别名：

- `gemini`：默认映射到 Gemini 3.1 Pro。
- `gemini-3.1-pro`：Pro。
- `gemini-3-flash`：快速。
- `gemini-3-flash-thinking`：思考。

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
- `GET /v1/gemini/jobs`

## 管理接口

常用状态和管理接口：

- `GET /health`
- `GET /v1/status`
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
- `POST /v1/auth/session`
- `POST /v1/auth/save`

## 持久化数据

SQLite 表包括：

- `accounts`：账号 Cookie 和启用状态。
- `runtime_state`：当前账号、轮换策略。
- `request_logs`：请求日志。
- `jobs`：Deep Research 和长任务状态。
- `media_outputs`：图片、视频、音频等媒体结果索引。
- `gems_cache`：Gems 缓存。
- `gemini_files`：上传文件记录。

默认只需要备份 `data/app.db`。如果使用 `data/accounts.json` 导入账号，也请自行安全保存。

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
