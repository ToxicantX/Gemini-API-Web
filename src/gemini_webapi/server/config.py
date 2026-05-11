from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_codes(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    codes: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            code = int(item)
        except ValueError:
            continue
        if 400 <= code <= 599:
            codes.append(code)
    return tuple(codes)


@dataclass(frozen=True)
class ServerConfig:
    database_path: Path
    accounts_file: Path | None
    switch_on_uses: int
    failure_threshold: int
    immediate_switch_status_codes: tuple[int, ...]
    proxy: str | None
    request_timeout: float
    auto_refresh: bool
    auth_url: str
    auth_headless: bool
    api_keys: tuple[str, ...]
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "ServerConfig":
        data_dir = Path(os.getenv("GEMINI_DATA_DIR", "/app/data"))
        database_path = Path(
            os.getenv("GEMINI_DATABASE_PATH", str(data_dir / "app.db"))
        )
        accounts_file_raw = os.getenv("GEMINI_ACCOUNTS_FILE", str(data_dir / "accounts.json"))
        accounts_file = Path(accounts_file_raw) if accounts_file_raw else None
        proxy = (
            os.getenv("GEMINI_PROXY")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("HTTP_PROXY")
            or os.getenv("http_proxy")
        )
        return cls(
            database_path=database_path,
            accounts_file=accounts_file,
            switch_on_uses=_env_int("SWITCH_ON_USES", 40),
            failure_threshold=_env_int("FAILURE_THRESHOLD", 3),
            immediate_switch_status_codes=_env_codes(
                "IMMEDIATE_SWITCH_STATUS_CODES", (429, 503)
            ),
            proxy=proxy,
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "300")),
            auto_refresh=os.getenv("GEMINI_AUTO_REFRESH", "true").lower()
            not in {"0", "false", "no"},
            auth_url=os.getenv("GEMINI_AUTH_URL", "https://gemini.google.com/"),
            auth_headless=os.getenv("GEMINI_AUTH_HEADLESS", "false").lower()
            in {"1", "true", "yes"},
            api_keys=tuple(
                key.strip()
                for key in os.getenv("API_KEYS", os.getenv("OPENAI_API_KEYS", "")).split(",")
                if key.strip()
            ),
            host=os.getenv("HOST", "0.0.0.0"),
            port=_env_int("PORT", 7860, minimum=1),
        )
