from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def account_identity_keys(name: str | None) -> set[str]:
    if not isinstance(name, str):
        return set()
    normalized = name.strip().lower()
    if not normalized or normalized in {"gemini web account", "gemini-webapi", "gemini webapi"}:
        return set()
    keys = {normalized}
    if normalized.startswith("gemini-") and len(normalized) > len("gemini-"):
        keys.add(normalized[len("gemini-") :])
    if "@" in normalized:
        local, domain = normalized.split("@", 1)
        if local and domain in {"gmail.com", "googlemail.com"}:
            keys.add(local)
    return keys


@dataclass(frozen=True)
class Account:
    id: int
    name: str | None
    secure_1psid: str
    secure_1psidts: str | None
    cookies: dict[str, str]
    enabled: bool
    expired: bool
    usage_count: int
    failure_count: int
    last_used_at: str | None
    validation_status: str | None
    validation_message: str | None
    validated_at: str | None


@dataclass(frozen=True)
class RequestLog:
    id: int
    time: str
    duration_ms: int
    account_id: int | None
    account_name: str | None
    endpoint: str
    model: str | None
    stream: bool
    ok: bool
    output_type: str | None
    job_id: str | None
    media_count: int
    deep_research_state: str | None
    error: str | None


@dataclass(frozen=True)
class MediaOutput:
    id: int
    request_id: str | None
    account_id: int | None
    kind: str
    title: str | None
    url: str
    thumbnail: str | None
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class JobRecord:
    id: int
    job_id: str
    job_type: str
    state: str
    account_id: int | None
    model: str | None
    prompt: str | None
    plan_json: dict[str, Any] | None
    result_json: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GemCacheRecord:
    id: str
    name: str
    description: str | None
    prompt: str | None
    predefined: bool
    updated_at: str


@dataclass(frozen=True)
class GeminiFileRecord:
    id: str
    filename: str
    content_type: str | None
    path: str
    size: int
    created_at: str


class AccountStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                secure_1psid TEXT NOT NULL UNIQUE,
                secure_1psidts TEXT,
                cookies_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                expired INTEGER NOT NULL DEFAULT 0,
                usage_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                account_id INTEGER,
                account_name TEXT,
                endpoint TEXT NOT NULL,
                model TEXT,
                stream INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL,
                output_type TEXT,
                job_id TEXT,
                media_count INTEGER NOT NULL DEFAULT 0,
                deep_research_state TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_request_logs_time
            ON request_logs(time);

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                job_type TEXT NOT NULL,
                state TEXT NOT NULL,
                account_id INTEGER,
                model TEXT,
                prompt TEXT,
                plan_json TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_type_state
            ON jobs(job_type, state);

            CREATE TABLE IF NOT EXISTS media_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT,
                account_id INTEGER,
                kind TEXT NOT NULL,
                title TEXT,
                url TEXT NOT NULL,
                thumbnail TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_media_outputs_created
            ON media_outputs(created_at);

            CREATE TABLE IF NOT EXISTS gems_cache (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                prompt TEXT,
                predefined INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gemini_files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                content_type TEXT,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_column("accounts", "validation_status", "TEXT")
        self._ensure_column("accounts", "validation_message", "TEXT")
        self._ensure_column("accounts", "validated_at", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def import_accounts_file(self, path: str | Path | None) -> int:
        if path is None:
            return 0
        source = Path(path)
        if not source.is_file():
            return 0
        data = json.loads(source.read_text(encoding="utf-8"))
        accounts = self._normalize_accounts(data)
        for account in accounts:
            self.upsert_account(**account)
        return len(accounts)

    def _normalize_accounts(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [self._normalize_account(item, index) for index, item in enumerate(data)]
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return [
                self._normalize_account(item, index)
                for index, item in enumerate(data["accounts"])
            ]
        if isinstance(data, dict):
            return [self._normalize_account(data, 0)]
        raise ValueError("Accounts file must be an object, an array, or {accounts: []}.")

    def _normalize_account(self, item: Any, index: int) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError(f"Account #{index} must be an object.")

        cookies = self._extract_cookie_map(item)
        psid = (
            item.get("secure_1psid")
            or item.get("__Secure-1PSID")
            or cookies.get("__Secure-1PSID")
        )
        psidts = (
            item.get("secure_1psidts")
            or item.get("__Secure-1PSIDTS")
            or cookies.get("__Secure-1PSIDTS")
        )
        if not isinstance(psid, str) or not psid:
            raise ValueError(f"Account #{index} is missing __Secure-1PSID.")

        cookies["__Secure-1PSID"] = psid
        if isinstance(psidts, str) and psidts:
            cookies["__Secure-1PSIDTS"] = psidts

        return {
            "name": item.get("name") or item.get("accountName"),
            "secure_1psid": psid,
            "secure_1psidts": psidts or None,
            "cookies": cookies,
            "enabled": bool(item.get("enabled", True)),
            "expired": bool(item.get("expired", False)),
        }

    def _extract_cookie_map(self, item: dict[str, Any]) -> dict[str, str]:
        raw = item.get("cookies", item)
        cookies: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, str):
                    cookies[key] = value
        elif isinstance(raw, list):
            for cookie in raw:
                if not isinstance(cookie, dict):
                    continue
                name = cookie.get("name")
                value = cookie.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    cookies[name] = value
        return cookies

    def upsert_account(
        self,
        secure_1psid: str,
        secure_1psidts: str | None = None,
        cookies: dict[str, str] | None = None,
        name: str | None = None,
        enabled: bool = True,
        expired: bool = False,
    ) -> Account:
        now = utc_now()
        cookies_json = json.dumps(cookies or {}, ensure_ascii=True, sort_keys=True)
        existing_account = self.get_account_by_psid(secure_1psid)
        if existing_account is None:
            existing_account = self.get_account_by_name_identity(name)
        if existing_account is not None and existing_account.secure_1psid != secure_1psid:
            self.conn.execute(
                """
                UPDATE accounts
                SET name = ?,
                    secure_1psid = ?,
                    secure_1psidts = ?,
                    cookies_json = ?,
                    enabled = ?,
                    expired = ?,
                    validation_status = ?,
                    validation_message = ?,
                    validated_at = ?,
                    failure_count = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    secure_1psid,
                    secure_1psidts,
                    cookies_json,
                    1 if enabled else 0,
                    1 if expired else 0,
                    "PENDING",
                    "Account cookies were updated. Validation is pending.",
                    None,
                    now,
                    existing_account.id,
                ),
            )
            self.conn.commit()
            account = self.get_account_by_psid(secure_1psid)
            if account is None:
                raise RuntimeError("Failed to load saved account.")
            return account
        self.conn.execute(
            """
            INSERT INTO accounts (
                name, secure_1psid, secure_1psidts, cookies_json,
                enabled, expired, validation_status, validation_message,
                validated_at, failure_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(secure_1psid) DO UPDATE SET
                name = excluded.name,
                secure_1psidts = excluded.secure_1psidts,
                cookies_json = excluded.cookies_json,
                enabled = excluded.enabled,
                expired = excluded.expired,
                validation_status = excluded.validation_status,
                validation_message = excluded.validation_message,
                validated_at = excluded.validated_at,
                failure_count = 0,
                updated_at = excluded.updated_at
            """,
            (
                name,
                secure_1psid,
                secure_1psidts,
                cookies_json,
                1 if enabled else 0,
                1 if expired else 0,
                "PENDING",
                "Account cookies were updated. Validation is pending.",
                None,
                now,
                now,
            ),
        )
        self.conn.commit()
        account = self.get_account_by_psid(secure_1psid)
        if account is None:
            raise RuntimeError("Failed to load saved account.")
        return account

    def list_accounts(self, include_disabled: bool = True) -> list[Account]:
        where = "" if include_disabled else "WHERE enabled = 1 AND expired = 0"
        rows = self.conn.execute(f"SELECT * FROM accounts {where} ORDER BY id").fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_account(self, account_id: int) -> Account | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return self._row_to_account(row) if row else None

    def get_account_by_psid(self, secure_1psid: str) -> Account | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE secure_1psid = ?", (secure_1psid,)
        ).fetchone()
        return self._row_to_account(row) if row else None

    def get_account_by_name_identity(self, name: str | None) -> Account | None:
        keys = account_identity_keys(name)
        if not keys:
            return None
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        for row in rows:
            if keys & account_identity_keys(row["name"]):
                return self._row_to_account(row)
        return None

    def get_active_accounts(self) -> list[Account]:
        return self.list_accounts(include_disabled=False)

    def set_account_enabled(self, account_id: int, enabled: bool) -> bool:
        cursor = self.conn.execute(
            "UPDATE accounts SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, utc_now(), account_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_account_validation(
        self,
        account_id: int,
        *,
        expired: bool,
        status: str,
        message: str | None = None,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE accounts
            SET expired = ?,
                validation_status = ?,
                validation_message = ?,
                validated_at = ?,
                failure_count = CASE WHEN ? = 0 THEN 0 ELSE failure_count END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                1 if expired else 0,
                status,
                message,
                utc_now(),
                1 if expired else 0,
                utc_now(),
                account_id,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def delete_account(self, account_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self.conn.commit()
        if self.get_state("current_account_id") == str(account_id):
            self.conn.execute("DELETE FROM runtime_state WHERE key = 'current_account_id'")
            self.conn.commit()
        return cursor.rowcount > 0

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO runtime_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def mark_success(self, account_id: int) -> None:
        self.conn.execute(
            """
            UPDATE accounts
            SET usage_count = usage_count + 1,
                failure_count = 0,
                last_used_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), utc_now(), account_id),
        )
        self.conn.commit()

    def mark_failure(self, account_id: int) -> int:
        self.conn.execute(
            """
            UPDATE accounts
            SET failure_count = failure_count + 1,
                last_used_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), utc_now(), account_id),
        )
        self.conn.commit()
        account = self.get_account(account_id)
        return account.failure_count if account else 0

    def reset_counters(self, account_id: int) -> None:
        self.conn.execute(
            """
            UPDATE accounts
            SET usage_count = 0, failure_count = 0, updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), account_id),
        )
        self.conn.commit()

    def add_request_log(
        self,
        *,
        time: str,
        duration_ms: int,
        account_id: int | None,
        account_name: str | None,
        endpoint: str,
        model: str | None = None,
        stream: bool = False,
        ok: bool,
        output_type: str | None = None,
        job_id: str | None = None,
        media_count: int = 0,
        deep_research_state: str | None = None,
        error: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO request_logs (
                time, duration_ms, account_id, account_name, endpoint, model, stream,
                ok, output_type, job_id, media_count, deep_research_state, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time,
                duration_ms,
                account_id,
                account_name,
                endpoint,
                model,
                1 if stream else 0,
                1 if ok else 0,
                output_type,
                job_id,
                media_count,
                deep_research_state,
                error,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_request_logs(self, limit: int = 80) -> list[RequestLog]:
        rows = self.conn.execute(
            "SELECT * FROM request_logs ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._row_to_request_log(row) for row in rows]

    def upsert_job(
        self,
        *,
        job_id: str,
        job_type: str,
        state: str,
        account_id: int | None = None,
        model: str | None = None,
        prompt: str | None = None,
        plan: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, state, account_id, model, prompt, plan_json,
                result_json, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                state = excluded.state,
                account_id = COALESCE(excluded.account_id, jobs.account_id),
                model = COALESCE(excluded.model, jobs.model),
                prompt = COALESCE(excluded.prompt, jobs.prompt),
                plan_json = COALESCE(excluded.plan_json, jobs.plan_json),
                result_json = COALESCE(excluded.result_json, jobs.result_json),
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                job_type,
                state,
                account_id,
                model,
                prompt,
                json.dumps(plan, ensure_ascii=True, sort_keys=True) if plan is not None else None,
                json.dumps(result, ensure_ascii=True, sort_keys=True) if result is not None else None,
                error,
                now,
                now,
            ),
        )
        self.conn.commit()

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 80, job_type: str | None = None) -> list[JobRecord]:
        if job_type:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE job_type = ? ORDER BY id DESC LIMIT ?",
                (job_type, max(1, int(limit))),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def add_media_output(
        self,
        *,
        request_id: str | None,
        account_id: int | None,
        kind: str,
        url: str,
        title: str | None = None,
        thumbnail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO media_outputs (
                request_id, account_id, kind, title, url, thumbnail,
                metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                account_id,
                kind,
                title,
                url,
                thumbnail,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
                utc_now(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_media_outputs(
        self, limit: int = 80, kind: str | None = None
    ) -> list[MediaOutput]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM media_outputs WHERE kind = ? ORDER BY id DESC LIMIT ?",
                (kind, max(1, int(limit))),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM media_outputs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_media_output(row) for row in rows]

    def replace_gems_cache(self, gems: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute("DELETE FROM gems_cache")
            self.conn.executemany(
                """
                INSERT INTO gems_cache (
                    id, name, description, prompt, predefined, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        gem["id"],
                        gem["name"],
                        gem.get("description"),
                        gem.get("prompt"),
                        1 if gem.get("predefined") else 0,
                        now,
                    )
                    for gem in gems
                ],
            )

    def list_gems_cache(self) -> list[GemCacheRecord]:
        rows = self.conn.execute("SELECT * FROM gems_cache ORDER BY name").fetchall()
        return [self._row_to_gem_cache(row) for row in rows]

    def add_file(
        self,
        *,
        file_id: str,
        filename: str,
        content_type: str | None,
        path: str,
        size: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO gemini_files (id, filename, content_type, path, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, filename, content_type, path, size, utc_now()),
        )
        self.conn.commit()

    def get_file(self, file_id: str) -> GeminiFileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM gemini_files WHERE id = ?", (file_id,)
        ).fetchone()
        return self._row_to_file(row) if row else None

    def list_files(self, limit: int = 80) -> list[GeminiFileRecord]:
        rows = self.conn.execute(
            "SELECT * FROM gemini_files ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._row_to_file(row) for row in rows]

    def _row_to_account(self, row: sqlite3.Row) -> Account:
        return Account(
            id=int(row["id"]),
            name=row["name"],
            secure_1psid=row["secure_1psid"],
            secure_1psidts=row["secure_1psidts"],
            cookies=json.loads(row["cookies_json"] or "{}"),
            enabled=bool(row["enabled"]),
            expired=bool(row["expired"]),
            usage_count=int(row["usage_count"]),
            failure_count=int(row["failure_count"]),
            last_used_at=row["last_used_at"],
            validation_status=row["validation_status"],
            validation_message=row["validation_message"],
            validated_at=row["validated_at"],
        )

    def _row_to_request_log(self, row: sqlite3.Row) -> RequestLog:
        return RequestLog(
            id=int(row["id"]),
            time=row["time"],
            duration_ms=int(row["duration_ms"]),
            account_id=row["account_id"],
            account_name=row["account_name"],
            endpoint=row["endpoint"],
            model=row["model"],
            stream=bool(row["stream"]),
            ok=bool(row["ok"]),
            output_type=row["output_type"],
            job_id=row["job_id"],
            media_count=int(row["media_count"]),
            deep_research_state=row["deep_research_state"],
            error=row["error"],
        )

    def _row_to_media_output(self, row: sqlite3.Row) -> MediaOutput:
        return MediaOutput(
            id=int(row["id"]),
            request_id=row["request_id"],
            account_id=row["account_id"],
            kind=row["kind"],
            title=row["title"],
            url=row["url"],
            thumbnail=row["thumbnail"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
        )

    def _row_to_job(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=int(row["id"]),
            job_id=row["job_id"],
            job_type=row["job_type"],
            state=row["state"],
            account_id=row["account_id"],
            model=row["model"],
            prompt=row["prompt"],
            plan_json=json.loads(row["plan_json"]) if row["plan_json"] else None,
            result_json=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_gem_cache(self, row: sqlite3.Row) -> GemCacheRecord:
        return GemCacheRecord(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            prompt=row["prompt"],
            predefined=bool(row["predefined"]),
            updated_at=row["updated_at"],
        )

    def _row_to_file(self, row: sqlite3.Row) -> GeminiFileRecord:
        return GeminiFileRecord(
            id=row["id"],
            filename=row["filename"],
            content_type=row["content_type"],
            path=row["path"],
            size=int(row["size"]),
            created_at=row["created_at"],
        )
