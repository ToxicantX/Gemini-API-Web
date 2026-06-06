from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse


_ACTIVE_API_KEY_HEADER = "authorization"
_SMOKE_USER_AGENT = "Gemini-API-Web-Smoke/1.0"
_CORS_REQUEST_HEADERS = (
    "authorization",
    "x-api-key",
    "api-key",
    "openai-api-key",
    "content-type",
)


def _normalize_api_key_header(value: str | None) -> str:
    """规范化 smoke 请求使用的 API Key 头名称。"""
    normalized = (value or "authorization").strip().lower()
    aliases = {
        "authorization": "authorization",
        "bearer": "authorization",
        "x-api-key": "x-api-key",
        "api-key": "api-key",
        "openai-api-key": "openai-api-key",
    }
    if normalized not in aliases:
        raise AssertionError(
            "--api-key-header must be one of: authorization, x-api-key, api-key, openai-api-key"
        )
    return aliases[normalized]


def _api_key_auth_header(api_key: str, api_key_header: str | None = None) -> dict[str, str]:
    """按外部客户端常用写法生成 API Key 请求头。"""
    header = _normalize_api_key_header(api_key_header or _ACTIVE_API_KEY_HEADER)
    if header == "authorization":
        return {"Authorization": f"Bearer {api_key}"}
    names = {
        "x-api-key": "X-API-Key",
        "api-key": "API-Key",
        "openai-api-key": "OpenAI-API-Key",
    }
    return {names[header]: api_key}


def _request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    api_key: str | None = None,
    api_key_header: str | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, dict, dict[str, str]]:
    status, body_text, response_headers = _raw_request(
        base_url,
        path,
        timeout=timeout,
        api_key=api_key,
        api_key_header=api_key_header,
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
    api_key_header: str | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, str, dict[str, str]]:
    url = f"{base_url.rstrip('/')}{path}"
    request_headers = {
        "Accept": "application/json",
        # 部分边缘安全规则会拦截 Python urllib 默认 UA，显式设置便于部署冒烟稳定通过。
        "User-Agent": _SMOKE_USER_AGENT,
    }
    if headers:
        request_headers.update(headers)
    if api_key:
        request_headers.update(_api_key_auth_header(api_key, api_key_header))
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


def _multipart_request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    file_path: str | None = None,
    file_field: str = "file",
    files: list[tuple[str, str]] | None = None,
    fields: dict[str, str] | None = None,
    api_key: str | None = None,
    api_key_header: str | None = None,
) -> tuple[int, dict, dict[str, str]]:
    """发送 multipart/form-data 请求，用于验证外部文件上传类接口。"""
    status, body_text, response_headers = _raw_multipart_request(
        base_url,
        path,
        timeout=timeout,
        file_path=file_path,
        file_field=file_field,
        files=files,
        fields=fields,
        api_key=api_key,
        api_key_header=api_key_header,
    )
    try:
        data = json.loads(body_text) if body_text else {}
    except json.JSONDecodeError:
        data = {}
    return status, data, response_headers


def _raw_multipart_request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    file_path: str | None = None,
    file_field: str = "file",
    files: list[tuple[str, str]] | None = None,
    fields: dict[str, str] | None = None,
    api_key: str | None = None,
    api_key_header: str | None = None,
) -> tuple[int, str, dict[str, str]]:
    """发送 multipart/form-data 请求并保留原始正文，用于 text/srt/vtt 等非 JSON 响应。"""
    upload_files = list(files or [])
    if file_path is not None:
        upload_files.append((file_field, file_path))
    _require(bool(upload_files), "multipart request requires at least one file")
    boundary = f"----gemini-smoke-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in (fields or {}).items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for field_name, upload_path in upload_files:
        source = Path(upload_path)
        _require(source.is_file(), f"upload file does not exist: {upload_path}")
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{source.name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                source.read_bytes(),
                b"\r\n",
            ]
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        # 部分边缘安全规则会拦截 Python urllib 默认 UA，显式设置便于部署冒烟稳定通过。
        "User-Agent": _SMOKE_USER_AGENT,
    }
    if api_key:
        headers.update(_api_key_auth_header(api_key, api_key_header))
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=b"".join(parts),
        headers=headers,
        method="POST",
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


def _media_content_path(base_url: str, content_url: str) -> str:
    """把媒体 content_url 归一成当前服务内的路径，避免误探测外部域名。"""
    value = str(content_url or "").strip()
    _require(bool(value), "media content_url is empty")
    if value.startswith("/"):
        return value
    parsed = urlparse(value)
    _require(bool(parsed.scheme and parsed.netloc), f"media content_url is not a URL: {content_url}")
    base = urlparse(base_url.rstrip("/"))
    _require(
        parsed.netloc == base.netloc,
        f"media content_url points to another host: {content_url}",
    )
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _require_openai_error(
    status: int,
    data: dict,
    headers: dict[str, str],
    *,
    expected_status: int,
    expected_type: str,
    label: str,
) -> None:
    """校验外部错误响应保持 OpenAI 兼容格式，便于 SDK 和网关统一处理。"""
    lower_headers = {key.lower(): value for key, value in headers.items()}
    request_id = lower_headers.get("x-request-id", "")
    _require(status == expected_status, f"{label} returned {status}")
    _require(bool(request_id), f"{label} response missing X-Request-ID")
    error = data.get("error") or {}
    _require(isinstance(error, dict), f"{label} response missing OpenAI error body")
    _require(error.get("type") == expected_type, f"{label} returned unexpected error type")
    _require(error.get("code") == expected_status, f"{label} returned unexpected error code")
    _require(bool(error.get("message")), f"{label} response missing error message")
    _require(data.get("request_id") == request_id, f"{label} top request_id mismatch")
    _require(error.get("request_id") == request_id, f"{label} error request_id mismatch")


def _probe_unavailable_generation_endpoint(
    *,
    base_url: str,
    timeout: float,
    api_key: str | None,
    path: str,
    body: dict,
    label: str,
) -> None:
    """账号池为空时校验生成类入口都返回一致的服务不可用错误。"""
    status, data, headers = _request(
        base_url,
        path,
        timeout=timeout,
        api_key=api_key,
        method="POST",
        body=body,
    )
    _require_openai_error(
        status,
        data,
        headers,
        expected_status=503,
        expected_type="service_unavailable",
        label=label,
    )


def _require_head_response(
    status: int,
    body_text: str,
    headers: dict[str, str],
    *,
    expected_status: int = 200,
    label: str,
) -> None:
    """统一校验 HEAD 探测响应，避免网关返回正文或丢失请求号。"""
    _require(status == expected_status, f"{label} returned {status}")
    if expected_status < 400:
        _require(body_text == "", f"{label} should not return a body")
    _require(
        "x-request-id" in {key.lower(): value for key, value in headers.items()},
        f"{label} missing X-Request-ID",
    )


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


def _require_audio_response(body_text: str, response_format: str, label: str) -> None:
    """按 OpenAI 音频 response_format 校验 smoke 响应，覆盖 JSON 和纯文本类格式。"""
    fmt = response_format or "json"
    if fmt in {"json", "verbose_json"}:
        try:
            data = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{label} response is not JSON") from exc
        _require(bool(data.get("text")), f"{label} response missing text")
        if fmt == "verbose_json":
            _require(isinstance(data.get("segments"), list), f"{label} verbose_json missing segments")
        return
    if fmt == "srt":
        _require(" --> " in body_text, f"{label} srt response missing timestamp")
        _require(bool(body_text.strip()), f"{label} srt response is empty")
        return
    if fmt == "vtt":
        _require(body_text.startswith("WEBVTT"), f"{label} vtt response missing WEBVTT header")
        return
    _require(bool(body_text.strip()), f"{label} text response is empty")


def _require_media_cooldown_summary(data: dict) -> None:
    """校验媒体冷却摘要结构，避免外部调用方无法判断图片/视频/音频是否可用。"""
    _require(data.get("ok") is True, "/v1/media-cooldowns missing ok=true")
    summary = data.get("summary")
    _require(isinstance(summary, list), "/v1/media-cooldowns missing summary list")
    active_count = data.get("active_account_count")
    if active_count is not None:
        _require(
            isinstance(active_count, int) and active_count >= 0,
            "/v1/media-cooldowns returned invalid active_account_count",
        )
    for item in summary:
        _require(isinstance(item, dict), "/v1/media-cooldowns summary item is not an object")
        kind = item.get("kind")
        _require(kind in {"image", "video", "audio"}, "/v1/media-cooldowns summary item has invalid kind")
        for field in ("total", "blocked", "available"):
            value = item.get(field)
            _require(
                isinstance(value, int) and value >= 0,
                f"/v1/media-cooldowns summary item has invalid {field}",
            )
        _require(
            item["available"] <= item["total"],
            "/v1/media-cooldowns available count exceeds total",
        )
        _require(
            item["blocked"] <= item["total"],
            "/v1/media-cooldowns blocked count exceeds total",
        )


def _require_generation_readiness(data: dict) -> None:
    """校验生成就绪状态，方便外部监控区分服务异常和账号未授权。"""
    _require(data.get("ok") is True, "/v1/generation-readiness missing ok=true")
    _require(isinstance(data.get("ready"), bool), "/v1/generation-readiness missing ready boolean")
    reasons = data.get("reasons")
    _require(isinstance(reasons, list), "/v1/generation-readiness missing reasons list")
    accounts = data.get("accounts")
    _require(isinstance(accounts, dict), "/v1/generation-readiness missing accounts object")
    for field in ("total", "available", "disabled", "expired"):
        value = accounts.get(field)
        _require(
            isinstance(value, int) and value >= 0,
            f"/v1/generation-readiness accounts.{field} is invalid",
        )
    _require(
        accounts["available"] <= accounts["total"],
        "/v1/generation-readiness available accounts exceeds total",
    )
    media_cooldowns = data.get("media_cooldowns")
    _require(
        isinstance(media_cooldowns, list),
        "/v1/generation-readiness missing media_cooldowns list",
    )


def _require_admin_ui(body: str) -> None:
    """校验管理端首页包含关键诊断入口，避免线上部署后页面仍是旧版本。"""
    _require("<title>Gemini API 管理端</title>" in body, "admin UI title is missing")
    _require("metricReadiness" in body, "admin UI missing generation readiness metric")
    _require("readinessPanel" in body, "admin UI missing generation readiness panel")
    _require("外部调用未就绪" in body, "admin UI missing readiness diagnostic copy")
    _require("网页授权" in body, "admin UI missing web authorization action")
    _require("gemini-3.1-pro" in body, "admin UI missing gemini-3.1-pro model option")
    _require("gemini-3.5-flash" in body, "admin UI missing gemini-3.5-flash model option")
    _require(
        "gemini-3.1-flash-lite" in body,
        "admin UI missing gemini-3.1-flash-lite model option",
    )
    _require("gemini / 3.1 Pro" not in body, "admin UI still exposes the old gemini alias label")
    _require(
        "model: gemini 或" not in body,
        "admin UI still documents the old gemini alias label",
    )


def _require_media_content_headers(
    headers: dict[str, str],
    *,
    kind: str,
    label: str,
) -> None:
    """校验媒体代理 HEAD 响应头，确保外部客户端能识别预览类型。"""
    lower_headers = {key.lower(): value for key, value in headers.items()}
    content_type = lower_headers.get("content-type", "")
    _require(bool(content_type), f"{label} missing Content-Type")
    media_type = content_type.split(";", 1)[0].strip().lower()
    expected_prefixes = {
        "image": ("image/",),
        "web_image": ("image/",),
        "video": ("video/",),
        "audio": ("audio/",),
    }.get(str(kind or "").strip().lower(), ("image/", "video/", "audio/"))
    _require(
        media_type.startswith(expected_prefixes),
        f"{label} returned unexpected Content-Type: {content_type}",
    )
    content_length = lower_headers.get("content-length")
    if content_length:
        try:
            size = int(content_length)
        except ValueError as exc:
            raise AssertionError(f"{label} returned invalid Content-Length") from exc
        _require(size >= 0, f"{label} returned negative Content-Length")


def _require_cors_preflight(
    status: int,
    headers: dict[str, str],
    *,
    origin: str,
    label: str,
) -> None:
    """校验浏览器跨域预检响应，避免网页客户端卡在 OPTIONS 阶段。"""
    lower_headers = {key.lower(): value for key, value in headers.items()}
    _require(status == 200, f"{label} CORS preflight returned {status}")
    allow_origin = lower_headers.get("access-control-allow-origin", "")
    _require(
        allow_origin in {origin, "*"},
        f"{label} CORS preflight returned unexpected allow-origin",
    )
    allow_headers = {
        item.strip().lower()
        for item in lower_headers.get("access-control-allow-headers", "").split(",")
        if item.strip()
    }
    for header_name in _CORS_REQUEST_HEADERS:
        _require(
            header_name in allow_headers,
            f"{label} CORS preflight missing {header_name} header",
        )
    allow_methods = lower_headers.get("access-control-allow-methods", "").lower()
    _require("post" in allow_methods, f"{label} CORS preflight missing POST method")
    _require("x-request-id" in lower_headers, f"{label} CORS preflight missing X-Request-ID")


def _require_cors_actual_response(
    status: int,
    headers: dict[str, str],
    *,
    origin: str,
    label: str,
) -> None:
    """校验真实跨域响应头，确保浏览器端能读取排障用的请求编号。"""
    lower_headers = {key.lower(): value for key, value in headers.items()}
    _require(status in {200, 401}, f"{label} CORS actual response returned {status}")
    allow_origin = lower_headers.get("access-control-allow-origin", "")
    _require(
        allow_origin in {origin, "*"},
        f"{label} CORS actual response returned unexpected allow-origin",
    )
    expose_headers = lower_headers.get("access-control-expose-headers", "").lower()
    _require("x-request-id" in expose_headers, f"{label} CORS actual response does not expose X-Request-ID")
    _require("x-request-id" in lower_headers, f"{label} CORS actual response missing X-Request-ID")


def _require_chat_completion_response(data: dict, *, expected_model: str) -> None:
    """校验非流式 Chat Completions 基础结构，贴近 OpenAI SDK 的解析预期。"""
    _require(data.get("object") == "chat.completion", "chat response is not an OpenAI chat completion")
    _require(str(data.get("id") or "").startswith("chatcmpl-"), "chat response missing chatcmpl id")
    _require(isinstance(data.get("created"), int), "chat response missing integer created")
    _require(data.get("model") == expected_model, "chat response has unexpected model")
    usage = data.get("usage")
    _require(isinstance(usage, dict), "chat response missing usage")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        _require(isinstance(usage.get(field), int), f"chat response usage missing {field}")
    choices = data.get("choices") or []
    _require(bool(choices), "chat response missing choices")
    choice = choices[0]
    _require(choice.get("index") == 0, "chat response first choice index is not 0")
    _require(choice.get("finish_reason") in {"stop", "length", "tool_calls"}, "chat response has invalid finish_reason")
    message = choice.get("message") or {}
    _require(message.get("role") == "assistant", "chat response message role is not assistant")
    _require(
        bool(message.get("content") or message.get("tool_calls")),
        "chat response missing message content or tool_calls",
    )


def _require_chat_completion_stream_chunk(
    data: dict,
    *,
    expected_model: str,
    final: bool = False,
) -> None:
    """校验 Chat Completions 流式 chunk，避免外部 SDK 解析 SSE 时缺关键字段。"""
    _require(data.get("object") == "chat.completion.chunk", "stream chunk is not an OpenAI chat completion chunk")
    _require(str(data.get("id") or "").startswith("chatcmpl-"), "stream chunk missing chatcmpl id")
    _require(isinstance(data.get("created"), int), "stream chunk missing integer created")
    _require(data.get("model") == expected_model, "stream chunk has unexpected model")
    choices = data.get("choices")
    _require(isinstance(choices, list), "stream chunk missing choices")
    if final and choices == []:
        # include_usage=true 的用量 chunk 会用空 choices；普通 smoke 不强制要求，但保持兼容。
        return
    _require(bool(choices), "stream chunk missing choices")
    choice = choices[0]
    _require(choice.get("index") == 0, "stream chunk first choice index is not 0")
    _require(isinstance(choice.get("delta"), dict), "stream chunk missing delta")
    finish_reason = choice.get("finish_reason")
    if final:
        _require(finish_reason in {"stop", "length", "tool_calls"}, "stream final chunk has invalid finish_reason")
    else:
        _require(finish_reason is None, "stream delta chunk finish_reason must be null")


def _require_openai_stream_usage_chunk(data: dict, *, label: str) -> None:
    """校验 include_usage=true 时 OpenAI 兼容流式接口返回的用量 chunk。"""
    _require(data.get("choices") == [], f"{label} usage chunk choices must be empty")
    usage = data.get("usage")
    _require(isinstance(usage, dict), f"{label} stream missing usage object")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        _require(isinstance(usage.get(field), int), f"{label} stream usage missing {field}")


def _require_completion_response(data: dict, *, expected_model: str) -> None:
    """校验旧版 Completions API 返回结构，确保老客户端能读到模型、文本和用量。"""
    _require(data.get("object") == "text_completion", "completion body is not an OpenAI text completion")
    _require(str(data.get("id") or "").startswith("cmpl-"), "completion body missing cmpl id")
    _require(isinstance(data.get("created"), int), "completion body missing integer created")
    _require(data.get("model") == expected_model, "completion body has unexpected model")
    usage = data.get("usage")
    _require(isinstance(usage, dict), "completion body missing usage")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        _require(isinstance(usage.get(field), int), f"completion body usage missing {field}")
    choices = data.get("choices") or []
    _require(bool(choices), "completion body missing choices")
    choice = choices[0]
    _require(choice.get("index") == 0, "completion body first choice index is not 0")
    _require(isinstance(choice.get("text"), str) and bool(choice.get("text")), "completion body missing choice text")
    _require(choice.get("finish_reason") in {"stop", "length"}, "completion body has invalid finish_reason")


def _require_completion_stream_chunk(data: dict, *, expected_model: str, final: bool = False) -> None:
    """校验旧版 Completions 流式 chunk，避免老客户端解析时缺少关键字段。"""
    _require(data.get("object") == "text_completion.chunk", "completion stream chunk is not a text completion chunk")
    _require(str(data.get("id") or "").startswith("cmpl-"), "completion stream chunk missing cmpl id")
    _require(isinstance(data.get("created"), int), "completion stream chunk missing integer created")
    _require(data.get("model") == expected_model, "completion stream chunk has unexpected model")
    choices = data.get("choices")
    _require(isinstance(choices, list), "completion stream chunk missing choices")
    if final and choices == []:
        return
    _require(bool(choices), "completion stream chunk missing choices")
    choice = choices[0]
    _require(choice.get("index") == 0, "completion stream chunk first choice index is not 0")
    _require("text" in choice, "completion stream chunk missing text")
    finish_reason = choice.get("finish_reason")
    if final:
        _require(finish_reason in {"stop", "length"}, "completion stream final chunk has invalid finish_reason")
    else:
        _require(finish_reason is None, "completion stream delta finish_reason must be null")


def _require_responses_stream_usage_event(data: dict) -> None:
    """校验 Responses 流式完成事件里包含兼容 SDK 可读取的 usage 字段。"""
    response = data.get("response") if isinstance(data, dict) else None
    _require(isinstance(response, dict), "responses stream completed event missing response object")
    usage = response.get("usage")
    _require(isinstance(usage, dict), "responses stream completed event missing usage")
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        _require(isinstance(usage.get(field), int), f"responses stream usage missing {field}")


def _require_responses_response(data: dict, *, expected_model: str, label: str = "responses") -> None:
    """校验 OpenAI Responses API 返回结构，确保外部 SDK 能稳定读取模型、输出和用量。"""
    _require(data.get("object") == "response", f"{label} body is not an OpenAI response object")
    _require(str(data.get("id") or "").startswith("resp_"), f"{label} body missing resp_ id")
    _require(data.get("status") == "completed", f"{label} body status is not completed")
    _require(data.get("model") == expected_model, f"{label} body has unexpected model")
    usage = data.get("usage")
    _require(isinstance(usage, dict), f"{label} body missing usage")
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        _require(isinstance(usage.get(field), int), f"{label} body usage missing {field}")
    output = data.get("output") or []
    _require(bool(output), f"{label} body missing output")
    _require(
        bool(data.get("output_text") or any(item.get("type") == "function_call" for item in output if isinstance(item, dict))),
        f"{label} body missing output_text or function_call",
    )


def _require_chat_tool_call_response(data: dict, *, expected_model: str) -> None:
    """校验 Chat Completions 工具调用返回 OpenAI 客户端可执行的结构。"""
    _require_chat_completion_response(data, expected_model=expected_model)
    choices = data.get("choices") or []
    choice = choices[0]
    _require(choice.get("finish_reason") == "tool_calls", "tool probe finish_reason is not tool_calls")
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    _require(isinstance(tool_calls, list) and bool(tool_calls), "tool probe missing tool_calls")
    call = tool_calls[0]
    _require(call.get("type") == "function", "tool probe call type is not function")
    _require(bool(call.get("id")), "tool probe call missing id")
    function = call.get("function") or {}
    _require(function.get("name") == "echo_tool", "tool probe returned unexpected function name")
    arguments = function.get("arguments")
    _require(isinstance(arguments, str), "tool probe function arguments must be a JSON string")
    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise AssertionError("tool probe function arguments are not valid JSON") from exc
    _require(isinstance(parsed_arguments, dict), "tool probe function arguments are not a JSON object")
    _require(bool(parsed_arguments), "tool probe function arguments are empty")


def _run_smoke_impl(
    base_url: str,
    api_key: str | None,
    *,
    api_key_header: str = "authorization",
    auth_header_probes: bool = False,
    admin_username: str | None = None,
    admin_password: str | None = None,
    chat_prompt: str | None = None,
    chat_model: str = "gemini-3.1-pro",
    chat_stream: bool = False,
    chat_tool_probe: bool = False,
    image_prompt: str | None = None,
    image_model: str = "gemini-3.1-pro",
    image_response_format: str = "url",
    image_edit_file: str | None = None,
    image_edit_prompt: str | None = None,
    image_edit_mask_file: str | None = None,
    image_variation_file: str | None = None,
    media_history: bool = False,
    media_content_probes: bool = False,
    media_content_probe_limit: int = 3,
    probe_endpoints: bool = False,
    error_probes: bool = False,
    probe_model: str = "gemini-3.1-pro",
    file_probes: bool = False,
    file_smoke_path: str | None = None,
    file_smoke_purpose: str = "assistants",
    native_probes: bool = False,
    responses_prompt: str | None = None,
    responses_model: str = "gemini-3.1-pro",
    responses_stream: bool = False,
    completion_prompt: str | None = None,
    completion_model: str = "gemini-3.1-pro",
    completion_stream: bool = False,
    gemini_prompt: str | None = None,
    gemini_model: str = "gemini-3.1-pro",
    gemini_stream: bool = False,
    audio_transcription_file: str | None = None,
    audio_translation_file: str | None = None,
    audio_model: str = "gemini-3.1-pro",
    audio_response_format: str = "json",
    health_probes: bool = False,
    ui_probes: bool = False,
    cors_probes: bool = False,
    cors_origin: str = "https://your-panel.example.com",
    timeout: float = 120.0,
    fail_on_warnings: bool = False,
    readiness_probes: bool = False,
    unavailable_generation_probe: bool = False,
    require_account: bool = False,
) -> list[str]:
    results: list[str] = []

    health_status, health, _ = _request(base_url, "/health", timeout=timeout)
    _require(health_status == 200, f"/health returned {health_status}")
    _require(health.get("ok") is True, "/health did not return ok=true")
    _require("models" in health, "/health missing models")
    results.append("health ok")
    account_summary = health.get("accounts") or {}
    if require_account:
        # 对外真实生成必须至少有一个启用且未过期的 Gemini 账号；接口层 smoke 可不强制。
        available_accounts = int(account_summary.get("available") or 0)
        _require(
            available_accounts > 0,
            "/health reports no available Gemini accounts; authorize at least one account before external generation.",
        )
        results.append("available account ok")
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

    if health_probes:
        # 部署平台和反向代理常用 /healthz、/readyz、/livez 以及 HEAD 探测服务状态。
        for path in ("/healthz", "/readyz", "/livez"):
            probe_status, probe, probe_headers = _request(
                base_url,
                path,
                timeout=timeout,
            )
            _require(probe_status == 200, f"{path} returned {probe_status}")
            _require(probe.get("ok") is True, f"{path} did not return ok=true")
            _require("models" in probe, f"{path} missing models")
            _require("x-request-id" in {key.lower(): value for key, value in probe_headers.items()}, f"{path} response missing X-Request-ID")
            head_status, head_body, head_headers = _raw_request(
                base_url,
                path,
                timeout=timeout,
                method="HEAD",
            )
            _require_head_response(head_status, head_body, head_headers, label=f"HEAD {path}")
        results.append("health probes ok")

    if ui_probes:
        # 管理端首页是服务器自助授权和诊断入口；这里只检查静态 HTML，不触发任何模型调用。
        ui_status, ui_body, ui_headers = _raw_request(
            base_url,
            "/",
            timeout=timeout,
            headers={"Accept": "text/html"},
        )
        _require(ui_status == 200, f"admin UI returned {ui_status}")
        content_type = next(
            (value for key, value in ui_headers.items() if key.lower() == "content-type"),
            "",
        )
        _require("html" in content_type.lower(), "admin UI did not return HTML")
        _require_admin_ui(ui_body)
        results.append("admin ui ok")

    if cors_probes:
        # 浏览器客户端会先发 OPTIONS 预检；这里不消耗模型调用，只验证跨域调用面能过网关。
        cors_status, _, cors_headers = _request(
            base_url,
            "/v1/chat/completions",
            timeout=timeout,
            method="OPTIONS",
            headers={
                "Origin": cors_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": ",".join(_CORS_REQUEST_HEADERS),
                "X-Request-ID": "smoke-cors-preflight",
            },
        )
        _require_cors_preflight(
            cors_status,
            cors_headers,
            origin=cors_origin,
            label="/v1/chat/completions",
        )
        results.append("cors preflight ok")
        cors_actual_status, _, cors_actual_headers = _request(
            base_url,
            "/v1/models",
            timeout=timeout,
            api_key=api_key,
            headers={"Origin": cors_origin},
        )
        _require_cors_actual_response(
            cors_actual_status,
            cors_actual_headers,
            origin=cors_origin,
            label="/v1/models",
        )
        results.append("cors actual response ok")

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
        model_ids = {item.get("id") for item in models.get("data", [])}
        _require(
            {"gemini-3.1-pro", "gemini-3.5-flash", "gemini-3.1-flash-lite"}.issubset(model_ids),
            "/v1/models missing current Gemini models",
        )
        _require("x-request-id" in {key.lower(): value for key, value in headers.items()}, "authorized response missing X-Request-ID")
        results.append("authorized models ok")

        if readiness_probes or require_account:
            readiness_status, readiness, readiness_headers = _request(
                base_url,
                "/v1/generation-readiness",
                timeout=timeout,
                api_key=api_key,
            )
            _require(
                readiness_status == 200,
                f"/v1/generation-readiness returned {readiness_status}",
            )
            _require(
                "x-request-id" in {key.lower(): value for key, value in readiness_headers.items()},
                "/v1/generation-readiness response missing X-Request-ID",
            )
            _require_generation_readiness(readiness)
            results.append("generation readiness ok")
            if require_account:
                _require(
                    readiness.get("ready") is True,
                    "/v1/generation-readiness reports generation is not ready; authorize at least one Gemini account.",
                )

    if auth_header_probes:
        _require(bool(api_key), "--auth-header-probes requires --api-key")
        for header_name in ("authorization", "x-api-key", "api-key", "openai-api-key"):
            status, models, headers = _request(
                base_url,
                "/v1/models",
                timeout=timeout,
                api_key=api_key,
                api_key_header=header_name,
            )
            _require(status == 200, f"/v1/models with {header_name} returned {status}")
            _require(models.get("object") == "list", f"/v1/models with {header_name} did not return an OpenAI list")
            _require("x-request-id" in {key.lower(): value for key, value in headers.items()}, f"/v1/models with {header_name} missing X-Request-ID")
        results.append("auth header probes ok")

    if error_probes:
        if health.get("auth", {}).get("api_key_required"):
            for path in ("/v1/models", "/models", "/engines"):
                unauth_status, unauth, unauth_headers = _request(
                    base_url,
                    path,
                    timeout=timeout,
                )
                _require_openai_error(
                    unauth_status,
                    unauth,
                    unauth_headers,
                    expected_status=401,
                    expected_type="authentication_error",
                    label=f"unauthenticated {path}",
                )
        _require(
            bool(api_key) or not health.get("auth", {}).get("api_key_required"),
            "--error-probes requires --api-key when API key auth is enabled",
        )
        missing_status, missing, missing_headers = _request(
            base_url,
            "/v1/not-a-real-smoke-endpoint",
            timeout=timeout,
            api_key=api_key,
        )
        _require_openai_error(
            missing_status,
            missing,
            missing_headers,
            expected_status=404,
            expected_type="invalid_request_error",
            label="missing endpoint",
        )
        for path, label in (
            ("/models/not-a-real-smoke-model", "missing rootless model"),
            ("/engines/not-a-real-smoke-model", "missing rootless engine"),
        ):
            alias_missing_status, alias_missing, alias_missing_headers = _request(
                base_url,
                path,
                timeout=timeout,
                api_key=api_key,
            )
            _require_openai_error(
                alias_missing_status,
                alias_missing,
                alias_missing_headers,
                expected_status=404,
                expected_type="invalid_request_error",
                label=label,
            )
        method_status, method_body, method_headers = _request(
            base_url,
            "/v1/chat/completions",
            timeout=timeout,
            api_key=api_key,
            method="GET",
        )
        _require_openai_error(
            method_status,
            method_body,
            method_headers,
            expected_status=405,
            expected_type="invalid_request_error",
            label="wrong method endpoint",
        )
        results.append("error probes ok")

    if unavailable_generation_probe:
        _require(
            bool(api_key) or not health.get("auth", {}).get("api_key_required"),
            "--unavailable-generation-probe requires --api-key when API key auth is enabled",
        )
        available_accounts = int(account_summary.get("available") or 0)
        _require(
            available_accounts == 0,
            "--unavailable-generation-probe only runs when /health reports zero available Gemini accounts.",
        )
        # 账号池不可用时应返回服务暂不可用，而不是伪装成外部 API Key 鉴权失败。
        # 这里覆盖主要外部生成入口，避免某个兼容接口漏出 500 或非标准错误体。
        for path, body, label in (
            (
                "/v1/chat/completions",
                {
                    "model": chat_model,
                    "messages": [{"role": "user", "content": "smoke no available account"}],
                },
                "unavailable chat completions",
            ),
            (
                "/v1/responses",
                {
                    "model": chat_model,
                    "input": "smoke no available account",
                },
                "unavailable responses",
            ),
            (
                "/v1/completions",
                {
                    "model": chat_model,
                    "prompt": "smoke no available account",
                },
                "unavailable completions",
            ),
            (
                "/v1/gemini/generate",
                {
                    "model": chat_model,
                    "prompt": "smoke no available account",
                },
                "unavailable gemini generate",
            ),
            (
                "/v1/images/generations",
                {
                    "model": image_model,
                    "prompt": "smoke no available account image",
                },
                "unavailable image generation",
            ),
        ):
            _probe_unavailable_generation_endpoint(
                base_url=base_url,
                timeout=timeout,
                api_key=api_key,
                path=path,
                body=body,
                label=label,
            )
        results.append("unavailable generation ok")

    if probe_endpoints:
        # 外部 SDK、API 网关和反向代理经常先探测根路径、模型列表 HEAD 和模型详情。
        root_status, root, root_headers = _request(
            base_url,
            "/v1",
            timeout=timeout,
            api_key=api_key,
        )
        if health.get("auth", {}).get("api_key_required") and not api_key:
            _require(root_status == 401, "/v1 should require an API key")
            results.append("endpoint probe protection ok")
        else:
            _require(root_status == 200, f"/v1 returned {root_status}")
            _require(root.get("object") == "api.root", "/v1 did not return api.root")
            _require(isinstance(root.get("endpoints"), dict), "/v1 missing endpoints")
            _require("x-request-id" in {key.lower(): value for key, value in root_headers.items()}, "/v1 response missing X-Request-ID")
            root_head_status, root_head_body, root_head_headers = _raw_request(
                base_url,
                "/v1",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(root_head_status, root_head_body, root_head_headers, label="HEAD /v1")
            head_status, head_body, head_headers = _raw_request(
                base_url,
                "/v1/models",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(head_status, head_body, head_headers, label="HEAD /v1/models")
            detail_status, detail, detail_headers = _request(
                base_url,
                f"/v1/models/{probe_model}",
                timeout=timeout,
                api_key=api_key,
            )
            _require(detail_status == 200, f"/v1/models/{probe_model} returned {detail_status}")
            _require(detail.get("object") == "model", "model detail is not an OpenAI model object")
            _require(detail.get("id") == probe_model, "model detail returned unexpected id")
            _require("x-request-id" in {key.lower(): value for key, value in detail_headers.items()}, "model detail response missing X-Request-ID")
            alias_status, alias_models, alias_headers = _request(
                base_url,
                "/models",
                timeout=timeout,
                api_key=api_key,
            )
            _require(alias_status == 200, f"/models returned {alias_status}")
            _require(alias_models.get("object") == "list", "/models did not return an OpenAI list")
            _require("x-request-id" in {key.lower(): value for key, value in alias_headers.items()}, "/models response missing X-Request-ID")
            alias_head_status, alias_head_body, alias_head_headers = _raw_request(
                base_url,
                f"/models/{probe_model}",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(
                alias_head_status,
                alias_head_body,
                alias_head_headers,
                label=f"HEAD /models/{probe_model}",
            )
            engines_status, engines, engines_headers = _request(
                base_url,
                "/v1/engines",
                timeout=timeout,
                api_key=api_key,
            )
            _require(engines_status == 200, f"/v1/engines returned {engines_status}")
            _require(engines.get("object") == "list", "/v1/engines did not return an OpenAI list")
            _require("x-request-id" in {key.lower(): value for key, value in engines_headers.items()}, "/v1/engines response missing X-Request-ID")
            engine_head_status, engine_head_body, engine_head_headers = _raw_request(
                base_url,
                f"/v1/engines/{probe_model}",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(
                engine_head_status,
                engine_head_body,
                engine_head_headers,
                label=f"HEAD /v1/engines/{probe_model}",
            )
            rootless_engines_status, rootless_engines, rootless_engines_headers = _request(
                base_url,
                "/engines",
                timeout=timeout,
                api_key=api_key,
            )
            _require(rootless_engines_status == 200, f"/engines returned {rootless_engines_status}")
            _require(rootless_engines.get("object") == "list", "/engines did not return an OpenAI list")
            _require("x-request-id" in {key.lower(): value for key, value in rootless_engines_headers.items()}, "/engines response missing X-Request-ID")
            rootless_engine_head_status, rootless_engine_head_body, rootless_engine_head_headers = _raw_request(
                base_url,
                f"/engines/{probe_model}",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(
                rootless_engine_head_status,
                rootless_engine_head_body,
                rootless_engine_head_headers,
                label=f"HEAD /engines/{probe_model}",
            )
            results.append("endpoint probes ok")

    if file_probes:
        # 文件接口的 HEAD 探测不读取正文，适合验证外部 SDK 和网关的连通性检查。
        head_paths = ("/v1/files", "/v1/gemini/files")
        if health.get("auth", {}).get("api_key_required") and not api_key:
            status, body_text, headers = _raw_request(
                base_url,
                head_paths[0],
                timeout=timeout,
                method="HEAD",
            )
            _require_head_response(
                status,
                body_text,
                headers,
                expected_status=401,
                label="HEAD /v1/files",
            )
            results.append("file probe protection ok")
        else:
            for path in head_paths:
                status, body_text, headers = _raw_request(
                    base_url,
                    path,
                    timeout=timeout,
                    api_key=api_key,
                    method="HEAD",
                )
                _require_head_response(status, body_text, headers, label=f"HEAD {path}")
            results.append("file probes ok")

    if file_smoke_path:
        # 完整文件生命周期 smoke 不触发模型调用，但会验证外部文件上传、读取和删除链路。
        source = Path(file_smoke_path)
        _require(source.is_file(), f"file smoke path does not exist: {file_smoke_path}")
        if health.get("auth", {}).get("api_key_required") and not api_key:
            upload_status, _, _ = _multipart_request(
                base_url,
                "/v1/files",
                timeout=timeout,
                file_path=file_smoke_path,
                fields={"purpose": file_smoke_purpose},
            )
            _require(upload_status == 401, "/v1/files upload should require an API key")
            results.append("file lifecycle protection ok")
        else:
            upload_status, upload, upload_headers = _multipart_request(
                base_url,
                "/v1/files",
                timeout=timeout,
                api_key=api_key,
                file_path=file_smoke_path,
                fields={"purpose": file_smoke_purpose},
            )
            _require(upload_status == 200, f"/v1/files upload returned {upload_status}")
            _require("x-request-id" in {key.lower(): value for key, value in upload_headers.items()}, "file upload response missing X-Request-ID")
            _require(upload.get("object") == "file", "file upload response is not an OpenAI file object")
            file_id = str(upload.get("id") or "")
            _require(file_id.startswith("file-"), "file upload response missing file id")
            _require(upload.get("filename") == source.name, "file upload response returned unexpected filename")
            _require(int(upload.get("bytes") or -1) == source.stat().st_size, "file upload response returned unexpected byte size")
            _require(upload.get("purpose") == file_smoke_purpose, "file upload response returned unexpected purpose")

            list_status, listed, list_headers = _request(
                base_url,
                "/v1/files",
                timeout=timeout,
                api_key=api_key,
            )
            _require(list_status == 200, f"/v1/files returned {list_status}")
            _require("x-request-id" in {key.lower(): value for key, value in list_headers.items()}, "file list response missing X-Request-ID")
            _require(listed.get("object") == "list", "file list response is not an OpenAI list")
            _require(any(item.get("id") == file_id for item in listed.get("data", [])), "file list response missing uploaded file")

            detail_status, detail, detail_headers = _request(
                base_url,
                f"/v1/files/{file_id}",
                timeout=timeout,
                api_key=api_key,
            )
            _require(detail_status == 200, f"/v1/files/{file_id} returned {detail_status}")
            _require("x-request-id" in {key.lower(): value for key, value in detail_headers.items()}, "file detail response missing X-Request-ID")
            _require(detail.get("id") == file_id, "file detail returned unexpected id")

            detail_head_status, detail_head_body, detail_head_headers = _raw_request(
                base_url,
                f"/v1/files/{file_id}",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(
                detail_head_status,
                detail_head_body,
                detail_head_headers,
                label=f"HEAD /v1/files/{file_id}",
            )

            content_status, content_body, content_headers = _raw_request(
                base_url,
                f"/v1/files/{file_id}/content",
                timeout=timeout,
                api_key=api_key,
            )
            _require(content_status == 200, f"/v1/files/{file_id}/content returned {content_status}")
            _require("x-request-id" in {key.lower(): value for key, value in content_headers.items()}, "file content response missing X-Request-ID")
            expected_body = source.read_bytes().decode("utf-8", errors="replace")
            _require(content_body == expected_body, "file content response did not match uploaded file")

            content_head_status, content_head_body, content_head_headers = _raw_request(
                base_url,
                f"/v1/files/{file_id}/content",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require_head_response(
                content_head_status,
                content_head_body,
                content_head_headers,
                label=f"HEAD /v1/files/{file_id}/content",
            )

            native_status, native, native_headers = _request(
                base_url,
                "/v1/gemini/files",
                timeout=timeout,
                api_key=api_key,
            )
            _require(native_status == 200, f"/v1/gemini/files returned {native_status}")
            _require("x-request-id" in {key.lower(): value for key, value in native_headers.items()}, "native file list response missing X-Request-ID")
            _require(any(item.get("id") == file_id for item in native.get("files", [])), "native file list missing uploaded file")

            delete_status, deleted, delete_headers = _request(
                base_url,
                f"/v1/files/{file_id}",
                timeout=timeout,
                api_key=api_key,
                method="DELETE",
            )
            _require(delete_status == 200, f"DELETE /v1/files/{file_id} returned {delete_status}")
            _require("x-request-id" in {key.lower(): value for key, value in delete_headers.items()}, "file delete response missing X-Request-ID")
            _require(deleted.get("deleted") is True, "file delete response missing deleted=true")
            missing_status, _, _ = _request(
                base_url,
                f"/v1/files/{file_id}",
                timeout=timeout,
                api_key=api_key,
            )
            _require(missing_status == 404, "deleted file detail should return 404")
            results.append("file lifecycle ok")

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
        _require_media_cooldown_summary(media)
        results.append("media cooldown summary ok")

    if media_history:
        # 媒体历史不消耗模型次数，用来验证外部客户端能读取图片/视频/音频索引和代理链接。
        history_status, history, history_headers = _request(
            base_url,
            "/v1/gemini/media?limit=5",
            timeout=timeout,
            api_key=api_key,
        )
        if health.get("auth", {}).get("api_key_required") and not api_key:
            _require(history_status == 401, "/v1/gemini/media should require an API key")
            results.append("media history protection ok")
        else:
            _require(history_status == 200, f"/v1/gemini/media returned {history_status}")
            _require("x-request-id" in {key.lower(): value for key, value in history_headers.items()}, "media history response missing X-Request-ID")
            records = history.get("media")
            _require(isinstance(records, list), "/v1/gemini/media missing media list")
            for item in records:
                _require(isinstance(item, dict), "media history item is not an object")
                _require(bool(item.get("kind")), "media history item missing kind")
                _require(bool(item.get("url")), "media history item missing url")
                _require(bool(item.get("content_url")), "media history item missing content_url")
            results.append("media history ok")
            if media_content_probes:
                if not records:
                    results.append("media content probes skipped: no media records")
                else:
                    checked = 0
                    for item in records[: max(1, int(media_content_probe_limit))]:
                        content_path = _media_content_path(base_url, str(item.get("content_url") or ""))
                        status, body_text, headers = _raw_request(
                            base_url,
                            content_path,
                            timeout=timeout,
                            method="HEAD",
                        )
                        _require_head_response(status, body_text, headers, label=f"HEAD {content_path}")
                        _require_media_content_headers(
                            headers,
                            kind=str(item.get("kind") or ""),
                            label=f"HEAD {content_path}",
                        )
                        checked += 1
                    results.append(f"media content probes ok ({checked})")

    if native_probes:
        # Gemini 原生只读端点不触发模型调用，适合部署后确认 API Key 保护和原生能力入口可达。
        read_paths = (
            ("/v1/gemini/gems", "gems"),
            ("/v1/gemini/jobs", "jobs"),
        )
        if health.get("auth", {}).get("api_key_required") and not api_key:
            for path, _field in read_paths:
                status, _, _ = _request(
                    base_url,
                    path,
                    timeout=timeout,
                )
                _require(status == 401, f"{path} should require an API key")
            results.append("native probe protection ok")
        else:
            for path, field in read_paths:
                status, body, headers = _request(
                    base_url,
                    path,
                    timeout=timeout,
                    api_key=api_key,
                )
                _require(status == 200, f"{path} returned {status}")
                _require("x-request-id" in {key.lower(): value for key, value in headers.items()}, f"{path} response missing X-Request-ID")
                _require(isinstance(body.get(field), list), f"{path} missing {field} list")
            results.append("native probes ok")

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
        protected_status, protected, protected_headers = _request(
            base_url,
            "/v1/request-logs",
            timeout=timeout,
        )
        _require(protected_status == 401, "/v1/request-logs should require admin login")
        _require("x-request-id" in {key.lower(): value for key, value in protected_headers.items()}, "admin protection response missing X-Request-ID")
        _require(
            protected.get("detail") == "Admin login required.",
            "admin protection response did not require admin login",
        )
        if api_key:
            # 外部 API Key 只能调用模型/媒体等外部接口，不能读取后台日志或改管理状态。
            api_key_admin_status, api_key_admin, _ = _request(
                base_url,
                "/v1/request-logs",
                timeout=timeout,
                api_key=api_key,
            )
            _require(
                api_key_admin_status == 401,
                "/v1/request-logs should reject external API key",
            )
            _require(
                api_key_admin.get("detail") == "Admin login required.",
                "external API key unexpectedly accessed admin endpoint",
            )
            api_key_create_status, api_key_create, _ = _request(
                base_url,
                "/v1/system-settings/api-keys",
                timeout=timeout,
                api_key=api_key,
                method="POST",
                body={},
            )
            _require(
                api_key_create_status == 401,
                "/v1/system-settings/api-keys should reject external API key",
            )
            _require(
                api_key_create.get("detail") == "Admin login required.",
                "external API key unexpectedly generated a system API key",
            )
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
        generated_status, generated, _ = _request(
            base_url,
            "/v1/system-settings/api-keys",
            timeout=timeout,
            method="POST",
            body={},
            headers={"Cookie": cookie},
        )
        _require(
            generated_status == 200,
            f"/v1/system-settings/api-keys with admin cookie returned {generated_status}",
        )
        generated_key = str(generated.get("api_key") or "")
        generated_fingerprint = str(generated.get("fingerprint") or "")
        _require(generated_key.startswith("sk-gemini-"), "admin API key generation returned an unexpected key")
        _require(bool(generated_fingerprint), "admin API key generation missing fingerprint")
        delete_status, deleted, _ = _request(
            base_url,
            f"/v1/system-settings/api-keys/{generated_fingerprint}",
            timeout=timeout,
            method="DELETE",
            headers={"Cookie": cookie},
        )
        _require(
            delete_status == 200,
            f"DELETE /v1/system-settings/api-keys/{generated_fingerprint} returned {delete_status}",
        )
        _require(deleted.get("deleted") == 1, "admin API key cleanup did not delete the generated key")
        logout_status, logout, logout_headers = _request(
            base_url,
            "/v1/admin/logout",
            timeout=timeout,
            method="POST",
            body={},
            headers={"Cookie": cookie},
        )
        _require(logout_status == 200, f"/v1/admin/logout returned {logout_status}")
        _require(logout.get("ok") is True, "admin logout did not return ok=true")
        logout_cookie = next(
            (
                value
                for key, value in logout_headers.items()
                if key.lower() == "set-cookie"
            ),
            "",
        )
        _require(
            "gemini_admin_session=" in logout_cookie and "Max-Age=0" in logout_cookie,
            "admin logout did not clear the session cookie",
        )
        results.append("admin login and boundary ok")

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
        _require("x-request-id" in {key.lower(): value for key, value in chat_headers.items()}, "chat response missing X-Request-ID")
        _require_chat_completion_response(chat, expected_model=chat_model)
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
                    "stream_options": {"include_usage": True},
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
            parsed_chunks = [json.loads(item) for item in chunks]
            _require_chat_completion_stream_chunk(parsed_chunks[0], expected_model=chat_model)
            final_chunks = [
                item
                for item in parsed_chunks
                if (item.get("choices") or [{}])[0].get("finish_reason") is not None
            ]
            _require(bool(final_chunks), "stream response missing final finish_reason chunk")
            _require_chat_completion_stream_chunk(
                final_chunks[-1],
                expected_model=chat_model,
                final=True,
            )
            usage_chunks = [item for item in parsed_chunks if item.get("choices") == []]
            _require(bool(usage_chunks), "stream response missing include_usage chunk")
            _require_openai_stream_usage_chunk(usage_chunks[-1], label="chat")
            results.append("chat stream ok")

    if chat_tool_probe:
        # OpenClaw 等工具型客户端依赖 OpenAI 标准 tool_calls 结构；这里显式验证函数名和 arguments JSON 字符串。
        _require(
            bool(api_key) or not health.get("auth", {}).get("api_key_required"),
            "--chat-tool-probe requires --api-key when API key auth is enabled",
        )
        tool_status, tool_chat, tool_headers = _request(
            base_url,
            "/v1/chat/completions",
            timeout=timeout,
            api_key=api_key,
            method="POST",
            body={
                "model": chat_model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Call echo_tool with a JSON object containing "
                            'the key "message" and value "smoke".'
                        ),
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "echo_tool",
                            "description": "Echo a short message for smoke testing.",
                            "parameters": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                            },
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "echo_tool"},
                },
            },
        )
        _require(tool_status == 200, f"tool probe /v1/chat/completions returned {tool_status}")
        _require("x-request-id" in {key.lower(): value for key, value in tool_headers.items()}, "tool probe response missing X-Request-ID")
        _require_chat_tool_call_response(tool_chat, expected_model=chat_model)
        results.append("chat tool_calls ok")

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

    if image_edit_file:
        # 图片编辑会消耗图片生成次数；显式传入图片文件时才验证 OpenAI 兼容 multipart 入口。
        _require(bool(image_edit_prompt), "--image-edit-file requires --image-edit-prompt")
        files = [("image", image_edit_file)]
        if image_edit_mask_file:
            files.append(("mask", image_edit_mask_file))
        edit_status, edit, edit_headers = _multipart_request(
            base_url,
            "/v1/images/edits",
            timeout=timeout,
            api_key=api_key,
            files=files,
            fields={
                "model": image_model,
                "prompt": image_edit_prompt or "",
                "response_format": image_response_format,
            },
        )
        _require(edit_status == 200, f"/v1/images/edits returned {edit_status}")
        _require("x-request-id" in {key.lower(): value for key, value in edit_headers.items()}, "image edit response missing X-Request-ID")
        data = edit.get("data") or []
        _require(bool(data), "image edit response missing data")
        first_item = data[0]
        if image_response_format == "b64_json":
            _require(bool(first_item.get("b64_json")), "image edit response missing b64_json")
        else:
            url = str(first_item.get("url") or "")
            _require(url.startswith(("http://", "https://")), "image edit response missing absolute url")
        results.append("image edit ok")

    if image_variation_file:
        # 图片变体同样是媒体生成入口，显式传入图片文件时才执行真实接口验证。
        variation_status, variation, variation_headers = _multipart_request(
            base_url,
            "/v1/images/variations",
            timeout=timeout,
            api_key=api_key,
            files=[("image", image_variation_file)],
            fields={
                "model": image_model,
                "response_format": image_response_format,
            },
        )
        _require(variation_status == 200, f"/v1/images/variations returned {variation_status}")
        _require("x-request-id" in {key.lower(): value for key, value in variation_headers.items()}, "image variation response missing X-Request-ID")
        data = variation.get("data") or []
        _require(bool(data), "image variation response missing data")
        first_item = data[0]
        if image_response_format == "b64_json":
            _require(bool(first_item.get("b64_json")), "image variation response missing b64_json")
        else:
            url = str(first_item.get("url") or "")
            _require(url.startswith(("http://", "https://")), "image variation response missing absolute url")
        results.append("image variation ok")

    if audio_transcription_file:
        # 音频转写需要上传本地文件，只有显式传入文件路径时才执行真实外部接口验证。
        audio_status, audio_body, audio_headers = _raw_multipart_request(
            base_url,
            "/v1/audio/transcriptions",
            timeout=timeout,
            api_key=api_key,
            file_path=audio_transcription_file,
            fields={
                "model": audio_model,
                "response_format": audio_response_format,
            },
        )
        _require(audio_status == 200, f"/v1/audio/transcriptions returned {audio_status}")
        _require("x-request-id" in {key.lower(): value for key, value in audio_headers.items()}, "audio transcription response missing X-Request-ID")
        _require_audio_response(audio_body, audio_response_format, "audio transcription")
        results.append("audio transcription ok")

    if audio_translation_file:
        # 音频翻译同样走 OpenAI 兼容 multipart 入口，按调用方指定格式校验响应。
        audio_status, audio_body, audio_headers = _raw_multipart_request(
            base_url,
            "/v1/audio/translations",
            timeout=timeout,
            api_key=api_key,
            file_path=audio_translation_file,
            fields={
                "model": audio_model,
                "response_format": audio_response_format,
            },
        )
        _require(audio_status == 200, f"/v1/audio/translations returned {audio_status}")
        _require("x-request-id" in {key.lower(): value for key, value in audio_headers.items()}, "audio translation response missing X-Request-ID")
        _require_audio_response(audio_body, audio_response_format, "audio translation")
        results.append("audio translation ok")

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
        _require_responses_response(responses, expected_model=responses_model)
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
                    "stream_options": {"include_usage": True},
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
            completed_items = [
                json.loads(item)
                for item in data_items
                if item != "[DONE]" and json.loads(item).get("type") == "response.completed"
            ]
            _require(bool(completed_items), "responses stream missing completed data")
            _require_responses_stream_usage_event(completed_items[-1])
            _require_responses_response(
                completed_items[-1]["response"],
                expected_model=responses_model,
                label="responses stream completed",
            )
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
        _require_completion_response(completion, expected_model=completion_model)
        results.append("completions api ok")

        if completion_stream:
            stream_status, stream_body, stream_headers = _raw_request(
                base_url,
                "/v1/completions",
                timeout=timeout,
                api_key=api_key,
                method="POST",
                body={
                    "model": completion_model,
                    "prompt": completion_prompt,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )
            _require(stream_status == 200, f"stream /v1/completions returned {stream_status}")
            content_type = next(
                (value for key, value in stream_headers.items() if key.lower() == "content-type"),
                "",
            )
            _require("text/event-stream" in content_type.lower(), "completion stream is not text/event-stream")
            _require("x-request-id" in {key.lower(): value for key, value in stream_headers.items()}, "completion stream missing X-Request-ID")
            data_items = _sse_data_items(stream_body)
            _require("[DONE]" in data_items, "completion stream missing [DONE]")
            chunks = [item for item in data_items if item != "[DONE]"]
            _require(bool(chunks), "completion stream missing data chunks")
            parsed_chunks = [json.loads(item) for item in chunks]
            _require_completion_stream_chunk(parsed_chunks[0], expected_model=completion_model)
            final_chunks = [
                item
                for item in parsed_chunks
                if (item.get("choices") or [{}])[0].get("finish_reason") is not None
            ]
            _require(bool(final_chunks), "completion stream missing final finish_reason chunk")
            _require_completion_stream_chunk(
                final_chunks[-1],
                expected_model=completion_model,
                final=True,
            )
            usage_chunks = [item for item in parsed_chunks if item.get("choices") == []]
            _require(bool(usage_chunks), "completion stream missing include_usage chunk")
            _require_openai_stream_usage_chunk(usage_chunks[-1], label="completion")
            results.append("completions stream ok")

    if gemini_prompt:
        # Gemini 原生接口暴露分类输出，适合验证非 OpenAI 兼容调用面的基础结构。
        gemini_status, gemini, gemini_headers = _request(
            base_url,
            "/v1/gemini/generate",
            timeout=timeout,
            api_key=api_key,
            method="POST",
            body={
                "model": gemini_model,
                "prompt": gemini_prompt,
            },
        )
        _require(gemini_status == 200, f"/v1/gemini/generate returned {gemini_status}")
        _require("x-request-id" in {key.lower(): value for key, value in gemini_headers.items()}, "gemini generate response missing X-Request-ID")
        _require(gemini.get("ok") is True, "gemini generate body missing ok=true")
        _require(
            gemini.get("model") == gemini_model,
            "gemini generate body has unexpected model",
        )
        _require(isinstance(gemini.get("output"), dict), "gemini generate body missing output object")
        _require("metadata" in gemini, "gemini generate body missing metadata")
        output = gemini["output"]
        _require(
            bool(output.get("text") or output.get("images") or output.get("videos") or output.get("media")),
            "gemini generate output is empty",
        )
        results.append("gemini generate ok")

        if gemini_stream:
            # Gemini 原生流式接口返回分类输出的 final 包，和 OpenAI 兼容流式格式不同。
            stream_status, stream_body, stream_headers = _raw_request(
                base_url,
                "/v1/gemini/stream",
                timeout=timeout,
                api_key=api_key,
                method="POST",
                body={
                    "model": gemini_model,
                    "prompt": gemini_prompt,
                },
            )
            _require(stream_status == 200, f"/v1/gemini/stream returned {stream_status}")
            content_type = next(
                (value for key, value in stream_headers.items() if key.lower() == "content-type"),
                "",
            )
            _require("text/event-stream" in content_type.lower(), "gemini stream is not text/event-stream")
            _require("x-request-id" in {key.lower(): value for key, value in stream_headers.items()}, "gemini stream missing X-Request-ID")
            data_items = _sse_data_items(stream_body)
            _require("[DONE]" in data_items, "gemini stream missing [DONE]")
            chunks = [json.loads(item) for item in data_items if item != "[DONE]"]
            final_chunks = [item for item in chunks if item.get("type") == "final"]
            _require(bool(final_chunks), "gemini stream missing final chunk")
            final_chunk = final_chunks[-1]
            _require(final_chunk.get("ok") is True, "gemini stream final chunk missing ok=true")
            _require(isinstance(final_chunk.get("output"), dict), "gemini stream final chunk missing output object")
            _require("metadata" in final_chunk, "gemini stream final chunk missing metadata")
            results.append("gemini stream ok")

    return results


def run_smoke(
    base_url: str,
    api_key: str | None,
    *,
    api_key_header: str = "authorization",
    auth_header_probes: bool = False,
    admin_username: str | None = None,
    admin_password: str | None = None,
    chat_prompt: str | None = None,
    chat_model: str = "gemini-3.1-pro",
    chat_stream: bool = False,
    chat_tool_probe: bool = False,
    image_prompt: str | None = None,
    image_model: str = "gemini-3.1-pro",
    image_response_format: str = "url",
    image_edit_file: str | None = None,
    image_edit_prompt: str | None = None,
    image_edit_mask_file: str | None = None,
    image_variation_file: str | None = None,
    media_history: bool = False,
    media_content_probes: bool = False,
    media_content_probe_limit: int = 3,
    probe_endpoints: bool = False,
    error_probes: bool = False,
    probe_model: str = "gemini-3.1-pro",
    file_probes: bool = False,
    file_smoke_path: str | None = None,
    file_smoke_purpose: str = "assistants",
    native_probes: bool = False,
    responses_prompt: str | None = None,
    responses_model: str = "gemini-3.1-pro",
    responses_stream: bool = False,
    completion_prompt: str | None = None,
    completion_model: str = "gemini-3.1-pro",
    completion_stream: bool = False,
    gemini_prompt: str | None = None,
    gemini_model: str = "gemini-3.1-pro",
    gemini_stream: bool = False,
    audio_transcription_file: str | None = None,
    audio_translation_file: str | None = None,
    audio_model: str = "gemini-3.1-pro",
    audio_response_format: str = "json",
    health_probes: bool = False,
    ui_probes: bool = False,
    cors_probes: bool = False,
    cors_origin: str = "https://your-panel.example.com",
    timeout: float = 120.0,
    fail_on_warnings: bool = False,
    readiness_probes: bool = False,
    unavailable_generation_probe: bool = False,
    require_account: bool = False,
) -> list[str]:
    global _ACTIVE_API_KEY_HEADER
    previous_api_key_header = _ACTIVE_API_KEY_HEADER
    _ACTIVE_API_KEY_HEADER = _normalize_api_key_header(api_key_header)
    try:
        return _run_smoke_impl(
            base_url,
            api_key,
            api_key_header=api_key_header,
            auth_header_probes=auth_header_probes,
            admin_username=admin_username,
            admin_password=admin_password,
            chat_prompt=chat_prompt,
            chat_model=chat_model,
            chat_stream=chat_stream,
            chat_tool_probe=chat_tool_probe,
            image_prompt=image_prompt,
            image_model=image_model,
            image_response_format=image_response_format,
            image_edit_file=image_edit_file,
            image_edit_prompt=image_edit_prompt,
            image_edit_mask_file=image_edit_mask_file,
            image_variation_file=image_variation_file,
            media_history=media_history,
            media_content_probes=media_content_probes,
            media_content_probe_limit=media_content_probe_limit,
            probe_endpoints=probe_endpoints,
            error_probes=error_probes,
            probe_model=probe_model,
            file_probes=file_probes,
            file_smoke_path=file_smoke_path,
            file_smoke_purpose=file_smoke_purpose,
            native_probes=native_probes,
            responses_prompt=responses_prompt,
            responses_model=responses_model,
            responses_stream=responses_stream,
            completion_prompt=completion_prompt,
            completion_model=completion_model,
            completion_stream=completion_stream,
            gemini_prompt=gemini_prompt,
            gemini_model=gemini_model,
            gemini_stream=gemini_stream,
            audio_transcription_file=audio_transcription_file,
            audio_translation_file=audio_translation_file,
            audio_model=audio_model,
            audio_response_format=audio_response_format,
            health_probes=health_probes,
            ui_probes=ui_probes,
            cors_probes=cors_probes,
            cors_origin=cors_origin,
            timeout=timeout,
            fail_on_warnings=fail_on_warnings,
            readiness_probes=readiness_probes,
            unavailable_generation_probe=unavailable_generation_probe,
            require_account=require_account,
        )
    finally:
        _ACTIVE_API_KEY_HEADER = previous_api_key_header


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deployed Gemini API Web service.")
    parser.add_argument("--base-url", default="http://localhost:7860")
    parser.add_argument("--api-key", default="")
    parser.add_argument(
        "--api-key-header",
        default="authorization",
        choices=("authorization", "bearer", "x-api-key", "api-key", "openai-api-key"),
        help="Header style used by --api-key for smoke requests.",
    )
    parser.add_argument(
        "--auth-header-probes",
        action="store_true",
        help="Verify Authorization, X-API-Key, API-Key, and OpenAI-API-Key auth headers.",
    )
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
    parser.add_argument("--chat-model", default="gemini-3.1-pro")
    parser.add_argument(
        "--chat-stream",
        action="store_true",
        help="Also verify streaming /v1/chat/completions when --chat-prompt is set.",
    )
    parser.add_argument(
        "--chat-tool-probe",
        action="store_true",
        help="Verify OpenAI Chat Completions tool_calls shape with a real tool request.",
    )
    parser.add_argument(
        "--image-prompt",
        default="",
        help="Optional prompt for a real /v1/images/generations smoke request.",
    )
    parser.add_argument("--image-model", default="gemini-3.1-pro")
    parser.add_argument(
        "--image-response-format",
        default="url",
        choices=("url", "b64_json"),
    )
    parser.add_argument(
        "--image-edit-file",
        default="",
        help="Optional local image file for a real /v1/images/edits smoke request.",
    )
    parser.add_argument(
        "--image-edit-prompt",
        default="",
        help="Prompt used with --image-edit-file.",
    )
    parser.add_argument(
        "--image-edit-mask-file",
        default="",
        help="Optional local mask file used with --image-edit-file.",
    )
    parser.add_argument(
        "--image-variation-file",
        default="",
        help="Optional local image file for a real /v1/images/variations smoke request.",
    )
    parser.add_argument(
        "--audio-transcription-file",
        default="",
        help="Optional local audio file for a real /v1/audio/transcriptions smoke request.",
    )
    parser.add_argument(
        "--audio-translation-file",
        default="",
        help="Optional local audio file for a real /v1/audio/translations smoke request.",
    )
    parser.add_argument("--audio-model", default="gemini-3.1-pro")
    parser.add_argument(
        "--audio-response-format",
        default="json",
        choices=("json", "text", "verbose_json", "srt", "vtt"),
        help="Response format used by audio transcription/translation smoke requests.",
    )
    parser.add_argument(
        "--media-history",
        action="store_true",
        help="Verify /v1/gemini/media history shape without consuming model calls.",
    )
    parser.add_argument(
        "--media-content-probes",
        action="store_true",
        help="When --media-history is set, also HEAD probe media content_url links.",
    )
    parser.add_argument(
        "--media-content-probe-limit",
        type=int,
        default=3,
        help="Maximum number of media content_url links to HEAD probe.",
    )
    parser.add_argument(
        "--probe-endpoints",
        action="store_true",
        help="Verify /v1 root, model, and legacy engine probe endpoints.",
    )
    parser.add_argument(
        "--error-probes",
        action="store_true",
        help="Verify OpenAI-compatible 401/404/405 error responses and request IDs.",
    )
    parser.add_argument("--probe-model", default="gemini-3.1-pro")
    parser.add_argument(
        "--file-probes",
        action="store_true",
        help="Verify HEAD /v1/files and HEAD /v1/gemini/files probes.",
    )
    parser.add_argument(
        "--file-smoke-path",
        default="",
        help="Optional local file for a full /v1/files upload/list/read/delete smoke request.",
    )
    parser.add_argument("--file-smoke-purpose", default="assistants")
    parser.add_argument(
        "--native-probes",
        action="store_true",
        help="Verify read-only Gemini native endpoints such as /v1/gemini/gems and /v1/gemini/jobs.",
    )
    parser.add_argument(
        "--responses-prompt",
        default="",
        help="Optional prompt for a real /v1/responses smoke request.",
    )
    parser.add_argument("--responses-model", default="gemini-3.1-pro")
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
    parser.add_argument("--completion-model", default="gemini-3.1-pro")
    parser.add_argument(
        "--completion-stream",
        action="store_true",
        help="Also verify streaming /v1/completions when --completion-prompt is set.",
    )
    parser.add_argument(
        "--gemini-prompt",
        default="",
        help="Optional prompt for a real /v1/gemini/generate smoke request.",
    )
    parser.add_argument("--gemini-model", default="gemini-3.1-pro")
    parser.add_argument(
        "--gemini-stream",
        action="store_true",
        help="Also verify streaming /v1/gemini/stream when --gemini-prompt is set.",
    )
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Fail when /health reports deployment warnings.",
    )
    parser.add_argument(
        "--health-probes",
        action="store_true",
        help="Verify GET/HEAD /healthz, /readyz, and /livez deployment probes.",
    )
    parser.add_argument(
        "--ui-probes",
        action="store_true",
        help="Verify the admin UI contains readiness diagnostics and current model labels.",
    )
    parser.add_argument(
        "--cors-probes",
        action="store_true",
        help="Verify browser CORS preflight and actual response header exposure.",
    )
    parser.add_argument(
        "--cors-origin",
        default="https://your-panel.example.com",
        help="Origin header used by --cors-probes.",
    )
    parser.add_argument(
        "--readiness-probes",
        action="store_true",
        help="Verify /v1/generation-readiness without consuming model calls.",
    )
    parser.add_argument(
        "--unavailable-generation-probe",
        action="store_true",
        help="When no account is available, verify generation returns OpenAI-compatible 503.",
    )
    parser.add_argument(
        "--require-account",
        action="store_true",
        help="Fail unless /health reports at least one available Gemini account.",
    )
    args = parser.parse_args()
    try:
        results = run_smoke(
            args.base_url,
            args.api_key or None,
            api_key_header=args.api_key_header,
            auth_header_probes=args.auth_header_probes,
            admin_username=args.admin_username or None,
            admin_password=args.admin_password or None,
            chat_prompt=args.chat_prompt or None,
            chat_model=args.chat_model,
            chat_stream=args.chat_stream,
            chat_tool_probe=args.chat_tool_probe,
            image_prompt=args.image_prompt or None,
            image_model=args.image_model,
            image_response_format=args.image_response_format,
            image_edit_file=args.image_edit_file or None,
            image_edit_prompt=args.image_edit_prompt or None,
            image_edit_mask_file=args.image_edit_mask_file or None,
            image_variation_file=args.image_variation_file or None,
            media_history=args.media_history,
            media_content_probes=args.media_content_probes,
            media_content_probe_limit=max(1, args.media_content_probe_limit),
            probe_endpoints=args.probe_endpoints,
            error_probes=args.error_probes,
            probe_model=args.probe_model,
            file_probes=args.file_probes,
            file_smoke_path=args.file_smoke_path or None,
            file_smoke_purpose=args.file_smoke_purpose,
            native_probes=args.native_probes,
            responses_prompt=args.responses_prompt or None,
            responses_model=args.responses_model,
            responses_stream=args.responses_stream,
            completion_prompt=args.completion_prompt or None,
            completion_model=args.completion_model,
            completion_stream=args.completion_stream,
            gemini_prompt=args.gemini_prompt or None,
            gemini_model=args.gemini_model,
            gemini_stream=args.gemini_stream,
            audio_transcription_file=args.audio_transcription_file or None,
            audio_translation_file=args.audio_translation_file or None,
            audio_model=args.audio_model,
            audio_response_format=args.audio_response_format,
            health_probes=args.health_probes,
            ui_probes=args.ui_probes,
            cors_probes=args.cors_probes,
            cors_origin=args.cors_origin,
            timeout=max(1.0, args.timeout),
            fail_on_warnings=args.fail_on_warnings,
            readiness_probes=args.readiness_probes,
            unavailable_generation_probe=args.unavailable_generation_probe,
            require_account=args.require_account,
        )
    except Exception as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    for item in results:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
