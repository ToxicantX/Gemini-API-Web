from __future__ import annotations

import mimetypes
import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import orjson as json
import websockets
import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..constants import Model
from ..exceptions import (
    AuthError,
    GeminiError,
    MediaGenerationEmptyResult,
    MediaGenerationTemporarilyUnavailable,
    ModelInvalid,
    VideoGenerationFailed,
    VideoGenerationNotSubmitted,
)
from ..types import DeepResearchPlan
from .auth_browser import AuthBrowserManager, AuthBrowserUnavailable
from .config import ServerConfig
from .database import AccountStore
from .object_storage import (
    ObjectStorageConfig,
    build_media_object_key,
    upload_s3_compatible,
)
from .rotator import AccountRotator


MEDIA_CONTENT_MAX_BYTES = 100 * 1024 * 1024
MEDIA_CONTENT_ALLOWED_HOST_SUFFIXES = (
    ".google.com",
    ".googleusercontent.com",
    ".usercontent.google.com",
    ".gstatic.com",
    ".googlevideo.com",
    "storage.googleapis.com",
)
SYSTEM_SETTINGS_KEY = "system_settings"
MASKED_SECRET = "********"
DEFAULT_SYSTEM_SETTINGS = {
    "api_keys": [],
    "object_storage": {
        "enabled": False,
        "endpoint": "",
        "region": "auto",
        "bucket": "",
        "access_key_id": "",
        "secret_access_key": "",
        "prefix": "gemini-web",
        "public_url": "",
        "force_path_style": True,
    },
}
ADMIN_LOGIN_FAILURE_LIMIT = 5
ADMIN_LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60


class GenerateRequest(BaseModel):
    prompt: str
    model: str | None = None
    mode: str | None = None
    temporary: bool = False


class GeminiGenerateRequest(BaseModel):
    prompt: str
    model: str | None = None
    mode: str | None = None
    temporary: bool = False
    store_media: bool = False
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


class ClearMediaCooldownRequest(BaseModel):
    kind: str | None = None


class ObjectStorageSettings(BaseModel):
    enabled: bool = False
    endpoint: str = ""
    region: str = "auto"
    bucket: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    prefix: str = "gemini-web"
    public_url: str = ""
    force_path_style: bool = True


class SystemSettingsRequest(BaseModel):
    api_keys: list[str] | None = None
    object_storage: ObjectStorageSettings | None = None


class AdminLoginRequest(BaseModel):
    username: str | None = None
    password: str


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


class ResponseFormatSpec(BaseModel):
    type: str
    json_schema: dict[str, Any] | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    tools: list[ChatToolSpec] | None = None
    tool_choice: str | dict[str, Any] | None = None
    functions: list[FunctionToolSpec] | None = None
    function_call: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormatSpec | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None
    store: bool | None = None


class CompletionRequest(BaseModel):
    model: str | None = None
    prompt: str | list[str]
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    suffix: str | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None
    store: bool | None = None


class ResponsesRequest(BaseModel):
    model: str | None = None
    input: str | list[Any]
    instructions: str | None = None
    text: dict[str, Any] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    tools: list[ChatToolSpec] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None
    store: bool | None = None


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: str | None = None
    n: int | None = None
    size: str | None = None
    quality: str | None = None
    response_format: str | None = None
    store_media: bool = False


MODEL_ALIASES = {
    "gemini": "gemini-3.1-pro",
}

PUBLIC_MODEL_IDS = {
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
}

PUBLIC_MODEL_ORDER = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-pro",
]

OPENAI_IMAGE_MODEL_ALIASES = {
    "gpt-image-1",
    "gpt-image-2",
    "dall-e-2",
    "dall-e-3",
}

REMOVED_MODEL_IDS = {
    "gemini-3-pro",
    "gemini-3-pro-preview",
    "gemini-3.1-pro-preview",
    "gemini-3-flash",
    "gemini-3-flash-preview",
    "gemini-3-flash-thinking",
}


def _message_content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type in {"text", "input_text", "output_text", "summary_text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item_type in {"image_url", "input_image"}:
            # OpenAI 多模态消息里的图片 URL 转成明确的文本引用，避免外部客户端传图时被静默丢弃。
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, str):
                url = image_url
            elif isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                url = image_url["url"]
            elif isinstance(item.get("url"), str):
                url = item["url"]
            if url:
                parts.append(f"Image URL: {url}")
        elif item_type in {"input_file", "file"}:
            file_id = item.get("file_id") or item.get("id")
            if isinstance(file_id, str) and file_id:
                parts.append(f"Attached file: {file_id}")
        elif item_type == "input_audio":
            # OpenAI 多模态音频既可能引用已上传文件，也可能直接给 URL；这里转成 Gemini 可理解的附件提示。
            audio = item.get("input_audio")
            file_id = item.get("file_id")
            url = item.get("url")
            if isinstance(audio, dict):
                file_id = file_id or audio.get("file_id")
                url = url or audio.get("url")
            if isinstance(file_id, str) and file_id:
                parts.append(f"Attached audio file: {file_id}")
            if isinstance(url, str) and url:
                parts.append(f"Audio URL: {url}")
    return "\n".join(parts)


def _message_content_file_ids(content: str | list[dict[str, Any]] | None) -> list[str]:
    if not isinstance(content, list):
        return []
    file_ids: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type not in {"input_file", "file", "input_audio"}:
            continue
        audio = item.get("input_audio") if item_type == "input_audio" else None
        file_id = item.get("file_id") or item.get("id")
        if isinstance(audio, dict):
            file_id = file_id or audio.get("file_id")
        if isinstance(file_id, str) and file_id:
            file_ids.append(file_id)
    return file_ids


def _messages_file_ids(messages: list[ChatMessage]) -> list[str]:
    seen: set[str] = set()
    file_ids: list[str] = []
    for message in messages:
        for file_id in _message_content_file_ids(message.content):
            if file_id in seen:
                continue
            seen.add(file_id)
            file_ids.append(file_id)
    return file_ids


def _messages_to_prompt(messages: list[ChatMessage]) -> str:
    prompt_parts: list[str] = []
    for message in messages:
        text = _message_content_to_text(message.content)
        role = message.role.lower()
        if role in {"system", "developer"}:
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


def _responses_input_to_messages(input_value: str | list[Any]) -> list[ChatMessage]:
    """将 OpenAI Responses API 的 input 字段转换成现有 Chat Completions 消息。"""
    if isinstance(input_value, str):
        return [ChatMessage(role="user", content=input_value)]
    messages: list[ChatMessage] = []
    for item in input_value:
        if isinstance(item, str):
            messages.append(ChatMessage(role="user", content=item))
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            output = item.get("output")
            if isinstance(output, str):
                content = output
            else:
                content = json.dumps(output).decode()
            messages.append(
                ChatMessage(
                    role="tool",
                    content=content,
                    tool_call_id=item.get("call_id") or item.get("id"),
                    name=item.get("name"),
                )
            )
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, str) or content is None:
            messages.append(ChatMessage(role=role, content=content))
        elif isinstance(content, list):
            parts = [part for part in content if isinstance(part, dict)]
            messages.append(ChatMessage(role=role, content=parts))
    return messages


def _response_format_from_responses_text(text_options: dict[str, Any] | None) -> ResponseFormatSpec | None:
    if not isinstance(text_options, dict):
        return None
    raw_format = text_options.get("format")
    if raw_format is None:
        return None
    if isinstance(raw_format, str):
        return ResponseFormatSpec(type=raw_format)
    if isinstance(raw_format, dict):
        format_type = raw_format.get("type")
        if not isinstance(format_type, str):
            raise ValueError("text.format.type is required.")
        return ResponseFormatSpec(
            type=format_type,
            json_schema=raw_format.get("json_schema") or raw_format.get("schema"),
        )
    raise ValueError("text.format must be a string or object.")


def _responses_messages(request: ResponsesRequest) -> list[ChatMessage]:
    messages = _responses_input_to_messages(request.input)
    if request.instructions:
        return [
            ChatMessage(role="system", content=request.instructions),
            *messages,
        ]
    return messages


def _responses_prompt(request: ResponsesRequest) -> str:
    messages = _responses_messages(request)
    prompt = _messages_to_prompt(messages)
    response_format = _response_format_from_responses_text(request.text)
    if response_format is not None:
        shim_request = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="placeholder")],
            response_format=response_format,
        )
        prompt = _append_response_format_instructions(prompt, shim_request)
    if request.tools:
        # Responses API 的工具入参复用 Chat Completions 的提示词兼容层，保持两套接口行为一致。
        prompt = _append_tool_instructions(
            prompt,
            ChatCompletionRequest(
                messages=[ChatMessage(role="user", content="placeholder")],
                tools=request.tools,
                tool_choice=request.tool_choice,
                parallel_tool_calls=request.parallel_tool_calls,
            ),
        )
    return _append_responses_token_limit_instruction(prompt, request)


def _responses_output(
    *,
    response_id: str,
    model: str,
    text: str,
    tool_calls: list[dict[str, Any]] | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    """返回 OpenAI Responses API 的基础响应结构。"""
    output: list[dict[str, Any]] = []
    if tool_calls:
        for call in tool_calls:
            function = call.get("function") or {}
            call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
            output.append(
                {
                    "id": call_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}"),
                }
            )
        output_text = ""
    else:
        output_id = f"msg_{uuid.uuid4().hex}"
        content_id = f"out_{uuid.uuid4().hex}"
        output_text = text
        output.append(
            {
                "id": output_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "id": content_id,
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        )
    return {
        "id": response_id,
        "object": "response",
        "created_at": created or int(time.time()),
        "status": "completed",
        "model": model,
        "output_text": output_text,
        "output": output,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _responses_stream_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data).decode()}\n\n"


def _sse_error_event(data: dict[str, Any]) -> str:
    # EventSource 客户端可以直接监听 error 事件；data 仍保持原有 JSON，兼容逐行解析客户端。
    return f"event: error\ndata: {json.dumps(data).decode()}\n\n"


def _sse_response(event_stream: Any) -> StreamingResponse:
    """统一 SSE 响应头，减少反向代理或浏览器客户端缓冲流式数据。"""
    return StreamingResponse(
        event_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _openai_image_generation_output(
    media_items: list[Any],
    *,
    revised_prompt: str | None = None,
    response_format: str | None = None,
    media_content_loader: Any | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in media_items:
        media = _media_record_dict(item)
        if response_format == "b64_json":
            if media_content_loader is None:
                continue
            content = media_content_loader(item)
            if not content:
                continue
            row: dict[str, Any] = {
                "b64_json": base64.b64encode(content).decode("ascii"),
            }
        else:
            url = media.get("content_url") or media.get("url")
            if not url:
                continue
            if base_url and isinstance(url, str) and url.startswith("/"):
                url = f"{base_url.rstrip('/')}{url}"
            row = {"url": url}
        if revised_prompt:
            row["revised_prompt"] = revised_prompt
        data.append(row)
    return {"created": int(time.time()), "data": data}


def _audio_srt_text(text: str) -> str:
    # Gemini Web 无稳定音频时间戳；SRT/VTT 兼容返回使用零时长整段文本。
    return f"1\n00:00:00,000 --> 00:00:00,000\n{text}\n"


def _audio_vtt_text(text: str) -> str:
    return f"WEBVTT\n\n00:00:00.000 --> 00:00:00.000\n{text}\n"


def _normalized_chat_request_tools(
    request: ChatCompletionRequest,
) -> tuple[list[ChatToolSpec], str | dict[str, Any] | None]:
    # 兼容 OpenAI 旧版 functions/function_call 入参，内部统一走新版 tools/tool_choice 逻辑。
    tools = list(request.tools or [])
    if not tools and request.functions:
        tools = [ChatToolSpec(type="function", function=function) for function in request.functions]
    tool_choice = request.tool_choice
    if tool_choice is None and request.function_call is not None:
        if isinstance(request.function_call, dict):
            name = request.function_call.get("name")
            if isinstance(name, str) and name:
                tool_choice = {"type": "function", "function": {"name": name}}
        else:
            tool_choice = request.function_call
    return tools, tool_choice


def _tools_enabled(request: ChatCompletionRequest) -> bool:
    tools, tool_choice = _normalized_chat_request_tools(request)
    if not tools:
        return False
    return tool_choice != "none"


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
    tools, tool_choice = _normalized_chat_request_tools(request)
    if not tools or tool_choice == "none":
        return prompt
    forced_name = _tool_choice_name(tool_choice)
    choice_line = "If no tool is needed, answer normally."
    if tool_choice == "required":
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


def _append_response_format_instructions(prompt: str, request: ChatCompletionRequest) -> str:
    response_format = request.response_format
    if response_format is None or response_format.type == "text":
        return prompt
    if response_format.type not in {"json_object", "json_schema"}:
        raise ValueError("response_format.type must be one of: text, json_object, json_schema.")
    lines = [
        "JSON response mode is enabled.",
        "Respond with only valid JSON. Do not wrap the JSON in markdown. Do not include natural language outside JSON.",
    ]
    if response_format.type == "json_schema" and response_format.json_schema:
        lines.append("The response must follow this JSON schema:")
        lines.append(json.dumps(response_format.json_schema).decode())
    return f"{prompt}\n\nSystem: {' '.join(lines)}"


def _append_chat_token_limit_instruction(prompt: str, request: ChatCompletionRequest) -> str:
    # Gemini Web 没有稳定的 max_tokens 入参，这里通过系统提示兼容 OpenAI 客户端的长度约束。
    limit = request.max_completion_tokens or request.max_tokens
    if limit is None:
        return prompt
    try:
        value = int(limit)
    except Exception:
        return prompt
    if value <= 0:
        return prompt
    return f"{prompt}\n\nSystem: Keep the assistant response within approximately {value} tokens."


def _append_responses_token_limit_instruction(prompt: str, request: ResponsesRequest) -> str:
    # Responses API 常用 max_output_tokens；部分兼容客户端仍会传 max_tokens。
    limit = request.max_output_tokens or request.max_tokens
    if limit is None:
        return prompt
    try:
        value = int(limit)
    except Exception:
        return prompt
    if value <= 0:
        return prompt
    return f"{prompt}\n\nSystem: Keep the final response within approximately {value} tokens."


def _append_completion_token_limit_instruction(prompt: str, request: CompletionRequest) -> str:
    if request.max_tokens is None:
        return prompt
    try:
        value = int(request.max_tokens)
    except Exception:
        return prompt
    if value <= 0:
        return prompt
    return f"{prompt}\n\nSystem: Keep the completion within approximately {value} tokens."


def _append_sampling_parameter_instructions(prompt: str, request: Any) -> str:
    # Gemini Web API 没有稳定公开的采样参数入口，这里把 OpenAI 常见参数转成提示词约束。
    lines: list[str] = []
    if getattr(request, "temperature", None) is not None:
        lines.append(f"temperature={getattr(request, 'temperature')}")
    if getattr(request, "top_p", None) is not None:
        lines.append(f"top_p={getattr(request, 'top_p')}")
    if getattr(request, "presence_penalty", None) is not None:
        lines.append(f"presence_penalty={getattr(request, 'presence_penalty')}")
    if getattr(request, "frequency_penalty", None) is not None:
        lines.append(f"frequency_penalty={getattr(request, 'frequency_penalty')}")
    if getattr(request, "seed", None) is not None:
        lines.append(f"seed={getattr(request, 'seed')}")
    if not lines:
        return prompt
    return f"{prompt}\n\nSystem: Follow these caller generation preferences when possible: {', '.join(lines)}."


def _stop_sequences(stop: str | list[str] | None) -> list[str]:
    if stop is None:
        return []
    values = [stop] if isinstance(stop, str) else stop
    return [value for value in values if isinstance(value, str) and value]


def _apply_stop_sequences(text: str, stop: str | list[str] | None) -> tuple[str, bool]:
    earliest: int | None = None
    for sequence in _stop_sequences(stop):
        index = text.find(sequence)
        if index < 0:
            continue
        if earliest is None or index < earliest:
            earliest = index
    if earliest is None:
        return text, False
    return text[:earliest], True


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


def _legacy_function_call_from_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    return {
        "name": function.get("name", ""),
        "arguments": function.get("arguments", "{}"),
    }


def _openai_model_ids() -> list[str]:
    return [
        "gemini",
        *PUBLIC_MODEL_ORDER,
    ]


def _openai_model_object(model_id: str, created: int | None = None) -> dict[str, Any]:
    """返回 OpenAI 兼容模型对象，供模型列表和单模型查询共用。"""
    return {
        "id": model_id,
        "object": "model",
        "created": created or int(time.time()),
        "owned_by": "google",
    }


def _openai_engine_object(model_id: str, created: int | None = None) -> dict[str, Any]:
    # 旧版 OpenAI SDK 会探测 /engines；这里只做只读别名，不扩展真实模型映射。
    return {
        "id": model_id,
        "object": "engine",
        "created": created or int(time.time()),
        "owner": "google",
        "ready": True,
    }


def _resolve_model_arg(model: str | None) -> str | None:
    if not model:
        return None
    # 外部客户端配置模型名时偶尔会带大小写或首尾空白，这里做轻量归一化但不兼容旧模型映射。
    model_key = model.strip().lower()
    if not model_key:
        return None
    if model_key == "unspecified":
        return None
    if model_key in REMOVED_MODEL_IDS:
        raise ValueError(
            f"Model '{model}' is no longer exposed. Use gemini-3.1-pro, gemini-3.5-flash, or gemini-3.1-flash-lite."
        )
    resolved = MODEL_ALIASES.get(model_key, model_key)
    if resolved not in PUBLIC_MODEL_IDS:
        raise ValueError(
            f"Unsupported model '{model}'. Use gemini, gemini-3.1-pro, gemini-3.5-flash, or gemini-3.1-flash-lite."
        )
    return resolved


def _resolve_openai_image_model_arg(model: str | None) -> str | None:
    """解析 OpenAI 图片端点的模型名；兼容图片模型别名但不加入公开模型列表。"""
    model_key = (model or "").strip().lower()
    if model_key in OPENAI_IMAGE_MODEL_ALIASES:
        return _resolve_model_arg("gemini")
    return _resolve_model_arg(model)


def _generation_mode_arg(mode: str | None) -> str | None:
    normalized = (mode or "").strip().lower()
    if not normalized:
        return None
    if normalized not in {"image", "video", "audio"}:
        raise ValueError("mode must be one of: image, video, audio.")
    return normalized


def _ensure_media_generation_result(output: Any, mode: str | None) -> None:
    if not mode:
        return
    classified = _classified_output(output)
    has_result = {
        "image": bool(classified["images"]),
        "video": bool(classified["videos"]),
        "audio": bool(classified["media"]),
    }[mode]
    if has_result:
        return
    labels = {"image": "图片", "video": "视频", "audio": "音频"}
    # 上游 2xx 但没有媒体结果时必须显式失败，避免调用方把文本/JSON 当成成功媒体任务。
    raise MediaGenerationEmptyResult(
        f"{labels[mode]}生成请求已返回，但响应中没有可用的{labels[mode]}结果。"
    )


def _error_status(exc: Exception) -> int:
    if isinstance(exc, AuthError):
        return 401
    if isinstance(exc, (ValueError, ModelInvalid)):
        return 400
    if isinstance(exc, VideoGenerationNotSubmitted):
        return 409
    if isinstance(exc, VideoGenerationFailed):
        return 502
    if isinstance(exc, MediaGenerationTemporarilyUnavailable):
        return 429
    if isinstance(exc, MediaGenerationEmptyResult):
        return 502
    if isinstance(exc, GeminiError):
        return 502
    return 500


def _openai_error(
    message: str,
    status_code: int,
    error_type: str = "api_error",
    request_id: str | None = None,
) -> dict:
    error = {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": status_code,
        }
    }
    if request_id:
        # 外部客户端有时只记录 JSON 正文，这里和响应头同时暴露请求号，方便回查请求日志。
        error["request_id"] = request_id
        error["error"]["request_id"] = request_id
    return error


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


def _chat_zero_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _completion_zero_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _chat_usage_chunk(completion_id: str, model: str) -> dict[str, Any]:
    """OpenAI 流式 include_usage 会在结束前发送一个 choices 为空的 usage chunk。"""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": _chat_zero_usage(),
    }


def _completion_chunk(
    completion_id: str,
    model: str,
    text: str = "",
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "text_completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "text": text,
                "finish_reason": finish_reason,
            }
        ],
    }


def _completion_usage_chunk(completion_id: str, model: str) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "text_completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": _completion_zero_usage(),
    }


def _stream_include_usage(stream_options: dict[str, Any] | None) -> bool:
    return bool(isinstance(stream_options, dict) and stream_options.get("include_usage"))


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


def _media_record_dict(item: Any) -> dict[str, Any]:
    data = item.__dict__.copy()
    if "token" not in data and hasattr(item, "token"):
        data["token"] = getattr(item, "token")
    storage = (data.get("metadata") or {}).get("object_storage") or {}
    if data.get("token"):
        data["content_url"] = (
            storage.get("url") or f"/v1/gemini/media/{data['token']}/content"
        )
        data["cached"] = bool(data.get("local_path"))
        data["stored"] = bool(storage.get("url"))
    return data


def _public_media_content_path(path: str) -> bool:
    # OpenAI 图片接口返回的媒体代理链接会被外部客户端直接请求，这里只公开随机 token 的内容下载。
    return path.startswith("/v1/gemini/media/") and path.endswith("/content")


def _external_api_path(path: str) -> bool:
    """判断哪些接口属于外部 API Key 调用面，避免被管理员网页登录态误拦截。"""
    if _public_media_content_path(path):
        return True
    exact_paths = {
        "/models",
        "/engines",
        "/v1",
        "/v1/",
        "/v1/models",
        "/v1/engines",
        "/v1/completions",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/audio/transcriptions",
        "/v1/audio/translations",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/images/variations",
        "/v1/generate",
        "/v1/media-cooldowns",
        "/v1/gemini/generate",
        "/v1/gemini/stream",
        "/v1/gemini/media",
        "/v1/gemini/files",
        "/v1/gemini/gems",
        "/v1/gemini/jobs",
        "/v1/files",
    }
    if path in exact_paths:
        return True
    if path.startswith(
        (
            "/models/",
            "/engines/",
            "/v1/models/",
            "/v1/engines/",
            "/v1/files/",
            "/v1/gemini/gems/",
            "/v1/gemini/deep-research/",
        )
    ):
        return True
    management_prefixes = (
        "/v1/admin",
        "/v1/status",
        "/v1/settings",
        "/v1/system-settings",
        "/v1/request-logs",
        "/v1/accounts",
        "/v1/auth",
    )
    management_exact_paths = {
        "/v1/media-cooldowns/clear",
    }
    # 未知 /v1 路径仍属于外部客户端调用面；让有效 API Key 通过后返回真实 404/405。
    return path in {"/v1", "/v1/"} or (
        path.startswith("/v1/") and not path.startswith(management_prefixes)
        and path not in management_exact_paths
    )


def _api_key_from_request(request: Request) -> str:
    """兼容常见外部客户端的鉴权头写法。"""
    for header_name in ("x-api-key", "api-key", "openai-api-key"):
        api_key = request.headers.get(header_name, "").strip()
        if api_key:
            return api_key
    auth = request.headers.get("authorization", "").strip()
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return auth


def _request_id_from_request(request: Request) -> str:
    """提取或生成外部请求号，便于客户端与服务端日志关联。"""
    existing = getattr(request.state, "request_id", "")
    if existing:
        return existing
    request_id = request.headers.get("x-request-id", "").strip()
    if not request_id:
        request_id = f"req-{uuid.uuid4().hex}"
    request.state.request_id = request_id
    return request_id


def _with_request_id(response: Response, request: Request) -> Response:
    response.headers["X-Request-ID"] = _request_id_from_request(request)
    return response


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return MASKED_SECRET
    return f"{value[:4]}...{value[-4:]}"


def _key_fingerprint(value: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, value).hex[:16]


def _admin_secret(config: ServerConfig) -> str:
    # 管理端会话签名密钥：服务器部署时建议单独配置，避免重启或改密码后会话状态不可控。
    return (
        config.admin_session_secret
        or config.admin_password
        or "gemini-webapi-local-admin"
    )


def _admin_session_value(config: ServerConfig) -> str:
    # Cookie 中只保存签名后的时间戳，不保存管理员密码本身。
    timestamp = str(int(time.time()))
    signature = hmac.new(
        _admin_secret(config).encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{timestamp}.{signature}"


def _admin_session_valid(config: ServerConfig, value: str | None) -> bool:
    # 未配置管理员密码时保持本地开发模式，避免默认启动后把用户锁在管理端外。
    if not config.admin_password or not value:
        return not config.admin_password
    try:
        timestamp, signature = value.split(".", 1)
        issued_at = int(timestamp)
    except (ValueError, TypeError):
        return False
    if time.time() - issued_at > 7 * 24 * 60 * 60:
        return False
    expected = hmac.new(
        _admin_secret(config).encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _admin_username_valid(config: ServerConfig, username: str | None) -> bool:
    # ADMIN_USERNAME 是服务器部署时的可选增强；未配置时保持旧版“只输密码”的登录方式。
    if not config.admin_username:
        return True
    return hmac.compare_digest((username or "").strip(), config.admin_username)


def _admin_login_client_key(request: Request) -> str:
    """按客户端来源聚合管理员登录失败次数，避免公网部署时被简单爆破。"""
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _admin_login_limited(
    failures: dict[str, list[float]],
    key: str,
    now: float,
) -> tuple[bool, int]:
    # 只统计窗口内失败记录；达到阈值后短时间拒绝继续尝试。
    recent = [
        item
        for item in failures.get(key, [])
        if now - item < ADMIN_LOGIN_FAILURE_WINDOW_SECONDS
    ]
    failures[key] = recent
    if len(recent) < ADMIN_LOGIN_FAILURE_LIMIT:
        return False, 0
    retry_after = max(1, int(ADMIN_LOGIN_FAILURE_WINDOW_SECONDS - (now - recent[0])))
    return True, retry_after


def _record_admin_login_failure(
    failures: dict[str, list[float]],
    key: str,
    now: float,
) -> None:
    failures[key] = [
        item
        for item in failures.get(key, [])
        if now - item < ADMIN_LOGIN_FAILURE_WINDOW_SECONDS
    ] + [now]


def _normalize_api_keys(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for value in values or []:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        keys.append(item)
    return keys


def _resolve_masked_api_keys(
    incoming: list[str] | None,
    existing: list[str],
) -> list[str]:
    """前端会显示脱敏 API Key；保存时匹配脱敏值并保留原始密钥。"""
    resolved: list[str] = []
    masks = {_mask_secret(key): key for key in existing}
    for value in incoming or []:
        item = str(value or "").strip()
        if not item:
            continue
        resolved.append(masks.get(item, item))
    return _normalize_api_keys(resolved)


def _merge_system_settings(
    current: dict[str, Any],
    request: SystemSettingsRequest | None = None,
) -> dict[str, Any]:
    merged = {
        "api_keys": _normalize_api_keys(current.get("api_keys")),
        "object_storage": {
            **DEFAULT_SYSTEM_SETTINGS["object_storage"],
            **(current.get("object_storage") or {}),
        },
    }
    if request is None:
        return merged
    if request.api_keys is not None:
        merged["api_keys"] = _resolve_masked_api_keys(
            request.api_keys,
            merged["api_keys"],
        )
    if request.object_storage is not None:
        incoming = request.object_storage.model_dump()
        current_secret = merged["object_storage"].get("secret_access_key", "")
        if incoming.get("secret_access_key") in {
            MASKED_SECRET,
            _mask_secret(current_secret),
        }:
            incoming["secret_access_key"] = merged["object_storage"].get(
                "secret_access_key",
                "",
            )
        merged["object_storage"] = {
            **merged["object_storage"],
            **incoming,
        }
    return merged


def _public_system_settings(settings: dict[str, Any]) -> dict[str, Any]:
    public = _merge_system_settings(settings)
    public["api_keys"] = [
        {
            "fingerprint": _key_fingerprint(key),
            "masked": _mask_secret(key),
        }
        for key in public["api_keys"]
    ]
    storage = dict(public["object_storage"])
    storage["secret_access_key"] = _mask_secret(storage.get("secret_access_key"))
    public["object_storage"] = storage
    return public


def _media_cooldown_summary(status: dict[str, Any]) -> dict[str, Any]:
    accounts = status.get("accounts") or []
    active_accounts = [
        account for account in accounts if account.get("enabled") and not account.get("expired")
    ]
    labels = {"image": "图片", "video": "视频", "audio": "音频"}
    summary: list[dict[str, Any]] = []
    for kind in ("image", "video", "audio"):
        blocked: list[dict[str, Any]] = []
        for account in active_accounts:
            cooldown = (account.get("media_cooldowns") or {}).get(kind)
            if not cooldown:
                continue
            blocked.append(
                {
                    "account_id": account.get("id"),
                    "account_name": account.get("name"),
                    "blocked_until": cooldown.get("blocked_until"),
                    "remaining_seconds": cooldown.get("remaining_seconds", 0),
                    "reason": cooldown.get("reason", ""),
                }
            )
        blocked.sort(key=lambda item: item.get("remaining_seconds") or 0)
        summary.append(
            {
                "kind": kind,
                "label": labels[kind],
                "total": len(active_accounts),
                "blocked": len(blocked),
                "available": max(0, len(active_accounts) - len(blocked)),
                "next": blocked[0] if blocked else None,
                "accounts": blocked,
            }
        )
    return {"summary": summary, "active_account_count": len(active_accounts)}


def _media_host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return any(
        host == suffix or host.endswith(suffix)
        for suffix in MEDIA_CONTENT_ALLOWED_HOST_SUFFIXES
    )


def _media_content_type_allowed(kind: str | None, content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if not media_type:
        return True
    if kind in {"image", "web_image"}:
        return media_type.startswith("image/")
    if kind == "video":
        return media_type.startswith("video/")
    if kind == "audio":
        return media_type.startswith("audio/")
    return media_type.startswith(("image/", "video/", "audio/"))


def _job_dict(job: Any) -> dict[str, Any]:
    return job.__dict__


def _file_dict(file_record: Any) -> dict[str, Any]:
    return file_record.__dict__


def _openai_file_object(file_record: Any) -> dict[str, Any]:
    created_at = 0
    try:
        created_at = int(
            datetime.fromisoformat(
                file_record.created_at.replace("Z", "+00:00")
            ).timestamp()
        )
    except Exception:
        created_at = int(time.time())
    return {
        "id": file_record.id,
        "object": "file",
        "bytes": file_record.size,
        "created_at": created_at,
        "filename": file_record.filename,
        "purpose": getattr(file_record, "purpose", None) or "assistants",
        "status": "processed",
    }


def _gem_dict(gem: Any) -> dict[str, Any]:
    return {
        "id": gem.id,
        "name": gem.name,
        "description": gem.description,
        "prompt": gem.prompt,
        "predefined": gem.predefined,
    }


def create_app(config: ServerConfig | None = None):
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
        app.state.admin_login_failures = {}
        yield
        await auth_browser.close()
        await rotator.close()
        store.close()

    app = FastAPI(title="gemini-webapi server", version="0.1.0", lifespan=lifespan)
    # 外部 Web 面板或浏览器 SDK 调用需要 CORS；服务器部署时可通过环境变量收紧来源。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_allow_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException | StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code == 404 and detail == "Not Found":
            detail = "The requested endpoint was not found."
        elif exc.status_code == 405 and detail == "Method Not Allowed":
            detail = "The requested method is not allowed for this endpoint."
        error_type = "invalid_request_error"
        if exc.status_code in {401, 403}:
            error_type = "authentication_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=_openai_error(
                detail,
                exc.status_code,
                error_type,
                request_id=_request_id_from_request(request),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        # FastAPI 默认返回 422 结构；外部 OpenAI 兼容客户端更容易处理 400 + OpenAI error。
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.get("loc", []) if item != "body")
            message = str(first.get("msg", "Invalid request."))
            detail = f"{location}: {message}" if location else message
        else:
            detail = "Invalid request body."
        return JSONResponse(
            status_code=400,
            content=_openai_error(
                detail,
                400,
                "invalid_request_error",
                request_id=_request_id_from_request(request),
            ),
        )

    @app.middleware("http")
    async def request_id_header(request: Request, call_next):
        # 外部调用排障时需要一个稳定请求号；若客户端自带 X-Request-ID 则原样回传。
        request_id = _request_id_from_request(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        # 管理端登录只保护控制台和管理接口；OpenAI 兼容接口仍由外部 API Key 鉴权。
        admin_public_paths = {
            "/health",
            "/healthz",
            "/readyz",
            "/livez",
            "/v1/admin/status",
            "/v1/admin/login",
            "/static",
        }
        if config.admin_password and not (
            path == "/"
            or path.startswith("/static/")
            or path in admin_public_paths
        ):
            admin_ok = _admin_session_valid(
                config,
                request.cookies.get("gemini_admin_session"),
            )
            if not admin_ok and not _external_api_path(path):
                return _with_request_id(
                    JSONResponse(
                        status_code=401,
                        content={"ok": False, "detail": "Admin login required."},
                    ),
                    request,
                )

        system_settings = _merge_system_settings(
            store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        )
        allowed_api_keys = set(config.api_keys) | set(system_settings["api_keys"])
        if (
            (allowed_api_keys or config.require_api_key)
            and (
                path in {"/v1", "/v1/", "/models", "/engines"}
                or path.startswith("/v1/")
                or path.startswith("/models/")
                or path.startswith("/engines/")
            )
            and path not in {
                "/v1/status",
                "/v1/admin/status",
                "/v1/admin/login",
                "/v1/admin/logout",
            }
            and not _public_media_content_path(path)
            and not (
                config.admin_password
                and _admin_session_valid(
                    config,
                    request.cookies.get("gemini_admin_session"),
                )
            )
        ):
            token = _api_key_from_request(request)
            if not allowed_api_keys:
                return _with_request_id(
                    JSONResponse(
                        status_code=401,
                        content=_openai_error(
                            "API key is required, but no API key has been configured. Log in to the admin console and generate one in System Settings.",
                            401,
                            "authentication_error",
                            request_id=_request_id_from_request(request),
                        ),
                    ),
                    request,
                )
            if token not in allowed_api_keys:
                return _with_request_id(
                    JSONResponse(
                        status_code=401,
                        content=_openai_error(
                            "Invalid or missing API key.",
                            401,
                            "authentication_error",
                            request_id=_request_id_from_request(request),
                        ),
                    ),
                    request,
                )
        return await call_next(request)

    async def _proxy_novnc(path: str, request: Request) -> Response:
        url = f"http://127.0.0.1:6080/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        async with httpx.AsyncClient(timeout=30) as client:
            try:
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
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="授权浏览器尚未启动，请先在账户设置中点击网页授权。",
                ) from exc
        return Response(
            content=proxied.content,
            status_code=proxied.status_code,
            media_type=proxied.headers.get("content-type"),
        )

    async def _proxy_novnc_websocket(websocket: WebSocket) -> None:
        """把管理端同源 WebSocket 转发到容器内 noVNC，避免授权页跨端口不可用。"""
        # noVNC 的 WebSocket 不会经过 HTTP 中间件；这里单独校验管理员会话，避免公网部署时绕过控制台登录。
        if config.admin_password and not _admin_session_valid(
            config,
            websocket.cookies.get("gemini_admin_session"),
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async with websockets.connect("ws://127.0.0.1:6080/websockify") as upstream:
                async def client_to_upstream() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                await upstream.close()
                                return
                            if "bytes" in message and message["bytes"] is not None:
                                await upstream.send(message["bytes"])
                            elif "text" in message and message["text"] is not None:
                                await upstream.send(message["text"])
                    except WebSocketDisconnect:
                        await upstream.close()

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                done, pending = await asyncio.wait(
                    {
                        asyncio.create_task(client_to_upstream()),
                        asyncio.create_task(upstream_to_client()),
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except Exception:
            try:
                await websocket.close(code=1011)
            except RuntimeError:
                pass

    @app.websocket("/novnc/websockify")
    async def novnc_websockify(websocket: WebSocket) -> None:
        await _proxy_novnc_websocket(websocket)

    @app.websocket("/novnc/novnc/websockify")
    async def novnc_websockify_legacy(websocket: WebSocket) -> None:
        await _proxy_novnc_websocket(websocket)

    @app.api_route("/novnc", methods=["GET", "POST"])
    async def novnc_root(request: Request) -> Response:
        return await _proxy_novnc("", request)

    @app.api_route("/novnc/{path:path}", methods=["GET", "POST"])
    async def novnc_proxy(path: str, request: Request) -> Response:
        return await _proxy_novnc(path, request)

    @app.get("/")
    async def console() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    def _health_payload() -> dict[str, Any]:
        # 探活接口只返回部署排障需要的非敏感摘要，不暴露 Cookie、API Key 或账号明文。
        system_settings = _merge_system_settings(
            store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        )
        status_data = rotator.status()
        accounts = status_data.get("accounts") or []
        warnings: list[str] = []
        if not config.admin_password:
            warnings.append(
                "ADMIN_PASSWORD is not configured. The admin console and management APIs are open; set ADMIN_PASSWORD for server deployments."
            )
        elif not config.admin_username:
            warnings.append(
                "ADMIN_USERNAME is not configured. The admin console uses password-only login; set ADMIN_USERNAME for server deployments."
            )
        if (
            config.require_api_key
            and not config.api_keys
            and not system_settings["api_keys"]
            and not config.admin_password
        ):
            warnings.append(
                "REQUIRE_API_KEY is enabled but no API key or admin password is configured. Set API_KEYS or ADMIN_PASSWORD to bootstrap external access."
            )
        if (
            config.admin_password
            and not config.require_api_key
            and not config.api_keys
            and not system_settings["api_keys"]
        ):
            warnings.append(
                "Admin login protects the console, but external /v1/* APIs are currently open because no API key is configured and REQUIRE_API_KEY is disabled."
            )
        return {
            "ok": True,
            "version": app.version,
            "build": {
                "commit": config.git_commit,
            },
            "models": _openai_model_ids(),
            "accounts": {
                "total": len(accounts),
                "available": len(
                    [
                        account
                        for account in accounts
                        if account.get("enabled") and not account.get("expired")
                    ]
                ),
                "current_account_id": status_data.get("current_account_id"),
            },
            "auth": {
                "admin_enabled": bool(config.admin_password),
                "admin_username_configured": bool(config.admin_username),
                "api_key_required": bool(
                    config.require_api_key
                    or config.api_keys
                    or system_settings["api_keys"]
                ),
                "api_key_configured": bool(config.api_keys or system_settings["api_keys"]),
            },
            "warnings": warnings,
        }

    @app.api_route("/health", methods=["GET", "HEAD"])
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    @app.api_route("/readyz", methods=["GET", "HEAD"])
    @app.api_route("/livez", methods=["GET", "HEAD"])
    async def health() -> dict[str, Any]:
        return _health_payload()

    @app.get("/v1/admin/status")
    async def admin_status(request: Request) -> dict[str, Any]:
        enabled = bool(config.admin_password)
        return {
            "enabled": enabled,
            "username_required": bool(config.admin_username),
            "authenticated": _admin_session_valid(
                config,
                request.cookies.get("gemini_admin_session"),
            ),
        }

    @app.post("/v1/admin/login")
    async def admin_login(request: AdminLoginRequest, http_request: Request) -> Response:
        if not config.admin_password:
            return JSONResponse({"ok": True, "enabled": False, "authenticated": True})
        client_key = _admin_login_client_key(http_request)
        now = time.time()
        limited, retry_after = _admin_login_limited(
            app.state.admin_login_failures,
            client_key,
            now,
        )
        if limited:
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "detail": "管理员登录失败次数过多，请稍后再试。",
                },
                headers={"Retry-After": str(retry_after)},
            )
        if not _admin_username_valid(config, request.username) or not hmac.compare_digest(
            request.password,
            config.admin_password,
        ):
            _record_admin_login_failure(
                app.state.admin_login_failures,
                client_key,
                now,
            )
            raise HTTPException(status_code=401, detail="管理员密码错误。")
        app.state.admin_login_failures.pop(client_key, None)
        response = JSONResponse(
            {"ok": True, "enabled": True, "authenticated": True}
        )
        response.set_cookie(
            "gemini_admin_session",
            _admin_session_value(config),
            httponly=True,
            samesite="lax",
            secure=config.admin_cookie_secure,
            max_age=7 * 24 * 60 * 60,
            path="/",
        )
        return response

    @app.post("/v1/admin/logout")
    async def admin_logout() -> Response:
        response = JSONResponse({"ok": True})
        # 删除 Cookie 时保持和登录写入一致的安全属性，避免 HTTPS 反代下浏览器残留旧会话。
        response.delete_cookie(
            "gemini_admin_session",
            path="/",
            secure=config.admin_cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/v1/status")
    async def status() -> dict[str, Any]:
        return rotator.status()

    @app.get("/v1/media-cooldowns")
    async def media_cooldowns() -> dict[str, Any]:
        status_data = rotator.status()
        return {
            "ok": True,
            **_media_cooldown_summary(status_data),
        }

    @app.post("/v1/media-cooldowns/clear")
    async def clear_media_cooldowns(request: ClearMediaCooldownRequest) -> dict[str, Any]:
        kind = (request.kind or "").strip().lower()
        if kind and kind not in {"audio", "image", "video"}:
            raise HTTPException(
                status_code=400,
                detail="kind must be one of: image, video, audio.",
            )
        cleared = store.clear_media_cooldowns(kind or None)
        status_data = rotator.status()
        return {
            "ok": True,
            "kind": kind or None,
            "cleared": cleared,
            **_media_cooldown_summary(status_data),
        }

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

    @app.get("/v1/system-settings")
    async def get_system_settings() -> dict[str, Any]:
        settings = _merge_system_settings(
            store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        )
        return {
            "ok": True,
            "settings": _public_system_settings(settings),
            "object_storage_ready": ObjectStorageConfig.from_dict(
                settings["object_storage"]
            ).usable(),
        }

    @app.patch("/v1/system-settings")
    async def update_system_settings(request: SystemSettingsRequest) -> dict[str, Any]:
        current = store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        settings = _merge_system_settings(current, request)
        store.set_json_state(SYSTEM_SETTINGS_KEY, settings)
        return {
            "ok": True,
            "settings": _public_system_settings(settings),
            "object_storage_ready": ObjectStorageConfig.from_dict(
                settings["object_storage"]
            ).usable(),
        }

    @app.post("/v1/system-settings/api-keys")
    async def create_system_api_key() -> dict[str, Any]:
        current = store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        settings = _merge_system_settings(current)
        api_key = f"sk-gemini-{secrets.token_urlsafe(32)}"
        settings["api_keys"] = _normalize_api_keys([*settings["api_keys"], api_key])
        store.set_json_state(SYSTEM_SETTINGS_KEY, settings)
        return {
            "ok": True,
            "api_key": api_key,
            "fingerprint": _key_fingerprint(api_key),
            "settings": _public_system_settings(settings),
        }

    @app.delete("/v1/system-settings/api-keys/{fingerprint}")
    async def delete_system_api_key(fingerprint: str) -> dict[str, Any]:
        current = store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        settings = _merge_system_settings(current)
        before = len(settings["api_keys"])
        settings["api_keys"] = [
            key for key in settings["api_keys"] if _key_fingerprint(key) != fingerprint
        ]
        store.set_json_state(SYSTEM_SETTINGS_KEY, settings)
        return {
            "ok": True,
            "deleted": before - len(settings["api_keys"]),
            "settings": _public_system_settings(settings),
        }

    @app.get("/v1/request-logs")
    async def request_logs(limit: int = 80) -> dict[str, Any]:
        return {"logs": rotator.request_logs(limit=max(1, min(limit, 500)))}

    @app.get("/v1/gemini/media")
    async def gemini_media(limit: int = 80, kind: str | None = None) -> dict[str, Any]:
        return {
            "media": [
                _media_record_dict(item)
                for item in store.list_media_outputs(
                    limit=max(1, min(limit, 500)), kind=kind
                )
            ]
        }

    def _media_content_bytes(item: Any) -> tuple[bytes, str]:
        """读取媒体内容；优先读本地缓存，缺失时按允许域名和账号 Cookie 拉取原始链接。"""
        if item.local_path:
            local_path = Path(item.local_path)
            if local_path.is_file():
                return (
                    local_path.read_bytes(),
                    item.local_content_type or "application/octet-stream",
                )
        if not _media_host_allowed(item.url):
            raise HTTPException(status_code=400, detail="Media host is not allowed.")
        account = store.get_account(item.account_id) if item.account_id else None
        cookies = account.cookies if account else None
        with httpx.Client(timeout=120, follow_redirects=True, cookies=cookies) as client:
            response = client.get(item.url)
            response.raise_for_status()
        content_type = response.headers.get("content-type") or "application/octet-stream"
        if not _media_content_type_allowed(item.kind, content_type):
            raise HTTPException(
                status_code=502,
                detail=f"Media source returned {content_type}, not {item.kind} content.",
            )
        content = response.content
        if len(content) > MEDIA_CONTENT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Media file is too large.")
        return content, content_type

    @app.api_route("/v1/gemini/media/{media_token}/content", methods=["GET", "HEAD"])
    async def gemini_media_content(request: Request, media_token: str) -> Response:
        item = store.get_media_output_by_token(media_token)
        if item is None:
            raise HTTPException(status_code=404, detail="Media not found.")
        content, content_type = _media_content_bytes(item)
        return Response(
            content=b"" if request.method == "HEAD" else content,
            media_type=content_type,
        )

    @app.get("/v1/gemini/jobs")
    async def gemini_jobs(limit: int = 80, job_type: str | None = None) -> dict[str, Any]:
        return {
            "jobs": [
                _job_dict(job)
                for job in store.list_jobs(limit=max(1, min(limit, 500)), job_type=job_type)
            ]
        }

    @app.api_route("/v1/gemini/files", methods=["GET", "HEAD"])
    async def list_files(limit: int = 80) -> dict[str, Any]:
        return {"files": [_file_dict(item) for item in store.list_files(limit=limit)]}

    @app.api_route("/v1", methods=["GET", "HEAD"])
    @app.api_route("/v1/", methods=["GET", "HEAD"])
    async def v1_root() -> dict[str, Any]:
        # 外部客户端有时会探测 base_url + "/v1"；这里返回非敏感能力摘要。
        return {
            "ok": True,
            "object": "api.root",
            "models": _openai_model_ids(),
            "endpoints": {
                "models": "/v1/models",
                "chat_completions": "/v1/chat/completions",
                "completions": "/v1/completions",
                "responses": "/v1/responses",
                "images": "/v1/images/generations",
                "audio_transcriptions": "/v1/audio/transcriptions",
                "audio_translations": "/v1/audio/translations",
                "gemini_generate": "/v1/gemini/generate",
                "gemini_stream": "/v1/gemini/stream",
                "gemini_media": "/v1/gemini/media",
            },
        }

    async def _save_uploaded_file(file: UploadFile, purpose: str = "assistants") -> Any:
        # 上传文件统一落到 data/uploads，OpenAI 兼容和 Gemini 原生接口共享同一个 file_id。
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
            purpose=purpose or "assistants",
        )
        return store.get_file(file_id)

    @app.post("/v1/gemini/files")
    async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
        record = await _save_uploaded_file(file)
        return {
            "ok": True,
            "file": _file_dict(record),
        }

    @app.api_route("/v1/files", methods=["GET", "HEAD"])
    async def openai_list_files(limit: int = 80) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                _openai_file_object(item)
                for item in store.list_files(limit=max(1, min(limit, 500)))
            ],
        }

    @app.post("/v1/files")
    async def openai_upload_file(
        file: UploadFile = File(...),
        purpose: str = Form("assistants"),
    ) -> dict[str, Any]:
        # OpenAI 兼容客户端会回看 purpose 字段，这里保留调用方传入的用途。
        record = await _save_uploaded_file(file, purpose=purpose or "assistants")
        return _openai_file_object(record)

    @app.api_route("/v1/files/{file_id}", methods=["GET", "HEAD"])
    async def openai_get_file(file_id: str) -> dict[str, Any]:
        record = store.get_file(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found.")
        return _openai_file_object(record)

    @app.api_route("/v1/files/{file_id}/content", methods=["GET", "HEAD"])
    async def openai_get_file_content(request: Request, file_id: str) -> Response:
        record = store.get_file(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found.")
        path = Path(record.path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="File content not found.")
        if request.method == "HEAD":
            # 外部客户端和反向代理常用 HEAD 探测文件类型；这里避免读取文件正文。
            return Response(
                content=b"",
                media_type=record.content_type or "application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{record.filename}"'},
            )
        return FileResponse(
            path,
            media_type=record.content_type or "application/octet-stream",
            filename=record.filename,
        )

    @app.delete("/v1/files/{file_id}")
    async def openai_delete_file(file_id: str) -> dict[str, Any]:
        record = store.get_file(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found.")
        deleted = store.delete_file(file_id)
        path = Path(record.path)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        return {
            "id": file_id,
            "object": "file",
            "deleted": bool(deleted),
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

    def _media_file_suffix(kind: str, content_type: str | None, url: str) -> str:
        media_type = (content_type or "").split(";", 1)[0].strip()
        suffix = mimetypes.guess_extension(media_type) if media_type else None
        if suffix:
            return suffix
        parsed_suffix = Path(urlparse(url).path).suffix
        if parsed_suffix:
            return parsed_suffix[:16]
        return {
            "image": ".png",
            "web_image": ".png",
            "video": ".mp4",
            "audio": ".mp3",
        }.get(kind, ".bin")

    def _download_media_item(item: dict[str, Any]) -> dict[str, Any]:
        url = item.get("url") or ""
        if not url or not _media_host_allowed(url):
            return {}
        try:
            account = store.get_account(rotator.status()["current_account_id"])
            cookies = account.cookies if account else None
            with httpx.Client(timeout=120, follow_redirects=True, cookies=cookies) as client:
                response = client.get(url)
                response.raise_for_status()
            content_type = response.headers.get("content-type")
            if not _media_content_type_allowed(item.get("kind"), content_type):
                return {}
            content = response.content
            if len(content) > MEDIA_CONTENT_MAX_BYTES:
                return {}
            return {
                "content": content,
                "content_type": content_type,
                "size": len(content),
            }
        except Exception:
            return {}

    def _cache_downloaded_media(item: dict[str, Any], downloaded: dict[str, Any]) -> dict[str, Any]:
        content = downloaded.get("content")
        if not content:
            return {}
        try:
            url = item.get("url") or ""
            content_type = downloaded.get("content_type")
            suffix = _media_file_suffix(item.get("kind", "media"), content_type, url)
            cache_dir = Path(config.database_path).resolve().parent / "media-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            dest = cache_dir / f"{uuid.uuid4().hex}{suffix}"
            dest.write_bytes(content)
            return {
                "path": str(dest),
                "content_type": content_type,
                "size": downloaded.get("size") or len(content),
            }
        except Exception:
            return {}

    async def _upload_media_to_object_storage(
        item: dict[str, Any],
        downloaded: dict[str, Any],
    ) -> dict[str, Any]:
        settings = _merge_system_settings(
            store.get_json_state(SYSTEM_SETTINGS_KEY, DEFAULT_SYSTEM_SETTINGS)
        )
        storage_config = ObjectStorageConfig.from_dict(settings["object_storage"])
        content = downloaded.get("content")
        if not storage_config.usable() or not content:
            return {}
        content_type = downloaded.get("content_type") or "application/octet-stream"
        category = {
            "image": "gemini/images",
            "web_image": "gemini/images",
            "video": "gemini/videos",
            "audio": "gemini/audio",
        }.get(item.get("kind"), "gemini/media")
        key = build_media_object_key(
            prefix=storage_config.prefix,
            category=category,
            data=content,
            content_type=content_type,
            source_url=item.get("url") or "",
        )
        return await upload_s3_compatible(
            config=storage_config,
            key=key,
            data=content,
            content_type=content_type,
        )

    async def _save_media_index(
        *,
        request_id: str,
        account_id: int | None,
        output: Any,
        store_media: bool = False,
    ) -> int:
        count = 0
        for item in _media_entries(output):
            downloaded = _download_media_item(item)
            cache: dict[str, Any] = {}
            storage: dict[str, Any] = {}
            if store_media and downloaded:
                try:
                    storage = await _upload_media_to_object_storage(item, downloaded)
                except Exception as exc:
                    storage = {"error": str(exc)}
            if not storage.get("url"):
                cache = _cache_downloaded_media(item, downloaded)
            metadata = {
                **item,
                "original_url": item["url"],
            }
            if storage:
                metadata["object_storage"] = storage
            store.add_media_output(
                request_id=request_id,
                account_id=account_id,
                kind=item["kind"],
                title=item.get("title"),
                url=storage.get("url") or item["url"],
                thumbnail=item.get("thumbnail"),
                local_path=cache.get("path"),
                local_content_type=cache.get("content_type"),
                local_size=cache.get("size"),
                metadata=metadata,
            )
            count += 1
        return count

    @app.post("/v1/gemini/generate")
    async def gemini_generate(
        http_request: Request,
        request: GeminiGenerateRequest,
    ) -> dict[str, Any]:
        request_id = _request_id_from_request(http_request)
        try:
            generation_mode = _generation_mode_arg(request.mode)
            resolved_model = _resolve_model_arg(request.model)

            async def operation(client):
                kwargs: dict[str, Any] = {
                    "temporary": request.temporary,
                    "deep_research": request.deep_research,
                }
                if resolved_model:
                    kwargs["model"] = resolved_model
                if generation_mode:
                    kwargs["generation_mode"] = generation_mode
                gem_arg = await _gem_arg(client, request)
                if gem_arg:
                    kwargs["gem"] = gem_arg
                files = _file_paths(request.file_ids)
                if files:
                    kwargs["files"] = files
                output = await client.generate_content(request.prompt, **kwargs)
                _ensure_media_generation_result(output, generation_mode)
                return output

            output = await rotator.run(
                operation,
                endpoint="/v1/gemini/generate",
                model=request.model or "gemini",
                output_type=f"gemini_{request.mode or 'native'}",
                job_id=request_id,
                require_video_generation=generation_mode == "video",
                media_generation_mode=generation_mode,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        account_id = rotator.status()["current_account_id"]
        media_count = await _save_media_index(
            request_id=request_id,
            account_id=account_id,
            output=output,
            store_media=request.store_media,
        )
        if media_count:
            store.update_request_log_media_count(request_id, media_count)
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
    async def gemini_stream(
        http_request: Request,
        request: GeminiGenerateRequest,
    ):
        request_id = _request_id_from_request(http_request)
        try:
            generation_mode = _generation_mode_arg(request.mode)
            resolved_model = _resolve_model_arg(request.model)
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        async def event_stream():
            final_output = None

            async def operation(client):
                kwargs: dict[str, Any] = {
                    "temporary": request.temporary,
                    "deep_research": request.deep_research,
                }
                if resolved_model:
                    kwargs["model"] = resolved_model
                if generation_mode:
                    kwargs["generation_mode"] = generation_mode
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
                    output_type=f"gemini_{request.mode or 'native'}",
                    job_id=request_id,
                    require_video_generation=generation_mode == "video",
                    media_generation_mode=generation_mode,
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
                yield _sse_error_event(error)
                yield "data: [DONE]\n\n"
                return

            if final_output is not None:
                try:
                    _ensure_media_generation_result(final_output, generation_mode)
                except Exception as exc:
                    error = {"ok": False, "error": str(exc), "status": _error_status(exc)}
                    yield _sse_error_event(error)
                    yield "data: [DONE]\n\n"
                    return
                account_id = rotator.status()["current_account_id"]
                media_count = await _save_media_index(
                    request_id=request_id,
                    account_id=account_id,
                    output=final_output,
                    store_media=request.store_media,
                )
                if media_count:
                    store.update_request_log_media_count(request_id, media_count)
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

        return _sse_response(event_stream())

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

    @app.api_route("/models", methods=["GET", "HEAD"])
    @app.api_route("/v1/models", methods=["GET", "HEAD"])
    async def models() -> dict[str, Any]:
        now = int(time.time())
        return {
            "object": "list",
            "data": [_openai_model_object(model_id, now) for model_id in _openai_model_ids()],
        }

    @app.api_route("/models/{model_id}", methods=["GET", "HEAD"])
    @app.api_route("/v1/models/{model_id}", methods=["GET", "HEAD"])
    async def model_detail(model_id: str) -> dict[str, Any]:
        try:
            resolved_model = _resolve_model_arg(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _openai_model_object(resolved_model or "gemini")

    @app.api_route("/engines", methods=["GET", "HEAD"])
    @app.api_route("/v1/engines", methods=["GET", "HEAD"])
    async def engines() -> dict[str, Any]:
        now = int(time.time())
        return {
            "object": "list",
            "data": [_openai_engine_object(model_id, now) for model_id in _openai_model_ids()],
        }

    @app.api_route("/engines/{model_id}", methods=["GET", "HEAD"])
    @app.api_route("/v1/engines/{model_id}", methods=["GET", "HEAD"])
    async def engine_detail(model_id: str) -> dict[str, Any]:
        try:
            resolved_model = _resolve_model_arg(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _openai_engine_object(resolved_model or "gemini")

    @app.get("/v1/accounts")
    async def list_accounts() -> dict[str, Any]:
        """返回账户池列表，便于管理端和调试脚本直接读取。"""
        status = rotator.status()
        return {
            "ok": True,
            "current_account_id": status["current_account_id"],
            "accounts": status["accounts"],
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

    @app.post("/v1/accounts/{account_id}/media-cooldowns/clear")
    async def clear_account_media_cooldowns(
        account_id: int, request: ClearMediaCooldownRequest
    ) -> dict[str, Any]:
        if store.get_account(account_id) is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        kinds = ["audio", "image", "video"] if request.kind is None else [request.kind]
        cleared: list[str] = []
        for kind in kinds:
            normalized = (kind or "").strip().lower()
            if normalized not in {"audio", "image", "video"}:
                raise HTTPException(
                    status_code=400,
                    detail="kind must be one of: image, video, audio.",
                )
            if store.clear_media_cooldown(account_id, normalized):
                cleared.append(normalized)
        return {
            "ok": True,
            "account_id": account_id,
            "cleared": cleared,
            "accounts": rotator.status()["accounts"],
        }

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
    async def generate(request: Request, payload: GenerateRequest) -> dict[str, Any]:
        request_id = _request_id_from_request(request)
        try:
            generation_mode = _generation_mode_arg(payload.mode)
            resolved_model = _resolve_model_arg(payload.model)

            async def operation(client):
                kwargs: dict[str, Any] = {"temporary": payload.temporary}
                if resolved_model:
                    kwargs["model"] = resolved_model
                if generation_mode:
                    kwargs["generation_mode"] = generation_mode
                output = await client.generate_content(payload.prompt, **kwargs)
                _ensure_media_generation_result(output, generation_mode)
                return output

            output = await rotator.run(
                operation,
                endpoint="/v1/generate",
                model=payload.model or "gemini",
                output_type=f"gemini_{payload.mode or 'native'}",
                job_id=request_id,
                require_video_generation=generation_mode == "video",
                media_generation_mode=generation_mode,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        account_id = rotator.status()["current_account_id"]
        media_count = await _save_media_index(
            request_id=request_id,
            account_id=account_id,
            output=output,
            store_media=False,
        )
        if media_count:
            store.update_request_log_media_count(request_id, media_count)
        return {
            "text": output.text,
            "metadata": output.metadata,
            "account": account_id,
            "request_id": request_id,
            "media_count": media_count,
        }

    @app.post("/v1/responses")
    async def responses(request: Request, payload: ResponsesRequest) -> dict[str, Any]:
        request_id = _request_id_from_request(request)
        try:
            response_messages = _responses_messages(payload)
            prompt = _responses_prompt(payload)
            prompt = _append_sampling_parameter_instructions(prompt, payload)
            files = _file_paths(_messages_file_ids(response_messages))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not prompt:
            raise HTTPException(status_code=400, detail="input must contain text.")
        model = payload.model or "gemini"
        try:
            resolved_model = _resolve_model_arg(payload.model)
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        if payload.stream:
            response_id = f"resp_{uuid.uuid4().hex}"
            output_id = f"msg_{uuid.uuid4().hex}"
            content_id = f"out_{uuid.uuid4().hex}"
            created = int(time.time())
            tools_enabled = bool(payload.tools)

            async def event_stream():
                yield _responses_stream_event(
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created,
                            "status": "in_progress",
                            "model": model,
                        },
                    },
                )
                if not tools_enabled:
                    yield _responses_stream_event(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "id": output_id,
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            },
                        },
                    )
                    yield _responses_stream_event(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "item_id": output_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {
                                "id": content_id,
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        },
                    )
                text_parts: list[str] = []

                async def operation(client):
                    kwargs: dict[str, Any] = {}
                    if resolved_model:
                        kwargs["model"] = resolved_model
                    if files:
                        kwargs["files"] = files
                    async for output in client.generate_content_stream(prompt, **kwargs):
                        yield output

                try:
                    async for output in rotator.run_stream(
                        operation,
                        endpoint="/v1/responses",
                        model=model,
                        job_id=request_id,
                    ):
                        delta = output.text_delta or ""
                        if not delta:
                            continue
                        text_parts.append(delta)
                        if not tools_enabled:
                            yield _responses_stream_event(
                                "response.output_text.delta",
                                {
                                    "type": "response.output_text.delta",
                                    "item_id": output_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": delta,
                                },
                            )
                except Exception as exc:
                    yield _responses_stream_event(
                        "response.failed",
                        {
                            "type": "response.failed",
                            "response": {
                                "id": response_id,
                                "object": "response",
                                "created_at": created,
                                "status": "failed",
                                "model": model,
                                "error": _openai_error(
                                    str(exc),
                                    _error_status(exc),
                                )["error"],
                            },
                        },
                    )
                    yield "data: [DONE]\n\n"
                    return

                text = "".join(text_parts)
                tool_calls = _tool_calls_from_output_text(text, payload.tools)
                if tool_calls:
                    for index, call in enumerate(tool_calls):
                        function = call.get("function") or {}
                        item = {
                            "id": call["id"],
                            "type": "function_call",
                            "status": "completed",
                            "call_id": call["id"],
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", "{}"),
                        }
                        yield _responses_stream_event(
                            "response.output_item.added",
                            {
                                "type": "response.output_item.added",
                                "output_index": index,
                                "item": item,
                            },
                        )
                        yield _responses_stream_event(
                            "response.output_item.done",
                            {
                                "type": "response.output_item.done",
                                "output_index": index,
                                "item": item,
                            },
                        )
                    yield _responses_stream_event(
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": _responses_output(
                                response_id=response_id,
                                model=model,
                                text=text,
                                tool_calls=tool_calls,
                                created=created,
                            ),
                        },
                    )
                    yield "data: [DONE]\n\n"
                    return

                if tools_enabled:
                    yield _responses_stream_event(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "id": output_id,
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            },
                        },
                    )
                    yield _responses_stream_event(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "item_id": output_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {
                                "id": content_id,
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        },
                    )
                    if text:
                        yield _responses_stream_event(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": output_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": text,
                            },
                        )
                yield _responses_stream_event(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": output_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {
                            "id": content_id,
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        },
                    },
                )
                yield _responses_stream_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": output_id,
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "id": content_id,
                                    "type": "output_text",
                                    "text": text,
                                    "annotations": [],
                                }
                            ],
                        },
                    },
                )
                yield _responses_stream_event(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": _responses_output(
                            response_id=response_id,
                            model=model,
                            text=text,
                            created=created,
                        ),
                    },
                )
                yield "data: [DONE]\n\n"

            return _sse_response(event_stream())

        async def operation(client):
            kwargs: dict[str, Any] = {}
            if resolved_model:
                kwargs["model"] = resolved_model
            if files:
                kwargs["files"] = files
            return await client.generate_content(prompt, **kwargs)

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/responses",
                model=model,
                job_id=request_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        tool_calls = _tool_calls_from_output_text(output.text, payload.tools)
        return _responses_output(
            response_id=f"resp_{uuid.uuid4().hex}",
            model=model,
            text=output.text,
            tool_calls=tool_calls,
        )

    async def _run_openai_image_request(
        *,
        endpoint: str,
        prompt: str,
        model: str | None,
        n: int | None,
        response_format: str | None,
        store_media: bool,
        files: list[str] | None = None,
        request_id: str | None = None,
        http_request: Request | None = None,
    ) -> dict[str, Any]:
        response_format = response_format or "url"
        if response_format not in {"url", "b64_json"}:
            raise HTTPException(
                status_code=400,
                detail=f"response_format must be one of: url, b64_json for {endpoint}.",
            )
        if n is not None and n < 1:
            raise HTTPException(status_code=400, detail="n must be at least 1.")
        if n is not None and n > 1:
            raise HTTPException(status_code=400, detail="n>1 is not supported for image endpoints.")
        # 图片接口也复用外部请求号，方便调用方用响应头里的 X-Request-ID 反查日志和媒体记录。
        request_id = request_id or f"img-{uuid.uuid4().hex}"
        generation_mode = "image"
        try:
            resolved_model = _resolve_openai_image_model_arg(model)

            async def operation(client):
                kwargs: dict[str, Any] = {"generation_mode": generation_mode}
                if resolved_model:
                    kwargs["model"] = resolved_model
                if files:
                    kwargs["files"] = files
                output = await client.generate_content(prompt, **kwargs)
                _ensure_media_generation_result(output, generation_mode)
                return output

            output = await rotator.run(
                operation,
                endpoint=endpoint,
                model=model or "gemini",
                output_type="gemini_image",
                job_id=request_id,
                media_generation_mode=generation_mode,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        account_id = rotator.status()["current_account_id"]
        media_count = await _save_media_index(
            request_id=request_id,
            account_id=account_id,
            output=output,
            store_media=store_media,
        )
        if media_count:
            store.update_request_log_media_count(request_id, media_count)
        media_items = [
            item
            for item in store.list_media_outputs(limit=max(media_count, 1), kind="image")
            if item.request_id == request_id
        ]
        if not media_items:
            raise HTTPException(status_code=502, detail="Image generation did not return a usable image URL.")
        return _openai_image_generation_output(
            media_items,
            revised_prompt=prompt,
            response_format=response_format,
            media_content_loader=lambda item: _media_content_bytes(item)[0],
            # 外部 OpenAI 客户端会直接使用返回 URL，带上当前服务根地址避免相对路径无法预览。
            base_url=str(http_request.base_url).rstrip("/") if http_request else None,
        )

    @app.post("/v1/images/generations")
    async def image_generations(
        http_request: Request,
        request: ImageGenerationRequest,
    ) -> dict[str, Any]:
        return await _run_openai_image_request(
            endpoint="/v1/images/generations",
            prompt=request.prompt,
            model=request.model,
            n=request.n,
            response_format=request.response_format,
            store_media=request.store_media,
            request_id=_request_id_from_request(http_request),
            http_request=http_request,
        )

    async def _save_temporary_upload_inputs(
        uploads: list[UploadFile],
        *,
        prefix: str,
    ) -> tuple[Path, list[str]]:
        # OpenAI 上传文件只作为本次 Gemini 调用输入，调用结束后立即清理，避免临时音频/图片占用磁盘。
        temp_dir = Path(config.database_path).resolve().parent / prefix / uuid.uuid4().hex
        temp_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for index, upload in enumerate(uploads):
            suffix = Path(upload.filename or f"image-{index}.png").suffix or ".png"
            dest = temp_dir / f"{index}{suffix}"
            dest.write_bytes(await upload.read())
            saved.append(str(dest))
        return temp_dir, saved

    async def _cleanup_temporary_upload_inputs(temp_dir: Path, paths: list[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    async def _run_openai_audio_request(
        *,
        endpoint: str,
        output_type: str,
        task: str,
        base_instruction: str,
        file: UploadFile,
        model: str | None,
        prompt: str | None,
        response_format: str | None,
        language: str | None,
        temperature: float | None,
        request_id: str | None = None,
    ) -> Response | dict[str, Any]:
        allowed_formats = {"json", "text", "verbose_json", "srt", "vtt"}
        if response_format and response_format not in allowed_formats:
            raise HTTPException(
                status_code=400,
                detail="response_format must be one of: json, text, verbose_json, srt, vtt.",
            )
        prefix = "audio-translations" if task == "translate" else "audio-transcriptions"
        audio_dir, paths = await _save_temporary_upload_inputs(
            [file],
            prefix=prefix,
        )
        try:
            try:
                resolved_model = _resolve_model_arg(model)
            except Exception as exc:
                raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
            instructions = [
                base_instruction,
            ]
            if language:
                instructions.append(f"音频语言提示：{language}。")
            if prompt:
                instructions.append(f"上下文提示：{prompt}")
            if temperature is not None:
                instructions.append(f"转写时尽量保持稳定，调用方 temperature={temperature}。")
            transcription_prompt = "\n".join(instructions)

            async def operation(client):
                kwargs: dict[str, Any] = {"files": paths}
                if resolved_model:
                    kwargs["model"] = resolved_model
                return await client.generate_content(transcription_prompt, **kwargs)

            try:
                output = await rotator.run(
                    operation,
                    endpoint=endpoint,
                    model=model or "gemini",
                    output_type=output_type,
                    job_id=request_id,
                )
            except Exception as exc:
                raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

            text = (output.text or "").strip()
            if response_format == "text":
                return Response(content=text, media_type="text/plain; charset=utf-8")
            if response_format == "srt":
                return Response(
                    content=_audio_srt_text(text),
                    media_type="text/plain; charset=utf-8",
                )
            if response_format == "vtt":
                return Response(
                    content=_audio_vtt_text(text),
                    media_type="text/vtt; charset=utf-8",
                )
            if response_format == "verbose_json":
                return {
                    "task": task,
                    "language": language,
                    "duration": None,
                    "text": text,
                    "segments": [],
                }
            return {"text": text}
        finally:
            await _cleanup_temporary_upload_inputs(audio_dir, paths)

    @app.post("/v1/audio/transcriptions", response_model=None)
    async def audio_transcriptions(
        request: Request,
        file: UploadFile = File(...),
        model: str | None = Form(None),
        prompt: str | None = Form(None),
        response_format: str | None = Form(None),
        language: str | None = Form(None),
        temperature: float | None = Form(None),
    ) -> Response | dict[str, Any]:
        return await _run_openai_audio_request(
            endpoint="/v1/audio/transcriptions",
            output_type="audio_transcription",
            task="transcribe",
            base_instruction="请转写上传的音频文件，只返回转写文本。",
            file=file,
            model=model,
            prompt=prompt,
            response_format=response_format,
            language=language,
            temperature=temperature,
            request_id=_request_id_from_request(request),
        )

    @app.post("/v1/audio/translations", response_model=None)
    async def audio_translations(
        request: Request,
        file: UploadFile = File(...),
        model: str | None = Form(None),
        prompt: str | None = Form(None),
        response_format: str | None = Form(None),
        language: str | None = Form(None),
        temperature: float | None = Form(None),
    ) -> Response | dict[str, Any]:
        return await _run_openai_audio_request(
            endpoint="/v1/audio/translations",
            output_type="audio_translation",
            task="translate",
            base_instruction="请将上传音频中的内容翻译成英文，只返回英文翻译文本。",
            file=file,
            model=model,
            prompt=prompt,
            response_format=response_format,
            language=language,
            temperature=temperature,
            request_id=_request_id_from_request(request),
        )

    @app.post("/v1/images/edits")
    async def image_edits(
        request: Request,
        prompt: str = Form(...),
        image: list[UploadFile] = File(...),
        mask: UploadFile | None = File(None),
        model: str | None = Form(None),
        n: int | None = Form(None),
        size: str | None = Form(None),
        response_format: str | None = Form(None),
    ) -> dict[str, Any]:
        uploads: list[UploadFile] = list(image)
        if mask is not None:
            uploads.append(mask)
        edit_dir, paths = await _save_temporary_upload_inputs(uploads, prefix="image-edits")
        try:
            return await _run_openai_image_request(
                endpoint="/v1/images/edits",
                prompt=prompt,
                model=model,
                n=n,
                response_format=response_format,
                store_media=False,
                files=paths,
                request_id=_request_id_from_request(request),
                http_request=request,
            )
        finally:
            await _cleanup_temporary_upload_inputs(edit_dir, paths)

    @app.post("/v1/images/variations")
    async def image_variations(
        request: Request,
        image: UploadFile = File(...),
        model: str | None = Form(None),
        n: int | None = Form(None),
        size: str | None = Form(None),
        response_format: str | None = Form(None),
    ) -> dict[str, Any]:
        variation_dir, paths = await _save_temporary_upload_inputs(
            [image],
            prefix="image-variations",
        )
        try:
            return await _run_openai_image_request(
                endpoint="/v1/images/variations",
                prompt="基于上传图片生成新的图片变体。",
                model=model,
                n=n,
                response_format=response_format,
                store_media=False,
                files=paths,
                request_id=_request_id_from_request(request),
                http_request=request,
            )
        finally:
            await _cleanup_temporary_upload_inputs(variation_dir, paths)

    @app.post("/v1/completions")
    async def completions(request: Request, payload: CompletionRequest):
        request_id = _request_id_from_request(request)
        if payload.n is not None and payload.n < 1:
            raise HTTPException(status_code=400, detail="n must be at least 1.")
        if payload.n and payload.n > 1:
            raise HTTPException(status_code=400, detail="Only n=1 is supported.")
        prompt_value = payload.prompt[0] if isinstance(payload.prompt, list) else payload.prompt
        if not isinstance(prompt_value, str) or not prompt_value:
            raise HTTPException(status_code=400, detail="prompt must contain text.")
        prompt = prompt_value
        if payload.suffix:
            prompt = f"{prompt}\n{payload.suffix}"
        prompt = _append_completion_token_limit_instruction(prompt, payload)
        prompt = _append_sampling_parameter_instructions(prompt, payload)
        model = payload.model or "gemini"
        try:
            resolved_model = _resolve_model_arg(payload.model)
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        if payload.stream:
            completion_id = f"cmpl-{uuid.uuid4().hex}"

            async def event_stream():
                emitted_text = ""
                stopped_by_sequence = False

                async def operation(client):
                    kwargs: dict[str, Any] = {}
                    if resolved_model:
                        kwargs["model"] = resolved_model
                    async for output in client.generate_content_stream(prompt, **kwargs):
                        yield output

                try:
                    async for output in rotator.run_stream(
                        operation,
                        endpoint="/v1/completions",
                        model=model,
                        job_id=request_id,
                    ):
                        delta = output.text_delta or ""
                        if not delta:
                            continue
                        next_text = f"{emitted_text}{delta}"
                        truncated, stopped_by_sequence = _apply_stop_sequences(
                            next_text,
                            payload.stop,
                        )
                        send_delta = truncated[len(emitted_text) :]
                        emitted_text = truncated
                        if send_delta:
                            chunk = _completion_chunk(completion_id, model, text=send_delta)
                            yield f"data: {json.dumps(chunk).decode()}\n\n"
                        if stopped_by_sequence:
                            break
                except Exception as exc:
                    error = _openai_error(str(exc), _error_status(exc))
                    yield _sse_error_event(error)
                    yield "data: [DONE]\n\n"
                    return

                final = _completion_chunk(
                    completion_id,
                    model,
                    finish_reason="stop",
                )
                yield f"data: {json.dumps(final).decode()}\n\n"
                if _stream_include_usage(payload.stream_options):
                    usage = _completion_usage_chunk(completion_id, model)
                    yield f"data: {json.dumps(usage).decode()}\n\n"
                yield "data: [DONE]\n\n"

            return _sse_response(event_stream())

        async def operation(client):
            kwargs: dict[str, Any] = {}
            if resolved_model:
                kwargs["model"] = resolved_model
            return await client.generate_content(prompt, **kwargs)

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/completions",
                model=model,
                job_id=request_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
        text, _ = _apply_stop_sequences(output.text, payload.stop)
        return {
            "id": f"cmpl-{uuid.uuid4().hex}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "text": text,
                    "finish_reason": "stop",
                }
            ],
            "usage": _completion_zero_usage(),
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, payload: ChatCompletionRequest):
        request_id = _request_id_from_request(request)
        try:
            files = _file_paths(_messages_file_ids(payload.messages))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prompt = _messages_to_prompt(payload.messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="messages must contain text.")
        prompt = _append_tool_instructions(prompt, payload)
        try:
            prompt = _append_response_format_instructions(prompt, payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prompt = _append_chat_token_limit_instruction(prompt, payload)
        prompt = _append_sampling_parameter_instructions(prompt, payload)
        model = payload.model or "gemini"
        normalized_tools, _ = _normalized_chat_request_tools(payload)
        try:
            resolved_model = _resolve_model_arg(payload.model)
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        if payload.stream:
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"

            async def event_stream():
                first = _chat_chunk(completion_id, model, role="assistant")
                yield f"data: {json.dumps(first).decode()}\n\n"
                buffered_text: list[str] = []
                emitted_text = ""
                stopped_by_sequence = False

                async def operation(client):
                    kwargs: dict[str, Any] = {}
                    if resolved_model:
                        kwargs["model"] = resolved_model
                    if files:
                        kwargs["files"] = files
                    async for output in client.generate_content_stream(prompt, **kwargs):
                        yield output

                try:
                    async for output in rotator.run_stream(
                        operation,
                        endpoint="/v1/chat/completions",
                        model=model,
                        job_id=request_id,
                    ):
                        delta = output.text_delta or ""
                        if delta:
                            if _tools_enabled(payload):
                                buffered_text.append(delta)
                                continue
                            next_text = f"{emitted_text}{delta}"
                            truncated, stopped_by_sequence = _apply_stop_sequences(
                                next_text,
                                payload.stop,
                            )
                            send_delta = truncated[len(emitted_text) :]
                            emitted_text = truncated
                            if send_delta:
                                chunk = _chat_chunk(completion_id, model, content=send_delta)
                                yield f"data: {json.dumps(chunk).decode()}\n\n"
                            if stopped_by_sequence:
                                break
                except Exception as exc:
                    error = _openai_error(str(exc), _error_status(exc))
                    yield _sse_error_event(error)
                    yield "data: [DONE]\n\n"
                    return

                finish_reason = "stop"
                if _tools_enabled(payload):
                    text = "".join(buffered_text)
                    tool_calls = _tool_calls_from_output_text(text, normalized_tools)
                    if tool_calls:
                        tool_chunk = _chat_tool_calls_chunk(completion_id, model, tool_calls)
                        yield f"data: {json.dumps(tool_chunk).decode()}\n\n"
                        finish_reason = "tool_calls"
                    elif text:
                        chunk = _chat_chunk(completion_id, model, content=text)
                        yield f"data: {json.dumps(chunk).decode()}\n\n"
                final = _chat_chunk(completion_id, model, finish_reason=finish_reason)
                yield f"data: {json.dumps(final).decode()}\n\n"
                if _stream_include_usage(payload.stream_options):
                    usage = _chat_usage_chunk(completion_id, model)
                    yield f"data: {json.dumps(usage).decode()}\n\n"
                yield "data: [DONE]\n\n"

            return _sse_response(event_stream())

        async def operation(client):
            kwargs: dict[str, Any] = {}
            if resolved_model:
                kwargs["model"] = resolved_model
            if files:
                kwargs["files"] = files
            return await client.generate_content(prompt, **kwargs)

        try:
            output = await rotator.run(
                operation,
                endpoint="/v1/chat/completions",
                model=model,
                job_id=request_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

        created = int(time.time())
        tool_calls = (
            _tool_calls_from_output_text(output.text, normalized_tools)
            if _tools_enabled(payload)
            else []
        )
        content_text = output.text
        if not tool_calls:
            content_text, _ = _apply_stop_sequences(content_text, payload.stop)
        message: dict[str, Any] = {"role": "assistant", "content": content_text}
        finish_reason = "stop"
        if tool_calls:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            }
            if payload.functions:
                message["function_call"] = _legacy_function_call_from_tool_call(
                    tool_calls[0]
                )
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
            "usage": _chat_zero_usage(),
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
