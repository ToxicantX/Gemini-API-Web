from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from curl_cffi.requests.exceptions import HTTPError

from ..client import GeminiClient
from ..constants import AccountStatus
from ..exceptions import AuthError, TemporarilyBlocked, UsageLimitExceeded
from .database import Account, AccountStore

T = TypeVar("T")


@dataclass
class ClientSlot:
    account: Account
    client: GeminiClient
    initialized: bool = False


class AccountRotator:
    def __init__(
        self,
        store: AccountStore,
        *,
        switch_on_uses: int = 40,
        failure_threshold: int = 3,
        immediate_switch_status_codes: tuple[int, ...] = (429, 503),
        proxy: str | None = None,
        request_timeout: float = 300,
        auto_refresh: bool = True,
    ):
        self.store = store
        self.switch_on_uses = switch_on_uses
        self.failure_threshold = failure_threshold
        self.immediate_switch_status_codes = set(immediate_switch_status_codes)
        self.proxy = proxy
        self.request_timeout = request_timeout
        self.auto_refresh = auto_refresh
        self._slots: dict[int, ClientSlot] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        for slot in self._slots.values():
            await slot.client.close()
        self._slots.clear()

    def status(self) -> dict:
        current_id = self._current_account_id()
        accounts = self.store.list_accounts()
        return {
            "current_account_id": current_id,
            "switch_on_uses": self.switch_on_uses,
            "failure_threshold": self.failure_threshold,
            "immediate_switch_status_codes": sorted(self.immediate_switch_status_codes),
            "request_log_count": len(self.store.list_request_logs(limit=500)),
            "accounts": [
                {
                    "id": account.id,
                    "name": account.name,
                    "enabled": account.enabled,
                    "expired": account.expired,
                    "usage_count": account.usage_count,
                    "failure_count": account.failure_count,
                    "last_used_at": account.last_used_at,
                    "validation_status": account.validation_status,
                    "validation_message": account.validation_message,
                    "validated_at": account.validated_at,
                    "current": account.id == current_id,
                }
                for account in accounts
            ],
        }

    def configure(
        self,
        *,
        switch_on_uses: int | None = None,
        failure_threshold: int | None = None,
    ) -> None:
        if switch_on_uses is not None:
            self.switch_on_uses = max(0, int(switch_on_uses))
        if failure_threshold is not None:
            self.failure_threshold = max(0, int(failure_threshold))

    def request_logs(self, limit: int = 80) -> list[dict[str, Any]]:
        return [log.__dict__ for log in self.store.list_request_logs(limit=limit)]

    async def switch_next(self) -> Account:
        async with self._lock:
            return await self._switch_to_next_locked()

    async def switch_to(self, account_id: int) -> Account:
        async with self._lock:
            account = self.store.get_account(account_id)
            if account is None or not account.enabled or account.expired:
                raise AuthError("Target account is not available.")
            self._set_current_account_id(account.id)
            return account

    async def validate_account(self, account_id: int | None = None) -> dict[str, Any]:
        async with self._lock:
            if account_id is None:
                account_id = self._current_account_id()
            account = self.store.get_account(account_id) if account_id is not None else None
            if account is None:
                raise AuthError("Account not found.")
            result = await self._probe_account(account)
            self.store.set_account_validation(
                account.id,
                expired=not result["valid"],
                status=result["status"],
                message=result["message"],
            )
            if account.id in self._slots:
                self._slots[account.id].initialized = False
            return result

    async def validate_accounts(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for account in self.store.list_accounts():
            results.append(await self.validate_account(account.id))
        return results

    async def run(
        self,
        operation: Callable[[GeminiClient], Awaitable[T]],
        *,
        count_usage: bool = True,
        count_failure: bool = True,
        endpoint: str = "",
        model: str = "",
        output_type: str | None = None,
        job_id: str | None = None,
        media_count: int = 0,
        deep_research_state: str | None = None,
    ) -> T:
        attempts = max(1, len(self.store.get_active_accounts()))
        last_exc: Exception | None = None
        for _ in range(attempts):
            started = datetime.now(timezone.utc)
            account: Account | None = None
            try:
                async with self._lock:
                    account = self._select_current_account()
                    slot = await self._get_slot(account)
                break
            except Exception as exc:
                last_exc = exc
                if count_failure and account is not None:
                    await self._handle_failure(account.id, exc)
                    self._append_request_log(
                        account=account,
                        endpoint=endpoint,
                        model=model,
                        ok=False,
                        started=started,
                        error=str(exc),
                        output_type=output_type,
                        job_id=job_id,
                        media_count=media_count,
                        deep_research_state=deep_research_state,
                    )
                continue
        else:
            raise last_exc or AuthError("No active Gemini accounts are available. Re-authenticate or enable a valid account.")

        started = datetime.now(timezone.utc)
        while True:
            try:
                result = await operation(slot.client)
                break
            except Exception as exc:
                if getattr(slot.client, "client", None) is None:
                    slot.initialized = False
                if count_failure:
                    await self._handle_failure(slot.account.id, exc)
                self._append_request_log(
                    account=slot.account,
                    endpoint=endpoint,
                    model=model,
                    ok=False,
                    started=started,
                    error=str(exc),
                    output_type=output_type,
                    job_id=job_id,
                    media_count=media_count,
                    deep_research_state=deep_research_state,
                )
                raise

        if count_usage:
            await self._handle_success(slot.account.id)
        self._append_request_log(
            account=slot.account,
            endpoint=endpoint,
            model=model,
            ok=True,
            started=started,
            output_type=output_type,
            job_id=job_id,
            media_count=media_count,
            deep_research_state=deep_research_state,
        )
        return result

    async def run_stream(
        self,
        operation: Callable[[GeminiClient], Any],
        *,
        count_usage: bool = True,
        count_failure: bool = True,
        endpoint: str = "",
        model: str = "",
        output_type: str | None = None,
        job_id: str | None = None,
        media_count: int = 0,
        deep_research_state: str | None = None,
    ):
        attempts = max(1, len(self.store.get_active_accounts()))
        last_exc: Exception | None = None
        for _ in range(attempts):
            started = datetime.now(timezone.utc)
            account: Account | None = None
            try:
                async with self._lock:
                    account = self._select_current_account()
                    slot = await self._get_slot(account)
                break
            except Exception as exc:
                last_exc = exc
                if count_failure and account is not None:
                    await self._handle_failure(account.id, exc)
                    self._append_request_log(
                        account=account,
                        endpoint=endpoint,
                        model=model,
                        ok=False,
                        started=started,
                        error=str(exc),
                        output_type=output_type,
                        job_id=job_id,
                        media_count=media_count,
                        deep_research_state=deep_research_state,
                    )
                continue
        else:
            raise last_exc or AuthError("No active Gemini accounts are available. Re-authenticate or enable a valid account.")

        started = datetime.now(timezone.utc)
        try:
            async for item in operation(slot.client):
                yield item
        except Exception as exc:
            if getattr(slot.client, "client", None) is None:
                slot.initialized = False
            if count_failure:
                await self._handle_failure(slot.account.id, exc)
            self._append_request_log(
                account=slot.account,
                endpoint=endpoint,
                model=model,
                ok=False,
                started=started,
                error=str(exc),
                output_type=output_type,
                job_id=job_id,
                media_count=media_count,
                deep_research_state=deep_research_state,
            )
            raise

        if count_usage:
            await self._handle_success(slot.account.id)
        self._append_request_log(
            account=slot.account,
            endpoint=endpoint,
            model=model,
            ok=True,
            started=started,
            stream=True,
            output_type=output_type,
            job_id=job_id,
            media_count=media_count,
            deep_research_state=deep_research_state,
        )

    def _select_current_account(self) -> Account:
        active = self.store.get_active_accounts()
        if not active:
            raise AuthError("No active Gemini accounts are available. Re-authenticate or enable a valid account.")

        current_id = self._current_account_id()
        if current_id is not None and any(account.id == current_id for account in active):
            return next(account for account in active if account.id == current_id)

        account = active[0]
        self._set_current_account_id(account.id)
        return account

    async def _get_slot(self, account: Account) -> ClientSlot:
        slot = self._slots.get(account.id)
        if slot is None:
            client = GeminiClient(
                secure_1psid=account.secure_1psid,
                secure_1psidts=account.secure_1psidts or "",
                proxy=self.proxy,
            )
            extra_cookies = {
                key: value
                for key, value in account.cookies.items()
                if key not in {"__Secure-1PSID", "__Secure-1PSIDTS"}
            }
            if extra_cookies:
                client.cookies = extra_cookies
            slot = ClientSlot(account=account, client=client)
            self._slots[account.id] = slot

        if not slot.initialized or getattr(slot.client, "client", None) is None:
            await slot.client.init(
                timeout=self.request_timeout,
                auto_refresh=self.auto_refresh,
            )
            status = getattr(slot.client, "account_status", AccountStatus.AVAILABLE)
            self._record_account_status(account.id, status)
            if status != AccountStatus.AVAILABLE:
                await slot.client.close()
                slot.initialized = False
                raise AuthError(
                    f"Account status: {status.name} - {status.description}"
                )
            slot.initialized = True
        return slot

    async def _probe_account(self, account: Account) -> dict[str, Any]:
        client = GeminiClient(
            secure_1psid=account.secure_1psid,
            secure_1psidts=account.secure_1psidts or "",
            proxy=self.proxy,
        )
        extra_cookies = {
            key: value
            for key, value in account.cookies.items()
            if key not in {"__Secure-1PSID", "__Secure-1PSIDTS"}
        }
        if extra_cookies:
            client.cookies = extra_cookies
        try:
            await client.init(
                timeout=self.request_timeout,
                auto_refresh=False,
            )
            status = getattr(client, "account_status", AccountStatus.AVAILABLE)
            valid = status == AccountStatus.AVAILABLE
            return {
                "account_id": account.id,
                "valid": valid,
                "expired": not valid,
                "status": status.name,
                "status_code": int(status),
                "message": status.description,
            }
        except Exception as exc:
            return {
                "account_id": account.id,
                "valid": False,
                "expired": True,
                "status": type(exc).__name__,
                "status_code": None,
                "message": str(exc),
            }
        finally:
            await client.close()

    def _record_account_status(self, account_id: int, status: AccountStatus) -> None:
        self.store.set_account_validation(
            account_id,
            expired=status != AccountStatus.AVAILABLE,
            status=status.name,
            message=status.description,
        )

    @staticmethod
    def _validation_from_auth_error(exc: AuthError) -> tuple[str, str]:
        message = str(exc)
        prefix = "Account status: "
        if not message.startswith(prefix):
            return type(exc).__name__, message
        status_message = message[len(prefix) :]
        if " - " not in status_message:
            return type(exc).__name__, message
        status, description = status_message.split(" - ", 1)
        return status, description

    async def _handle_success(self, account_id: int) -> None:
        async with self._lock:
            self.store.mark_success(account_id)
            account = self.store.get_account(account_id)
            if (
                self.switch_on_uses > 0
                and account is not None
                and account.usage_count >= self.switch_on_uses
            ):
                self.store.reset_counters(account_id)
                await self._switch_to_next_locked(account_id)

    async def _handle_failure(self, account_id: int, exc: Exception) -> None:
        async with self._lock:
            if isinstance(exc, AuthError):
                status, message = self._validation_from_auth_error(exc)
                self.store.set_account_validation(
                    account_id,
                    expired=True,
                    status=status,
                    message=message,
                )
            failure_count = self.store.mark_failure(account_id)
            if self._should_switch_immediately(exc) or (
                self.failure_threshold > 0 and failure_count >= self.failure_threshold
            ):
                await self._switch_to_next_locked(account_id)

    async def _switch_to_next_locked(self, current_account_id: int | None = None) -> Account:
        active = self.store.get_active_accounts()
        if not active:
            raise AuthError("No active Gemini accounts are available. Re-authenticate or enable a valid account.")
        ids = [account.id for account in active]
        if current_account_id not in ids:
            current_account_id = self._current_account_id()
        if current_account_id in ids:
            next_index = (ids.index(current_account_id) + 1) % len(ids)
        else:
            next_index = 0
        next_account = active[next_index]
        self._set_current_account_id(next_account.id)
        self.store.reset_counters(next_account.id)
        return next_account

    def _should_switch_immediately(self, exc: Exception) -> bool:
        if isinstance(exc, (AuthError, TemporarilyBlocked, UsageLimitExceeded)):
            return True
        status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if isinstance(exc, HTTPError) and response is not None:
            status = getattr(response, "status_code", status)
        return isinstance(status, int) and status in self.immediate_switch_status_codes

    def _current_account_id(self) -> int | None:
        raw = self.store.get_state("current_account_id")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _set_current_account_id(self, account_id: int) -> None:
        self.store.set_state("current_account_id", str(account_id))

    def _append_request_log(
        self,
        *,
        account: Account,
        endpoint: str,
        model: str,
        ok: bool,
        started: datetime,
        error: str | None = None,
        stream: bool = False,
        output_type: str | None = None,
        job_id: str | None = None,
        media_count: int = 0,
        deep_research_state: str | None = None,
    ) -> None:
        ended = datetime.now(timezone.utc)
        self.store.add_request_log(
            time=ended.isoformat().replace("+00:00", "Z"),
            duration_ms=int((ended - started).total_seconds() * 1000),
            account_id=account.id,
            account_name=account.name,
            endpoint=endpoint or "-",
            model=model or "-",
            stream=stream,
            ok=ok,
            output_type=output_type,
            job_id=job_id,
            media_count=media_count,
            deep_research_state=deep_research_state,
            error=error,
        )
