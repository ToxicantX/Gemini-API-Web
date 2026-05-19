from __future__ import annotations

import asyncio
import re
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .database import AccountStore


class AuthBrowserUnavailable(RuntimeError):
    pass


@dataclass
class BrowserSession:
    id: str
    created_at: float
    last_used_at: float
    playwright: Any
    browser: Any
    context: Any
    page: Any
    processes: list[Any]
    vnc_url: str | None = None


class AuthBrowserManager:
    def __init__(
        self,
        store: AccountStore,
        *,
        start_url: str = "https://gemini.google.com/",
        headless: bool = False,
    ):
        self.store = store
        self.start_url = start_url
        self.headless = headless
        self._session: BrowserSession | None = None
        self._lock = asyncio.Lock()
        self.display = ":99"
        self.vnc_port = 5901
        self.novnc_port = 6080

    async def close(self) -> None:
        await self.close_session()

    async def start_session(self) -> dict[str, Any]:
        async with self._lock:
            await self._close_current_locked()
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise AuthBrowserUnavailable(
                    "Playwright is not installed. Install with gemini-webapi[server]."
                ) from exc

            processes = await self._start_vnc_processes()
            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    env={"DISPLAY": self.display},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--window-size=1280,820",
                    ],
                )
            except Exception:
                await playwright.stop()
                await self._terminate_processes(processes)
                raise
            context = await browser.new_context(
                viewport={"width": 1280, "height": 820},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            await page.goto(self.start_url, wait_until="domcontentloaded", timeout=120000)
            session_id = uuid.uuid4().hex
            now = time.time()
            self._session = BrowserSession(
                id=session_id,
                created_at=now,
                last_used_at=now,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                processes=processes,
                vnc_url=(
                    f"/novnc/vnc.html?autoconnect=true&resize=scale"
                    f"&path=novnc/websockify"
                ),
            )
            return self._session_info(self._session)

    async def close_session(self) -> None:
        async with self._lock:
            await self._close_current_locked()

    async def screenshot(self) -> bytes:
        session = self._require_session()
        session.last_used_at = time.time()
        return await session.page.screenshot(type="png", full_page=False)

    async def click(self, x: float, y: float) -> dict[str, Any]:
        session = self._require_session()
        session.last_used_at = time.time()
        await session.page.mouse.click(x, y)
        return self._session_info(session)

    async def type_text(self, text: str) -> dict[str, Any]:
        session = self._require_session()
        session.last_used_at = time.time()
        await session.page.keyboard.type(text, delay=15)
        return self._session_info(session)

    async def press(self, key: str) -> dict[str, Any]:
        session = self._require_session()
        session.last_used_at = time.time()
        await session.page.keyboard.press(key)
        return self._session_info(session)

    async def navigate(self, url: str | None = None) -> dict[str, Any]:
        session = self._require_session()
        session.last_used_at = time.time()
        await session.page.goto(url or self.start_url, wait_until="domcontentloaded", timeout=120000)
        return self._session_info(session)

    async def save_account(self, name: str | None = None) -> dict[str, Any]:
        session = self._require_session()
        cookies_list = await session.context.cookies()
        cookies = {
            cookie["name"]: cookie["value"]
            for cookie in cookies_list
            if isinstance(cookie.get("name"), str)
            and isinstance(cookie.get("value"), str)
            and (
                "google.com" in str(cookie.get("domain", ""))
                or "gemini.google.com" in str(cookie.get("domain", ""))
            )
        }
        psid = cookies.get("__Secure-1PSID")
        psidts = cookies.get("__Secure-1PSIDTS")
        if not psid:
            raise ValueError(
                "No __Secure-1PSID cookie found. Finish Google login in the browser first."
            )

        account_name = name or await self._detect_account_name(session)
        account = self.store.upsert_account(
            name=account_name,
            secure_1psid=psid,
            secure_1psidts=psidts,
            cookies=cookies,
            enabled=True,
        )
        return {
            "ok": True,
            "account_id": account.id,
            "name": account_name,
            "cookie_count": len(cookies),
        }

    def _require_session(self) -> BrowserSession:
        if self._session is None:
            raise ValueError("No auth browser session is running.")
        return self._session

    async def _detect_account_name(self, session: BrowserSession) -> str | None:
        try:
            text = await session.page.locator("body").inner_text(timeout=5000)
            match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
            if match:
                return match.group(0).lower()
        except Exception:
            return None
        return None

    async def _close_current_locked(self) -> None:
        session = self._session
        self._session = None
        if not session:
            return
        try:
            await session.context.close()
        except Exception:
            pass
        try:
            await session.browser.close()
        except Exception:
            pass
        try:
            await session.playwright.stop()
        except Exception:
            pass
        await self._terminate_processes(session.processes)

    def _session_info(self, session: BrowserSession) -> dict[str, Any]:
        return {
            "id": session.id,
            "created_at": session.created_at,
            "last_used_at": session.last_used_at,
            "url": session.page.url,
            "headless": self.headless,
            "vnc_url": session.vnc_url,
        }

    async def _start_vnc_processes(self) -> list[Any]:
        if self.headless:
            return []
        missing = [
            name
            for name in ("Xvfb", "x11vnc", "websockify")
            if shutil.which(name) is None
        ]
        if missing:
            raise AuthBrowserUnavailable(
                "Missing VNC dependencies: " + ", ".join(missing)
            )

        processes: list[Any] = []
        xvfb = await asyncio.create_subprocess_exec(
            "Xvfb",
            self.display,
            "-screen",
            "0",
            "1280x820x24",
            "+extension",
            "RANDR",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(xvfb)
        await asyncio.sleep(0.5)

        x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display",
            self.display,
            "-rfbport",
            str(self.vnc_port),
            "-forever",
            "-nopw",
            "-shared",
            "-quiet",
            "-repeat",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(x11vnc)
        await self._wait_for_port(self.vnc_port)

        novnc_dir = "/usr/share/novnc"
        websockify = await asyncio.create_subprocess_exec(
            "websockify",
            "--web",
            novnc_dir,
            str(self.novnc_port),
            f"localhost:{self.vnc_port}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(websockify)
        await self._wait_for_port(self.novnc_port)
        return processes

    async def _terminate_processes(self, processes: list[Any]) -> None:
        for process in reversed(processes):
            if process.returncode is not None:
                continue
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    async def _wait_for_port(self, port: int, timeout: float = 15) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    return
            except OSError:
                await asyncio.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for port {port}.")
