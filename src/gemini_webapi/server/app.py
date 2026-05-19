from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import orjson as json
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..constants import Model
from ..exceptions import AuthError, GeminiError, ModelInvalid
from ..types import DeepResearchPlan
from .auth_browser import AuthBrowserManager, AuthBrowserUnavailable
from .config import ServerConfig
from .database import AccountStore
from .rotator import AccountRotator


class GenerateRequest(BaseModel):
    prompt: str
    model: str | None = None
    temporary: bool = False


class GeminiGenerateRequest(BaseModel):
    prompt: str
    model: str | None = None
    temporary: bool = False
    gem: str | None = None
    gem_id: str | None = None
    gem_name: str | None = None
    deep_research: bool = False
    extensions: list[str] | dict[str, Any] | None = None
    file_ids: list[str] = Field(default_factory=list)


class GemRequest(BaseModel):
    name: str
    prompt: str
    description: str = ""


class DeepResearchCreateRequest(BaseModel):
    prompt: str
    model: str | None = None


class DeepResearchStartRequest(BaseModel):
    job_id: str | None = None
    plan: dict[str, Any] | None = None
    confirm_prompt: str | None = None


class DeepResearchWaitRequest(BaseModel):
    job_id: str
    poll_interval: float = 10.0
    timeout: float = 600.0


class AccountRequest(BaseModel):
    name: str | None = None
    secure_1psid: str = Field(alias="__Secure-1PSID")
    secure_1psidts: str | None = Field(default=None, alias="__Secure-1PSIDTS")
    cookies: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    model_config = {"populate_by_name": True}


class AccountToggleRequest(BaseModel):
    enabled: bool


class SettingsRequest(BaseModel):
    switch_on_uses: int | None = None
    failure_threshold: int | None = None


class SwitchAccountRequest(BaseModel):
    account_id: int | None = None


class AuthClickRequest(BaseModel):
    x: float
    y: float


class AuthTypeRequest(BaseModel):
    text: str


class AuthPressRequest(BaseModel):
    key: str


class AuthSaveRequest(BaseModel):
    name: str | None = None


class FunctionToolSpec(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ChatToolSpec(BaseModel):
    type: str = "function"
    function: FunctionToolSpec


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    tools: list[ChatToolSpec] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None


MODEL_ALIASES = {
    "gemini": "gemini-3-pro",
    "gemini-3.1-pro": "gemini-3-pro",
    "gemini-3-pro-preview": "gemini-3-pro",
    "gemini-3.1-pro-preview": "gemini-3-pro",
    "gemini-3-flash-preview": "gemini-3-flash",
}


def _message_content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _messages_to_prompt(messages: list[ChatMessage]) -> str:
    prompt_parts: list[str] = []
    for message in messages:
        text = _message_content_to_text(message.content)
        role = message.role.lower()
        if role == "system":
            if not text:
                continue
            prompt_parts.append(f"System: {text}")
        elif role == "assistant":
            if text:
                prompt_parts.append(f"Assistant: {text}")
            if message.tool_calls:
                prompt_parts.append(
                    f"Assistant tool calls: {json.dumps(message.tool_calls).decode()}"
                )
        elif role == "tool":
            if not text:
                continue
            label = message.name or message.tool_call_id or "tool"
            prompt_parts.append(f"Tool result ({label}): {text}")
        else:
            if not text:
                continue
            prompt_parts.append(f"User: {text}")
    return "\n\n".join(prompt_parts)


def _tools_enabled(request: ChatCompletionRequest) -> bool:
    if not request.tools:
        return False
    return request.tool_choice != "none"


def _tool_choice_name(tool_choice: str | dict[str, Any] | None) -> str | None:
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return None


def _tool_specs_text(tools: list[ChatToolSpec]) -> str:
    lines: list[str] = []
    for tool in tools:
        if tool.type != "function":
            continue
        function = tool.function
        parameters = function.parameters or {"type": "object", "properties": {}}
        lines.append(
            "\n".join(
                [
                    f"- name: {function.name}",
                    f"  description: {function.description or ''}",
                    f"  parameters: {json.dumps(parameters).decode()}",
                ]
            )
        )
    return "\n".join(lines)


def _append_tool_instructions(prompt: str, request: ChatCompletionRequest) -> str:
    if not _tools_enabled(request):
        return prompt
    tools = request.tools or []
    forced_name = _tool_choice_name(request.tool_choice)
    choice_line = "If no tool is needed, answer normally."
    if request.tool_choice == "required":
        choice_line = "You must call one of the available tools."
    if forced_name:
        choice_line = f"You must call the tool named {forced_name}."
    parallel_line = ""
    if request.parallel_tool_calls is False:
        parallel_line = "Return at most one tool call."
    instructions = f"""
Tool calling is available.
When a tool is needed, respond with only valid JSON in this exact schema:
{{"tool_calls":[{{"name":"tool_name","arguments":{{}}}}]}}
Do not wrap the JSON in markdown. Do not include natural language with a tool call.
{choice_line}
{parallel_line}
Available tools:
{_tool_specs_text(tools)}
"""
    return f"{prompt}\n\nSystem: {instructions.strip()}"


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_json_value(text: str) -> Any | None:
    stripped = _strip_json_fence(text)
    try:
        return json.loads(stripped)
    except Exception:
        pass
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(stripped[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[start : index + 1])
                except Exception:
                    return None
    return None


def _arguments_to_openai_json(arguments: Any) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments).decode()


def _normalize_tool_call(
    item: Any,
    allowed_names: set[str],
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    function = item.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments", item.get("arguments"))
    elif isinstance(function, str):
        name = function
        arguments = item.get("arguments")
    else:
        name = item.get("name") or item.get("tool_name")
        arguments = item.get("arguments") if "arguments" in item else item.get("args")
    if not isinstance(name, str) or name not in allowed_names:
        return None
    call_id = item.get("id") if isinstance(item.get("id"), str) else f"call_{uuid.uuid4().hex}"
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": _arguments_to_openai_json(arguments),
        },
    }


def _tool_calls_from_output_text(
    text: str,
    tools: list[ChatToolSpec] | None,
) -> list[dict[str, Any]]:
    if not tools:
        return []
    parsed = _extract_json_value(text)
    if not isinstance(parsed, dict):
        return []
    raw_calls: Any
    if isinstance(parsed.get("tool_calls"), list):
        raw_calls = parsed["tool_calls"]
    elif isinstance(parsed.get("tool_call"), dict):
        raw_calls = [parsed["tool_call"]]
    elif "name" in parsed or "function" in parsed:
        raw_calls = [parsed]
    else:
        return []
    allowed_names = {tool.function.name for tool in tools if tool.type == "function"}
    calls: list[dict[str, Any]] = []
    for item in raw_calls:
        call = _normalize_tool_call(item, allowed_names)
        if call:
            calls.append(call)
    return calls


def _chat_tool_calls_chunk(
    completion_id: str,
    model: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    stream_calls = []
    for index, call in enumerate(tool_calls):
        stream_calls.append(
            {
                "index": index,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }
        )
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": stream_calls},
                "finish_reason": None,
            }
        ],
    }


def _openai_model_ids() -> list[str]:
    return [
        "gemini",
        "gemini-3.1-pro",
        "gemini-3-flash",
        "gemini-3-flash-thinking",
    ]


def _resolve_model_arg(model: str | None) -> str | None:
    if not model:
        return None
    model_key = model.lower()
    if model_key == "unspecified":
        return None
    return MODEL_ALIASES.get(model_key, model)


def _error_status(exc: Exception) -> int:
    if isinstance(exc, AuthError):
        return 401
    if isinstance(exc, (ValueError, ModelInvalid)):
        return 400
    if isinstance(exc, GeminiError):
        return 502
    return 500


def _openai_error(message: str, status_code: int, error_type: str = "api_error") -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": status_code,
        }
    }


def _chat_chunk(
    completion_id: str,
    model: str,
    content: str = "",
    *,
    role: str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    delta: dict[str, str] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _dump_model(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _image_dict(image: Any, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "url": getattr(image, "url", ""),
        "title": getattr(image, "title", None),
        "alt": getattr(image, "alt", None),
        "image_id": getattr(image, "image_id", None),
    }


def _video_dict(video: Any, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "url": getattr(video, "url", ""),
        "thumbnail": getattr(video, "thumbnail", None),
        "title": getattr(video, "title", None),
    }


def _media_dict(media: Any) -> dict[str, Any]:
    return {
        "kind": "audio",
        "url": getattr(media, "mp3_url", "") or getattr(media, "url", ""),
        "mp3_url": getattr(media, "mp3_url", ""),
        "mp4_url": getattr(media, "mp4_url", ""),
        "thumbnail": getattr(media, "mp3_thumbnail", "")
        or getattr(media, "mp4_thumbnail", ""),
        "title": getattr(media, "title", None),
    }


def _classified_output(output: Any) -> dict[str, Any]:
    candidate = output.candidates[output.chosen] if output.candidates else None
    web_images = [_image_dict(image, "web_image") for image in getattr(candidate, "web_images", [])]
    generated_images = [
        _image_dict(image, "image") for image in getattr(candidate, "generated_images", [])
    ]
    videos = [_video_dict(video, "video") for video in getattr(candidate, "generated_videos", [])]
    media = [_media_dict(item) for item in getattr(candidate, "generated_media", [])]
    return {
        "text": output.text,
        "thoughts": output.thoughts,
        "images": generated_images,
        "videos": videos,
        "media": media,
        "web_images": web_images,
        "deep_research_plan": _dump_model(output.deep_research_plan),
    }


def _media_entries(output: Any) -> list[dict[str, Any]]:
    classified = _classified_output(output)
    entries: list[dict[str, Any]] = []
    entries.extend(classified["images"])
    entries.extend(classified["videos"])
    entries.extend(classified["media"])
    entries.extend(classified["web_images"])
    return [entry for entry in entries if entry.get("url")]


def _job_dict(job: Any) -> dict[str, Any]:
    return job.__dict__


def _file_dict(file_record: Any) -> dict[str, Any]:
    return file_record.__dict__


def _gem_dict(gem: Any) -> dict[str, Any]:
    return {
        "id": gem.id,
        "name": gem.name,
        "description": gem.description,
        "prompt": gem.prompt,
        "predefined": gem.predefined,
    }


def create_app(config: ServerConfig | None = None):
    import httpx
    import orjson as json
    from pathlib import Path

    config = config or ServerConfig.from_env()
    store = AccountStore(config.database_path)
    store.import_accounts_file(config.accounts_file)
    switch_on_uses = int(store.get_state("switch_on_uses", str(config.switch_on_uses)))
    failure_threshold = int(
        store.get_state("failure_threshold", str(config.failure_threshold))
    )
    rotator = AccountRotator(
        store,
        switch_on_uses=switch_on_uses,
        failure_threshold=failure_threshold,
        immediate_switch_status_codes=config.immediate_switch_status_codes,
        proxy=config.proxy,
        request_timeout=config.request_timeout,
        auto_refresh=config.auto_refresh,
    )
    auth_browser = AuthBrowserManager(
        store,
        start_url=config.auth_url,
        headless=config.auth_headless,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        app.state.rotator = rotator
        app.state.auth_browser = auth_browser
        yield
        await auth_browser.close()
        await rotator.close()
        store.close()

    app = FastAPI(title="gemini-webapi server", version="0.1.0", lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        error_type = "invalid_request_error"
        if exc.status_code in {401, 403}:
            error_type = "authentication_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=_openai_error(detail, exc.status_code, error_type),
        )

    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        if (
            config.api_keys
            and request.url.path.startswith("/v1/")
            and request.url.path != "/v1/status"
        ):
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            if token not in config.api_keys:
                return JSONResponse(
                    status_code=401,
                    content=_openai_error(
                        "Invalid or missing API key.",
                        401,
                        "authentication_error",
                    ),
                )
        return await call_next(request)

    async def _proxy_novnc(path: str, request: Request) -> Response:
        url = f"http://127.0.0.1:6080/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        async with httpx.AsyncClient(timeout=30) as client:
            proxied = await client.request(
                request.method,
                url,
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in {"host", "connection"}
                },
                content=await request.body(),
            )
        return Response(
            content=proxied.content,
            status_code=proxied.status_code,
            media_type=proxied.headers.get("content-type"),
        )

    @app.api_route("/novnc", methods=["GET", "POST"])
    async def novnc_root(request: Request) -> Response:
        return await _proxy_novnc("", request)

    @app.api_route("/novnc/{path:path}", methods=["GET", "POST"])
    async def novnc_proxy(path: str, request: Request) -> Response:
        return await _proxy_novnc(path, request)

    @app.get("/")
    async def console() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/v1/status")
    async def status() -> dict[str, Any]:
        return rotator.status()

    @app.get("/v1/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "switch_on_uses": rotator.switch_on_uses,
            "failure_threshold": rotator.failure_threshold,
        }

    @app.patch("/v1/settings")
    async def update_settings(request: SettingsRequest) -> dict[str, Any]:
        if request.switch_on_uses is not None:
            store.set_state("switch_on_uses", str(max(0, request.switch_on_uses)))
        if request.failure_threshold is not None:
            store.set_state("failure_threshold", str(max(0, request.failure_threshold)))
        rotator.configure(
            switch_on_uses=request.switch_on_uses,
            failure_threshold=request.failure_threshold,
        )
        return {"ok": True, "settings": await get_settings()}

    @app.get("/v1/request-logs")
    async def request_logs(limit: int = 80) -> dict[str, Any]:
        return {"logs": rotator.request_logs(limit=max(1, min(limit, 500)))}

    @app.get("/v1/gemini/media")
    async def gemini_media(limit: int = 80, kind: str | None = None) -> dict[str, Any]:
        return {
            "media": [
                item.__dict__
                for item in store.list_media_outputs(
                    limit=max(1, min(limit, 500)), kind=kind
                )
            ]
        }

    @app.get("/v1/gemini/jobs")
    async def gemini_jobs(limit: int = 80, job_type: str | None = None) -> dict[str, Any]:
        return {
            "jobs": [
                _job_dict(job)
                for job in store.list_jobs(limit=max(1, min(limit, 500)), job_type=job_type)
            ]
        }

    @app.get("/v1/gemini/files")
    async def list_files(limit: int = 80) -> dict[str, Any]:
        return {"files": [_file_dict(item) for item in store.list_files(limit=limit)]}

    @app.post("/v1/gemini/files")
    async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
        file_id = f"file-{uuid.uuid4().hex}"
        upload_dir = Path(config.database_path).resolve().parent / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "upload").suffix
        safe_name = f"{file_id}{suffix}"
        dest = upload_dir / safe_name
        data = await file.read()
        dest.write_bytes(data)
        store.add_file(
            file_id=file_id,
            filename=file.filename or safe_name,
            content_type=file.content_type,
            path=str(dest),
            size=len(data),
        )
        return {
            "ok": True,
            "file": _file_dict(store.get_file(file_id)),
        }

    async def _gem_arg(client: Any, request: GeminiGenerateRequest) -> str | None:
        if request.gem:
            return request.gem
        if request.gem_id:
            return request.gem_id
        if request.gem_name:
            gems = await client.fetch_gems()
            gem = gems.get(name=request.gem_name)
            if gem is None:
                raise ValueError(f"Gem not found: {request.gem_name}")
            return gem.id
        return None

    def _file_paths(file_ids: list[str]) -> list[str]:
        paths: list[str] = []
        for file_id in file_ids:
            record = store.get_file(file_id)
            if record is None:
                raise ValueError(f"File not found: {file_id}")
            paths.append(record.path)
        return paths

    def _save_media_index(
        *,
        request_id: str,
        account_id: int | None,
        output: Any,
    ) -> int:
        count = 0
        for item in _media_entries(output):
            store.add_media_output(
                request_id=request_id,
                account_id=account_id,
                kind=item["kind"],
                title=item.get("title"),
                url=item["url"],
                thumbnail=item.get("thumbnail"),
                metadata=item,
            )
            count += 1
        return count

    @app.post("/v1/gemini/generate")
    async def gemini_generate(request: GeminiGenerateRequest) -> dict[str, Any]:
        request_id = f"req-{uuid.uuid4().hex}"

        async def operation(client):
            kwargs: dict[str, Any] = {
                "temporary": request.temporary,
                "deep_research": request.deep_research,
            }
            resolved_model = _resolve_model_arg(request.model)
            if resolved_model:
                kwargs["model"] = resolved_model
            gem_arg = await _gem_arg(client, request)
            if gem_arg:
                kwargs["gem"] = gem_arg
            files = _file_paths(request.file_ids)
            if files:
                kwargs["files"] = files
            return await client.generate_content(request.prompt, **kwargs)

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/gemini/generate",
                model=request.model or "gemini",
                output_type="gemini_native",
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        account_id = rotator.status()["current_account_id"]
        media_count = _save_media_index(
            request_id=request_id, account_id=account_id, output=output
        )
        job = None
        if output.deep_research_plan:
            job_id = output.deep_research_plan.research_id or f"dr-{uuid.uuid4().hex}"
            store.upsert_job(
                job_id=job_id,
                job_type="deep_research",
                state="planned",
                account_id=account_id,
                model=request.model or "gemini",
                prompt=request.prompt,
                plan=_dump_model(output.deep_research_plan),
            )
            job = _job_dict(store.get_job(job_id))

        return {
            "ok": True,
            "account": account_id,
            "model": request.model or "gemini",
            "metadata": output.metadata,
            "output": _classified_output(output),
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "job": job,
            "request_id": request_id,
            "media_count": media_count,
        }

    @app.post("/v1/gemini/stream")
    async def gemini_stream(request: GeminiGenerateRequest):
        request_id = f"req-{uuid.uuid4().hex}"

        async def event_stream():
            final_output = None

            async def operation(client):
                kwargs: dict[str, Any] = {
                    "temporary": request.temporary,
                    "deep_research": request.deep_research,
                }
                resolved_model = _resolve_model_arg(request.model)
                if resolved_model:
                    kwargs["model"] = resolved_model
                gem_arg = await _gem_arg(client, request)
                if gem_arg:
                    kwargs["gem"] = gem_arg
                files = _file_paths(request.file_ids)
                if files:
                    kwargs["files"] = files
                async for output in client.generate_content_stream(request.prompt, **kwargs):
                    yield output

            try:
                async for output in rotator.run_stream(
                    operation,
                    endpoint="/v1/gemini/stream",
                    model=request.model or "gemini",
                    output_type="gemini_native",
                ):
                    final_output = output
                    chunk = {
                        "type": "delta",
                        "text_delta": output.text_delta,
                        "thoughts_delta": output.thoughts_delta,
                        "metadata": output.metadata,
                    }
                    yield f"data: {json.dumps(chunk).decode()}\n\n"
            except Exception as exc:
                error = {"ok": False, "error": str(exc), "status": _error_status(exc)}
                yield f"data: {json.dumps(error).decode()}\n\n"
                yield "data: [DONE]\n\n"
                return

            if final_output is not None:
                account_id = rotator.status()["current_account_id"]
                media_count = _save_media_index(
                    request_id=request_id,
                    account_id=account_id,
                    output=final_output,
                )
                final = {
                    "type": "final",
                    "ok": True,
                    "account": account_id,
                    "model": request.model or "gemini",
                    "metadata": final_output.metadata,
                    "output": _classified_output(final_output),
                    "request_id": request_id,
                    "media_count": media_count,
                }
                yield f"data: {json.dumps(final).decode()}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/v1/gemini/gems")
    async def list_gems(include_hidden: bool = False) -> dict[str, Any]:
        async def operation(client):
            return await client.fetch_gems(include_hidden=include_hidden)

        try:
            gems = await rotator.run(
                operation,
                count_usage=False,
                count_failure=False,
                endpoint="/v1/gemini/gems",
                output_type="gems",
            )
        except Exception as exc:
            cached = store.list_gems_cache()
            if cached:
                return {
                    "ok": True,
                    "cached": True,
                    "gems": [item.__dict__ for item in cached],
                    "warning": str(exc),
                }
            return {"ok": False, "cached": True, "gems": [], "warning": str(exc)}
        gem_list = [_gem_dict(gem) for gem in gems]
        store.replace_gems_cache(gem_list)
        return {"ok": True, "cached": False, "gems": gem_list}

    @app.post("/v1/gemini/gems")
    async def create_gem(request: GemRequest) -> dict[str, Any]:
        async def operation(client):
            return await client.create_gem(
                name=request.name,
                prompt=request.prompt,
                description=request.description,
            )

        try:
            gem = await rotator.run(
                operation,
                count_usage=False,
                endpoint="/v1/gemini/gems",
                output_type="gem",
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {"ok": True, "gem": _gem_dict(gem)}

    @app.patch("/v1/gemini/gems/{gem_id}")
    async def update_gem(gem_id: str, request: GemRequest) -> dict[str, Any]:
        async def operation(client):
            return await client.update_gem(
                gem=gem_id,
                name=request.name,
                prompt=request.prompt,
                description=request.description,
            )

        try:
            gem = await rotator.run(
                operation,
                count_usage=False,
                endpoint="/v1/gemini/gems",
                output_type="gem",
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {"ok": True, "gem": _gem_dict(gem)}

    @app.delete("/v1/gemini/gems/{gem_id}")
    async def delete_gem(gem_id: str) -> dict[str, Any]:
        async def operation(client):
            await client.delete_gem(gem_id)
            return True

        try:
            await rotator.run(
                operation,
                count_usage=False,
                endpoint="/v1/gemini/gems",
                output_type="gem",
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/gemini/deep-research/plan")
    async def create_deep_research_plan(
        request: DeepResearchCreateRequest,
    ) -> dict[str, Any]:
        job_id = f"dr-{uuid.uuid4().hex}"

        async def operation(client):
            resolved_model = _resolve_model_arg(request.model) or Model.UNSPECIFIED
            return await client.create_deep_research_plan(
                request.prompt,
                model=resolved_model,
            )

        try:
            plan = await rotator.run(
                operation,
                endpoint="/v1/gemini/deep-research/plan",
                model=request.model or "gemini",
                output_type="deep_research",
                job_id=job_id,
                deep_research_state="planned",
            )
        except Exception as exc:
            store.upsert_job(
                job_id=job_id,
                job_type="deep_research",
                state="failed",
                model=request.model or "gemini",
                prompt=request.prompt,
                error=str(exc),
            )
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        if plan.research_id:
            job_id = plan.research_id
        store.upsert_job(
            job_id=job_id,
            job_type="deep_research",
            state="planned",
            account_id=rotator.status()["current_account_id"],
            model=request.model or "gemini",
            prompt=request.prompt,
            plan=_dump_model(plan),
        )
        return {
            "ok": True,
            "job": _job_dict(store.get_job(job_id)),
            "plan": _dump_model(plan),
        }

    @app.post("/v1/gemini/deep-research/start")
    async def start_deep_research(request: DeepResearchStartRequest) -> dict[str, Any]:
        if request.plan is None and request.job_id is None:
            raise HTTPException(status_code=400, detail="job_id or plan is required.")
        job = store.get_job(request.job_id) if request.job_id else None
        plan_data = request.plan or (job.plan_json if job else None)
        if not plan_data:
            raise HTTPException(status_code=404, detail="Deep research plan not found.")
        plan = DeepResearchPlan(**plan_data)
        job_id = request.job_id or plan.research_id or f"dr-{uuid.uuid4().hex}"

        async def operation(client):
            return await client.start_deep_research(
                plan,
                confirm_prompt=request.confirm_prompt,
            )

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/gemini/deep-research/start",
                output_type="deep_research",
                job_id=job_id,
                deep_research_state="running",
            )
        except Exception as exc:
            store.upsert_job(
                job_id=job_id,
                job_type="deep_research",
                state="failed",
                plan=plan_data,
                error=str(exc),
            )
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        store.upsert_job(
            job_id=job_id,
            job_type="deep_research",
            state="running",
            account_id=rotator.status()["current_account_id"],
            plan=plan_data,
            result={"start_output": _classified_output(output), "metadata": output.metadata},
        )
        return {
            "ok": True,
            "job": _job_dict(store.get_job(job_id)),
            "output": _classified_output(output),
        }

    @app.get("/v1/gemini/deep-research/{job_id}/status")
    async def deep_research_status(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")

        async def operation(client):
            return await client.get_deep_research_status(job_id)

        try:
            status_obj = await rotator.run(
                operation,
                count_usage=False,
                endpoint="/v1/gemini/deep-research/status",
                output_type="deep_research",
                job_id=job_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        status_data = _dump_model(status_obj)
        if status_obj:
            store.upsert_job(
                job_id=job_id,
                job_type="deep_research",
                state="done" if status_obj.done else status_obj.state,
                result={"status": status_data, **(job.result_json or {})},
            )
        return {"ok": True, "job": _job_dict(store.get_job(job_id)), "status": status_data}

    @app.post("/v1/gemini/deep-research/wait")
    async def wait_deep_research(request: DeepResearchWaitRequest) -> dict[str, Any]:
        job = store.get_job(request.job_id)
        if not job or not job.plan_json:
            raise HTTPException(status_code=404, detail="Deep research plan not found.")
        plan = DeepResearchPlan(**job.plan_json)

        async def operation(client):
            return await client.wait_for_deep_research(
                plan,
                poll_interval=request.poll_interval,
                timeout=request.timeout,
            )

        try:
            result = await rotator.run(
                operation,
                endpoint="/v1/gemini/deep-research/wait",
                output_type="deep_research",
                job_id=request.job_id,
                deep_research_state="waiting",
            )
        except Exception as exc:
            store.upsert_job(
                job_id=request.job_id,
                job_type="deep_research",
                state="failed",
                error=str(exc),
            )
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        result_data = _dump_model(result)
        store.upsert_job(
            job_id=request.job_id,
            job_type="deep_research",
            state="done" if result.done else "timeout",
            result=result_data,
        )
        return {"ok": True, "job": _job_dict(store.get_job(request.job_id)), "result": result_data}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "google",
                }
                for model_id in _openai_model_ids()
            ],
        }

    @app.post("/v1/accounts")
    async def add_account(request: AccountRequest) -> dict[str, Any]:
        cookies = dict(request.cookies)
        cookies["__Secure-1PSID"] = request.secure_1psid
        if request.secure_1psidts:
            cookies["__Secure-1PSIDTS"] = request.secure_1psidts
        account = store.upsert_account(
            name=request.name,
            secure_1psid=request.secure_1psid,
            secure_1psidts=request.secure_1psidts,
            cookies=cookies,
            enabled=request.enabled,
        )
        validation = await rotator.validate_account(account.id)
        return {
            "ok": True,
            "validation": validation,
            "accounts": rotator.status()["accounts"],
        }

    @app.post("/v1/accounts/import")
    async def import_accounts() -> dict[str, Any]:
        imported = store.import_accounts_file(config.accounts_file)
        return {"ok": True, "imported": imported, "accounts": rotator.status()["accounts"]}

    @app.get("/v1/accounts/export")
    async def export_accounts() -> dict[str, Any]:
        return {
            "accounts": [
                {
                    "name": account.name,
                    "__Secure-1PSID": account.secure_1psid,
                    "__Secure-1PSIDTS": account.secure_1psidts,
                    "cookies": account.cookies,
                    "enabled": account.enabled,
                    "expired": account.expired,
                }
                for account in store.list_accounts()
            ]
        }

    @app.post("/v1/accounts/switch")
    async def switch_account(request: SwitchAccountRequest) -> dict[str, Any]:
        try:
            if request.account_id is None:
                account = await rotator.switch_next()
            else:
                account = await rotator.switch_to(request.account_id)
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {"ok": True, "current_account_id": account.id, "status": rotator.status()}

    @app.post("/v1/accounts/validate")
    async def validate_current_account() -> dict[str, Any]:
        try:
            result = await rotator.validate_account()
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {"ok": True, "validation": result, "accounts": rotator.status()["accounts"]}

    @app.post("/v1/accounts/validate-all")
    async def validate_all_accounts() -> dict[str, Any]:
        results = await rotator.validate_accounts()
        return {"ok": True, "validations": results, "accounts": rotator.status()["accounts"]}

    @app.post("/v1/accounts/{account_id}/validate")
    async def validate_account(account_id: int) -> dict[str, Any]:
        try:
            result = await rotator.validate_account(account_id)
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {"ok": True, "validation": result, "accounts": rotator.status()["accounts"]}

    @app.patch("/v1/accounts/{account_id}")
    async def update_account(
        account_id: int, request: AccountToggleRequest
    ) -> dict[str, Any]:
        if not store.set_account_enabled(account_id, request.enabled):
            raise HTTPException(status_code=404, detail="Account not found.")
        return {"ok": True, "accounts": rotator.status()["accounts"]}

    @app.delete("/v1/accounts/{account_id}")
    async def delete_account(account_id: int) -> dict[str, Any]:
        if not store.delete_account(account_id):
            raise HTTPException(status_code=404, detail="Account not found.")
        return {"ok": True, "accounts": rotator.status()["accounts"]}

    @app.post("/v1/auth/session")
    async def start_auth_session() -> dict[str, Any]:
        try:
            return await auth_browser.start_session()
        except AuthBrowserUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.delete("/v1/auth/session")
    async def close_auth_session() -> dict[str, Any]:
        await auth_browser.close_session()
        return {"ok": True}

    @app.get("/v1/auth/screenshot")
    async def auth_screenshot() -> Response:
        try:
            image = await auth_browser.screenshot()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=image, media_type="image/png")

    @app.post("/v1/auth/click")
    async def auth_click(request: AuthClickRequest) -> dict[str, Any]:
        try:
            return await auth_browser.click(request.x, request.y)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/type")
    async def auth_type(request: AuthTypeRequest) -> dict[str, Any]:
        try:
            return await auth_browser.type_text(request.text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/press")
    async def auth_press(request: AuthPressRequest) -> dict[str, Any]:
        try:
            return await auth_browser.press(request.key)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/save")
    async def auth_save(request: AuthSaveRequest) -> dict[str, Any]:
        try:
            result = await auth_browser.save_account(name=request.name)
            validation = await rotator.validate_account(result["account_id"])
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**result, "validation": validation, "accounts": rotator.status()["accounts"]}

    @app.post("/v1/generate")
    async def generate(request: GenerateRequest) -> dict[str, Any]:
        async def operation(client):
            kwargs: dict[str, Any] = {"temporary": request.temporary}
            resolved_model = _resolve_model_arg(request.model)
            if resolved_model:
                kwargs["model"] = resolved_model
            return await client.generate_content(request.prompt, **kwargs)

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/generate",
                model=request.model or "gemini",
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        return {
            "text": output.text,
            "metadata": output.metadata,
            "account": rotator.status()["current_account_id"],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest):
        prompt = _messages_to_prompt(request.messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="messages must contain text.")
        prompt = _append_tool_instructions(prompt, request)
        model = request.model or "gemini"

        if request.stream:
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"

            async def event_stream():
                first = _chat_chunk(completion_id, model, role="assistant")
                yield f"data: {json.dumps(first).decode()}\n\n"
                buffered_text: list[str] = []

                async def operation(client):
                    kwargs: dict[str, Any] = {}
                    resolved_model = _resolve_model_arg(request.model)
                    if resolved_model:
                        kwargs["model"] = resolved_model
                    async for output in client.generate_content_stream(prompt, **kwargs):
                        yield output

                try:
                    async for output in rotator.run_stream(
                        operation,
                        endpoint="/v1/chat/completions",
                        model=model,
                    ):
                        delta = output.text_delta or ""
                        if delta:
                            if _tools_enabled(request):
                                buffered_text.append(delta)
                                continue
                            chunk = _chat_chunk(completion_id, model, content=delta)
                            yield f"data: {json.dumps(chunk).decode()}\n\n"
                except Exception as exc:
                    error = _openai_error(str(exc), _error_status(exc))
                    yield f"data: {json.dumps(error).decode()}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                finish_reason = "stop"
                if _tools_enabled(request):
                    text = "".join(buffered_text)
                    tool_calls = _tool_calls_from_output_text(text, request.tools)
                    if tool_calls:
                        tool_chunk = _chat_tool_calls_chunk(completion_id, model, tool_calls)
                        yield f"data: {json.dumps(tool_chunk).decode()}\n\n"
                        finish_reason = "tool_calls"
                    elif text:
                        chunk = _chat_chunk(completion_id, model, content=text)
                        yield f"data: {json.dumps(chunk).decode()}\n\n"
                final = _chat_chunk(completion_id, model, finish_reason=finish_reason)
                yield f"data: {json.dumps(final).decode()}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        async def operation(client):
            kwargs: dict[str, Any] = {}
            resolved_model = _resolve_model_arg(request.model)
            if resolved_model:
                kwargs["model"] = resolved_model
            return await client.generate_content(prompt, **kwargs)

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/chat/completions",
                model=model,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        created = int(time.time())
        tool_calls = (
            _tool_calls_from_output_text(output.text, request.tools)
            if _tools_enabled(request)
            else []
        )
        message: dict[str, Any] = {"role": "assistant", "content": output.text}
        finish_reason = "stop"
        if tool_calls:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            }
            finish_reason = "tool_calls"
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    config = ServerConfig.from_env()
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
    )


if __name__ == "__main__":
    main()
