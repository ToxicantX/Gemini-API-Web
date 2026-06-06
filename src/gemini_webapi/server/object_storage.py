from __future__ import annotations

import hashlib
import hmac
import mimetypes
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx


@dataclass(frozen=True)
class ObjectStorageConfig:
    enabled: bool
    endpoint: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str
    public_url: str
    force_path_style: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObjectStorageConfig":
        return cls(
            enabled=bool(data.get("enabled")),
            endpoint=str(data.get("endpoint") or "").strip(),
            region=str(data.get("region") or "auto").strip() or "auto",
            bucket=str(data.get("bucket") or "").strip(),
            access_key_id=str(data.get("access_key_id") or "").strip(),
            secret_access_key=str(data.get("secret_access_key") or "").strip(),
            prefix=str(data.get("prefix") or "gemini-web").strip().strip("/"),
            public_url=str(data.get("public_url") or "").strip().rstrip("/"),
            force_path_style=bool(data.get("force_path_style", True)),
        )

    def usable(self) -> bool:
        return bool(
            self.enabled
            and self.endpoint
            and self.bucket
            and self.access_key_id
            and self.secret_access_key
            and self.public_url
        )


def media_public_url(config: ObjectStorageConfig, key: str) -> str:
    return f"{config.public_url.rstrip('/')}/{quote(key, safe='/')}"


def build_media_object_key(
    *,
    prefix: str,
    category: str,
    data: bytes,
    content_type: str | None,
    source_url: str = "",
) -> str:
    """生成媒体对象路径；路径前缀由桶生命周期规则控制清理范围。"""
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(data).hexdigest()[:24]
    suffix = _media_suffix(content_type, source_url)
    parts = [
        _safe_part(prefix or "gemini-web"),
        _safe_path(category or "media"),
        now.strftime("%Y/%m/%d"),
        f"{digest}{suffix}",
    ]
    return posixpath.join(*(part for part in parts if part))


async def upload_s3_compatible(
    *,
    config: ObjectStorageConfig,
    key: str,
    data: bytes,
    content_type: str,
    timeout: float = 120,
) -> dict[str, Any]:
    """上传到 S3 兼容对象存储，使用 AWS Signature V4 签名。"""
    if not config.usable():
        raise ValueError("object storage is not fully configured")
    parsed = urlparse(config.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("object storage endpoint must be an http(s) URL")

    if config.force_path_style:
        host = parsed.netloc
        canonical_uri = "/" + "/".join(
            quote(part, safe="") for part in [config.bucket, *key.split("/")]
        )
        url = f"{parsed.scheme}://{host}{canonical_uri}"
    else:
        host = f"{config.bucket}.{parsed.netloc}"
        canonical_uri = "/" + "/".join(quote(part, safe="") for part in key.split("/"))
        url = f"{parsed.scheme}://{host}{canonical_uri}"
    if parsed.query:
        url = f"{url}?{parsed.query}"

    payload_hash = hashlib.sha256(data).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    credential_scope = f"{date_stamp}/{config.region}/s3/aws4_request"

    headers = {
        "content-type": content_type or "application/octet-stream",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    signed_headers = ";".join(sorted(headers))
    canonical_request = "\n".join(
        [
            "PUT",
            canonical_uri,
            parsed.query or "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _signing_key(config.secret_access_key, date_stamp, config.region)
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={config.access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.put(url, content=data, headers=headers)
    response.raise_for_status()
    return {
        "url": media_public_url(config, key),
        "key": key,
        "size": len(data),
        "content_type": content_type,
    }


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    key = ("AWS4" + secret).encode("utf-8")
    for value in (date_stamp, region, "s3", "aws4_request"):
        key = hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()
    return key


def _media_suffix(content_type: str | None, source_url: str) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = mimetypes.guess_extension(media_type) if media_type else None
    if suffix:
        return suffix
    parsed_suffix = Path(urlparse(source_url).path).suffix
    if parsed_suffix:
        return parsed_suffix[:16]
    return ".bin"


def _safe_part(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-._") or "default"


def _safe_path(value: str) -> str:
    return "/".join(_safe_part(part) for part in value.split("/") if part.strip())
