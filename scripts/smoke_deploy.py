from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, dict, dict[str, str]]:
    status, body_text, response_headers = _raw_request(
        base_url,
        path,
        timeout=timeout,
        api_key=api_key,
        headers=headers,
        method=method,
        body=body,
    )
    try:
        data = json.loads(body_text) if body_text else {}
    except json.JSONDecodeError:
        data = {}
    return status, data, response_headers


def _raw_request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, str, dict[str, str]]:
    url = f"{base_url.rstrip('/')}{path}"
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if api_key:
        request_headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        status = exc.code
        response_headers = dict(exc.headers.items())
    return status, response_body.decode("utf-8", errors="replace"), response_headers


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _cookie_header(headers: dict[str, str]) -> str:
    raw_cookie = ""
    for key, value in headers.items():
        if key.lower() == "set-cookie":
            raw_cookie = value
            break
    if not raw_cookie:
        return ""
    parts: list[str] = []
    for item in raw_cookie.split(","):
        cookie_pair = item.split(";", 1)[0].strip()
        if "=" in cookie_pair:
            parts.append(cookie_pair)
    return "; ".join(parts)


def _sse_data_items(body_text: str) -> list[str]:
    items: list[str] = []
    for line in body_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        items.append(stripped.removeprefix("data:").strip())
    return items


def _sse_event_names(body_text: str) -> list[str]:
    events: list[str] = []
    for line in body_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("event:"):
            continue
        events.append(stripped.removeprefix("event:").strip())
    return events


def run_smoke(
    base_url: str,
    api_key: str | None,
    *,
    admin_username: str | None = None,
    admin_password: str | None = None,
    chat_prompt: str | None = None,
    chat_model: str = "gemini",
    chat_stream: bool = False,
    image_prompt: str | None = None,
    image_model: str = "gemini",
    image_response_format: str = "url",
    responses_prompt: str | None = None,
    responses_model: str = "gemini",
    responses_stream: bool = False,
    completion_prompt: str | None = None,
    completion_model: str = "gemini",
    timeout: float = 120.0,
    fail_on_warnings: bool = False,
) -> list[str]:
    results: list[str] = []

    health_status, health, _ = _request(base_url, "/health", timeout=timeout)
    _require(health_status == 200, f"/health returned {health_status}")
    _require(health.get("ok") is True, "/health did not return ok=true")
    _require("models" in health, "/health missing models")
    results.append("health ok")
    warnings = [
        str(item)
        for item in health.get("warnings", [])
        if str(item).strip()
    ]
    for warning in warnings:
        results.append(f"health warning: {warning}")
    if fail_on_warnings and warnings:
        # 正式部署前可开启严格模式，把默认密码、默认会话密钥等安全提示直接视为失败。
        raise AssertionError(f"/health returned {len(warnings)} warning(s)")

    unauth_status, unauth, unauth_headers = _request(
        base_url,
        "/v1/models",
        timeout=timeout,
    )
    if health.get("auth", {}).get("api_key_required"):
        _require(unauth_status == 401, "/v1/models should require an API key")
        _require("x-request-id" in {key.lower(): value for key, value in unauth_headers.items()}, "401 response missing X-Request-ID")
        _require("error" in unauth, "401 response missing OpenAI error body")
        results.append("api key protection ok")
    else:
        _require(unauth_status == 200, f"/v1/models returned {unauth_status}")
        results.append("open local api ok")

    if api_key:
        models_status, models, headers = _request(
            base_url,
            "/v1/models",
            timeout=timeout,
            api_key=api_key,
        )
        _require(models_status == 200, f"/v1/models with API key returned {models_status}")
        _require(models.get("object") == "list", "/v1/models did not return an OpenAI list")
        _require(any(item.get("id") == "gemini" for item in models.get("data", [])), "/v1/models missing gemini")
        _require("x-request-id" in {key.lower(): value for key, value in headers.items()}, "authorized response missing X-Request-ID")
        results.append("authorized models ok")

    media_status, media, _ = _request(
        base_url,
        "/v1/media-cooldowns",
        timeout=timeout,
        api_key=api_key,
    )
    if health.get("auth", {}).get("api_key_required") and not api_key:
        _require(media_status == 401, "/v1/media-cooldowns should require an API key")
        results.append("media cooldown protection ok")
    else:
        _require(media_status == 200, f"/v1/media-cooldowns returned {media_status}")
        _require(media.get("ok") is True, "/v1/media-cooldowns missing ok=true")
        results.append("media cooldown summary ok")

    if admin_password:
        admin_status, admin, _ = _request(
            base_url,
            "/v1/admin/status",
            timeout=timeout,
        )
        _require(admin_status == 200, f"/v1/admin/status returned {admin_status}")
        _require(admin.get("enabled") is True, "admin login is not enabled")
        payload = {"password": admin_password}
        if admin.get("username_required"):
            _require(
                bool(admin_username),
                "admin username is required; pass --admin-username",
            )
            payload["username"] = admin_username
        login_status, login, login_headers = _request(
            base_url,
            "/v1/admin/login",
            timeout=timeout,
            method="POST",
            body=payload,
        )
        _require(login_status == 200, f"/v1/admin/login returned {login_status}")
        _require(login.get("authenticated") is True, "admin login did not authenticate")
        cookie = _cookie_header(login_headers)
        _require("gemini_admin_session=" in cookie, "admin login missing session cookie")
        logs_status, logs, _ = _request(
            base_url,
            "/v1/request-logs",
            timeout=timeout,
            headers={"Cookie": cookie},
        )
        _require(logs_status == 200, f"/v1/request-logs with admin cookie returned {logs_status}")
        _require("logs" in logs, "/v1/request-logs missing logs")
        results.append("admin login ok")

    if chat_prompt:
        # 真实模型调用会消耗账号请求次数，因此只在显式传入 --chat-prompt 时执行。
        chat_status, chat, chat_headers = _request(
            base_url,
            "/v1/chat/completions",
            timeout=timeout,
            api_key=api_key,
            method="POST",
            body={
                "model": chat_model,
                "messages": [{"role": "user", "content": chat_prompt}],
            },
        )
        _require(chat_status == 200, f"/v1/chat/completions returned {chat_status}")
        _require(chat.get("object") == "chat.completion", "chat response is not an OpenAI chat completion")
        _require("x-request-id" in {key.lower(): value for key, value in chat_headers.items()}, "chat response missing X-Request-ID")
        choices = chat.get("choices") or []
        _require(bool(choices), "chat response missing choices")
        message = choices[0].get("message") or {}
        _require(
            bool(message.get("content") or message.get("tool_calls")),
            "chat response missing message content or tool_calls",
        )
        results.append("chat completions ok")

        if chat_stream:
            # 流式接口是很多 OpenAI 兼容客户端的默认路径，这里校验 SSE chunk 和结束标记。
            stream_status, stream_body, stream_headers = _raw_request(
                base_url,
                "/v1/chat/completions",
                timeout=timeout,
                api_key=api_key,
                method="POST",
                body={
                    "model": chat_model,
                    "stream": True,
                    "messages": [{"role": "user", "content": chat_prompt}],
                },
            )
            _require(stream_status == 200, f"stream /v1/chat/completions returned {stream_status}")
            content_type = next(
                (value for key, value in stream_headers.items() if key.lower() == "content-type"),
                "",
            )
            _require("text/event-stream" in content_type.lower(), "stream response is not text/event-stream")
            _require("x-request-id" in {key.lower(): value for key, value in stream_headers.items()}, "stream response missing X-Request-ID")
            data_items = _sse_data_items(stream_body)
            _require("[DONE]" in data_items, "stream response missing [DONE]")
            chunks = [item for item in data_items if item != "[DONE]"]
            _require(bool(chunks), "stream response missing data chunks")
            first_chunk = json.loads(chunks[0])
            _require(first_chunk.get("object") == "chat.completion.chunk", "stream chunk is not an OpenAI chat completion chunk")
            _require("choices" in first_chunk, "stream chunk missing choices")
            results.append("chat stream ok")

    if image_prompt:
        # 图片生成会消耗媒体生成次数，因此只在显式传入 --image-prompt 时执行。
        image_status, image, image_headers = _request(
            base_url,
            "/v1/images/generations",
            timeout=timeout,
            api_key=api_key,
            method="POST",
            body={
                "model": image_model,
                "prompt": image_prompt,
                "response_format": image_response_format,
            },
        )
        _require(image_status == 200, f"/v1/images/generations returned {image_status}")
        _require("x-request-id" in {key.lower(): value for key, value in image_headers.items()}, "image response missing X-Request-ID")
        data = image.get("data") or []
        _require(bool(data), "image response missing data")
        first_item = data[0]
        if image_response_format == "b64_json":
            _require(bool(first_item.get("b64_json")), "image response missing b64_json")
        else:
            url = str(first_item.get("url") or "")
            _require(url.startswith(("http://", "https://")), "image response missing absolute url")
        results.append("image generation ok")

    if responses_prompt:
        # Responses API 是新版 OpenAI SDK 的常用入口；显式传参时验证它的基础返回结构。
        responses_status, responses, responses_headers = _request(
            base_url,
            "/v1/responses",
            timeout=timeout,
            api_key=api_key,
            method="POST",
            body={
                "model": responses_model,
                "input": responses_prompt,
            },
        )
        _require(responses_status == 200, f"/v1/responses returned {responses_status}")
        _require("x-request-id" in {key.lower(): value for key, value in responses_headers.items()}, "responses response missing X-Request-ID")
        _require(responses.get("object") == "response", "responses body is not an OpenAI response object")
        output = responses.get("output") or []
        _require(bool(output), "responses body missing output")
        _require(
            bool(responses.get("output_text") or any(item.get("type") == "function_call" for item in output if isinstance(item, dict))),
            "responses body missing output_text or function_call",
        )
        results.append("responses api ok")

        if responses_stream:
            stream_status, stream_body, stream_headers = _raw_request(
                base_url,
                "/v1/responses",
                timeout=timeout,
                api_key=api_key,
                method="POST",
                body={
                    "model": responses_model,
                    "input": responses_prompt,
                    "stream": True,
                },
            )
            _require(stream_status == 200, f"stream /v1/responses returned {stream_status}")
            content_type = next(
                (value for key, value in stream_headers.items() if key.lower() == "content-type"),
                "",
            )
            _require("text/event-stream" in content_type.lower(), "responses stream is not text/event-stream")
            _require("x-request-id" in {key.lower(): value for key, value in stream_headers.items()}, "responses stream missing X-Request-ID")
            events = _sse_event_names(stream_body)
            data_items = _sse_data_items(stream_body)
            _require("response.completed" in events, "responses stream missing response.completed event")
            _require("[DONE]" in data_items, "responses stream missing [DONE]")
            results.append("responses stream ok")

    if completion_prompt:
        # 旧版 OpenAI 兼容客户端仍可能调用 /v1/completions，这里显式验证文本补全结构。
        completion_status, completion, completion_headers = _request(
            base_url,
            "/v1/completions",
            timeout=timeout,
            api_key=api_key,
            method="POST",
            body={
                "model": completion_model,
                "prompt": completion_prompt,
            },
        )
        _require(completion_status == 200, f"/v1/completions returned {completion_status}")
        _require("x-request-id" in {key.lower(): value for key, value in completion_headers.items()}, "completion response missing X-Request-ID")
        _require(completion.get("object") == "text_completion", "completion body is not an OpenAI text completion")
        choices = completion.get("choices") or []
        _require(bool(choices), "completion body missing choices")
        _require(bool(choices[0].get("text")), "completion body missing choice text")
        results.append("completions api ok")

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deployed Gemini API Web service.")
    parser.add_argument("--base-url", default="http://localhost:7860")
    parser.add_argument("--api-key", default="")
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds for smoke HTTP calls.",
    )
    parser.add_argument("--admin-username", default="")
    parser.add_argument("--admin-password", default="")
    parser.add_argument(
        "--chat-prompt",
        default="",
        help="Optional prompt for a real /v1/chat/completions smoke request.",
    )
    parser.add_argument("--chat-model", default="gemini")
    parser.add_argument(
        "--chat-stream",
        action="store_true",
        help="Also verify streaming /v1/chat/completions when --chat-prompt is set.",
    )
    parser.add_argument(
        "--image-prompt",
        default="",
        help="Optional prompt for a real /v1/images/generations smoke request.",
    )
    parser.add_argument("--image-model", default="gemini")
    parser.add_argument(
        "--image-response-format",
        default="url",
        choices=("url", "b64_json"),
    )
    parser.add_argument(
        "--responses-prompt",
        default="",
        help="Optional prompt for a real /v1/responses smoke request.",
    )
    parser.add_argument("--responses-model", default="gemini")
    parser.add_argument(
        "--responses-stream",
        action="store_true",
        help="Also verify streaming /v1/responses when --responses-prompt is set.",
    )
    parser.add_argument(
        "--completion-prompt",
        default="",
        help="Optional prompt for a real /v1/completions smoke request.",
    )
    parser.add_argument("--completion-model", default="gemini")
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Fail when /health reports deployment warnings.",
    )
    args = parser.parse_args()
    try:
        results = run_smoke(
            args.base_url,
            args.api_key or None,
            admin_username=args.admin_username or None,
            admin_password=args.admin_password or None,
            chat_prompt=args.chat_prompt or None,
            chat_model=args.chat_model,
            chat_stream=args.chat_stream,
            image_prompt=args.image_prompt or None,
            image_model=args.image_model,
            image_response_format=args.image_response_format,
            responses_prompt=args.responses_prompt or None,
            responses_model=args.responses_model,
            responses_stream=args.responses_stream,
            completion_prompt=args.completion_prompt or None,
            completion_model=args.completion_model,
            timeout=max(1.0, args.timeout),
            fail_on_warnings=args.fail_on_warnings,
        )
    except Exception as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    for item in results:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
