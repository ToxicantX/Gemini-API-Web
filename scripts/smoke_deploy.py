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


def _multipart_request(
    base_url: str,
    path: str,
    *,
    timeout: float,
    file_path: str,
    file_field: str = "file",
    fields: dict[str, str] | None = None,
    api_key: str | None = None,
) -> tuple[int, dict, dict[str, str]]:
    """发送 multipart/form-data 请求，用于验证外部文件上传类接口。"""
    source = Path(file_path)
    _require(source.is_file(), f"audio file does not exist: {file_path}")
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
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    parts.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{source.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            source.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
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
    try:
        data = json.loads(response_body.decode("utf-8", errors="replace")) if response_body else {}
    except json.JSONDecodeError:
        data = {}
    return status, data, response_headers


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
    media_history: bool = False,
    media_content_probes: bool = False,
    media_content_probe_limit: int = 3,
    probe_endpoints: bool = False,
    probe_model: str = "gemini",
    file_probes: bool = False,
    file_smoke_path: str | None = None,
    file_smoke_purpose: str = "assistants",
    responses_prompt: str | None = None,
    responses_model: str = "gemini",
    responses_stream: bool = False,
    completion_prompt: str | None = None,
    completion_model: str = "gemini",
    completion_stream: bool = False,
    gemini_prompt: str | None = None,
    gemini_model: str = "gemini",
    gemini_stream: bool = False,
    audio_transcription_file: str | None = None,
    audio_translation_file: str | None = None,
    audio_model: str = "gemini",
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
            head_status, _, head_headers = _raw_request(
                base_url,
                "/v1/models",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require(head_status == 200, f"HEAD /v1/models returned {head_status}")
            _require("x-request-id" in {key.lower(): value for key, value in head_headers.items()}, "HEAD /v1/models missing X-Request-ID")
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
            alias_head_status, _, alias_head_headers = _raw_request(
                base_url,
                f"/models/{probe_model}",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require(alias_head_status == 200, f"HEAD /models/{probe_model} returned {alias_head_status}")
            _require("x-request-id" in {key.lower(): value for key, value in alias_head_headers.items()}, "HEAD /models/{model} missing X-Request-ID")
            engines_status, engines, engines_headers = _request(
                base_url,
                "/v1/engines",
                timeout=timeout,
                api_key=api_key,
            )
            _require(engines_status == 200, f"/v1/engines returned {engines_status}")
            _require(engines.get("object") == "list", "/v1/engines did not return an OpenAI list")
            _require("x-request-id" in {key.lower(): value for key, value in engines_headers.items()}, "/v1/engines response missing X-Request-ID")
            engine_head_status, _, engine_head_headers = _raw_request(
                base_url,
                f"/v1/engines/{probe_model}",
                timeout=timeout,
                api_key=api_key,
                method="HEAD",
            )
            _require(engine_head_status == 200, f"HEAD /v1/engines/{probe_model} returned {engine_head_status}")
            _require("x-request-id" in {key.lower(): value for key, value in engine_head_headers.items()}, "HEAD /v1/engines/{model} missing X-Request-ID")
            results.append("endpoint probes ok")

    if file_probes:
        # 文件接口的 HEAD 探测不读取正文，适合验证外部 SDK 和网关的连通性检查。
        head_paths = ("/v1/files", "/v1/gemini/files")
        if health.get("auth", {}).get("api_key_required") and not api_key:
            status, _, _ = _raw_request(
                base_url,
                head_paths[0],
                timeout=timeout,
                method="HEAD",
            )
            _require(status == 401, "HEAD /v1/files should require an API key")
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
                _require(status == 200, f"HEAD {path} returned {status}")
                _require(body_text == "", f"HEAD {path} should not return a body")
                _require("x-request-id" in {key.lower(): value for key, value in headers.items()}, f"HEAD {path} missing X-Request-ID")
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
        _require(media.get("ok") is True, "/v1/media-cooldowns missing ok=true")
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
                        _require(status == 200, f"HEAD {content_path} returned {status}")
                        _require(body_text == "", f"HEAD {content_path} should not return a body")
                        content_type = next(
                            (
                                value
                                for key, value in headers.items()
                                if key.lower() == "content-type"
                            ),
                            "",
                        )
                        _require(bool(content_type), f"HEAD {content_path} missing Content-Type")
                        checked += 1
                    results.append(f"media content probes ok ({checked})")

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

    if audio_transcription_file:
        # 音频转写需要上传本地文件，只有显式传入文件路径时才执行真实外部接口验证。
        audio_status, audio, audio_headers = _multipart_request(
            base_url,
            "/v1/audio/transcriptions",
            timeout=timeout,
            api_key=api_key,
            file_path=audio_transcription_file,
            fields={
                "model": audio_model,
                "response_format": "json",
            },
        )
        _require(audio_status == 200, f"/v1/audio/transcriptions returned {audio_status}")
        _require("x-request-id" in {key.lower(): value for key, value in audio_headers.items()}, "audio transcription response missing X-Request-ID")
        _require(bool(audio.get("text")), "audio transcription response missing text")
        results.append("audio transcription ok")

    if audio_translation_file:
        # 音频翻译同样走 OpenAI 兼容 multipart 入口，校验 JSON text 字段即可覆盖外部客户端常用路径。
        audio_status, audio, audio_headers = _multipart_request(
            base_url,
            "/v1/audio/translations",
            timeout=timeout,
            api_key=api_key,
            file_path=audio_translation_file,
            fields={
                "model": audio_model,
                "response_format": "json",
            },
        )
        _require(audio_status == 200, f"/v1/audio/translations returned {audio_status}")
        _require("x-request-id" in {key.lower(): value for key, value in audio_headers.items()}, "audio translation response missing X-Request-ID")
        _require(bool(audio.get("text")), "audio translation response missing text")
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
            first_chunk = json.loads(chunks[0])
            _require(first_chunk.get("object") == "text_completion.chunk", "completion stream chunk is not a text completion chunk")
            _require("choices" in first_chunk, "completion stream chunk missing choices")
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
        _require(gemini.get("model") in {gemini_model, "gemini"}, "gemini generate body has unexpected model")
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
        "--audio-transcription-file",
        default="",
        help="Optional local audio file for a real /v1/audio/transcriptions smoke request.",
    )
    parser.add_argument(
        "--audio-translation-file",
        default="",
        help="Optional local audio file for a real /v1/audio/translations smoke request.",
    )
    parser.add_argument("--audio-model", default="gemini")
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
        help="Verify /v1 root, HEAD /v1/models, and /v1/models/{model} probes.",
    )
    parser.add_argument("--probe-model", default="gemini")
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
        "--completion-stream",
        action="store_true",
        help="Also verify streaming /v1/completions when --completion-prompt is set.",
    )
    parser.add_argument(
        "--gemini-prompt",
        default="",
        help="Optional prompt for a real /v1/gemini/generate smoke request.",
    )
    parser.add_argument("--gemini-model", default="gemini")
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
            media_history=args.media_history,
            media_content_probes=args.media_content_probes,
            media_content_probe_limit=max(1, args.media_content_probe_limit),
            probe_endpoints=args.probe_endpoints,
            probe_model=args.probe_model,
            file_probes=args.file_probes,
            file_smoke_path=args.file_smoke_path or None,
            file_smoke_purpose=args.file_smoke_purpose,
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
