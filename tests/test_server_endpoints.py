import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from gemini_webapi.constants import AccountStatus
from gemini_webapi.client import GeminiClient
from gemini_webapi.server.app import create_app
from gemini_webapi.server.config import ServerConfig
from gemini_webapi.types.image import GeneratedImage
from gemini_webapi.types.video import GeneratedVideo
from gemini_webapi.types import Candidate, ModelOutput


class FakeSession:
    cookies = {}


class FakeNoVncResponse:
    content = b"<html>noVNC</html>"
    status_code = 200
    headers = {"content-type": "text/html"}


class FakeNoVncClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeNoVncResponse()


class FakeMediaResponse:
    headers = {"content-type": "image/png"}
    content = b"image-bytes"

    def raise_for_status(self):
        return None


class FakeMediaHTTPClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url):
        return FakeMediaResponse()


class FakeAsyncMediaSession:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        return FakeMediaResponse()


class ServerEndpointTests(unittest.TestCase):
    def _config(self, tmp: str) -> ServerConfig:
        return ServerConfig(
            database_path=Path(tmp) / "app.db",
            accounts_file=None,
            switch_on_uses=40,
            failure_threshold=3,
            immediate_switch_status_codes=(429, 503),
            proxy=None,
            request_timeout=300,
            auto_refresh=True,
            auth_url="https://gemini.google.com/",
            auth_headless=True,
            api_keys=(),
            host="127.0.0.1",
            port=7860,
        )

    def test_cors_preflight_supports_external_browser_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                cors_allow_origins=("https://panel.example.com",),
            )
            app = create_app(config)
            with TestClient(app) as client:
                response = client.options(
                    "/v1/chat/completions",
                    headers={
                        "Origin": "https://panel.example.com",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "authorization,content-type",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers.get("access-control-allow-origin"),
                    "https://panel.example.com",
                )
                self.assertIn(
                    "authorization",
                    response.headers.get("access-control-allow-headers", "").lower(),
                )
                self.assertEqual(client.get("/v1/models").status_code, 401)

    def test_parse_candidate_falls_back_to_nested_video_urls(self):
        client = GeminiClient()
        candidate_data = [
            None,
            ["您的视频准备好了！"],
            None,
            {
                "status": "ready",
                "placeholder": "http://googleusercontent.com/video_gen_chip/0",
                "thumb": "https://lh3.googleusercontent.com/preview/video_generation_content/thumb.jpg",
                "video": "https://rr1---sn.googlevideo.com/videoplayback?id=video-test",
            },
        ]

        _, _, _, _, videos, _ = client._parse_candidate(
            candidate_data, "cid", "rid", "rcid"
        )

        self.assertEqual(len(videos), 1)
        self.assertIsInstance(videos[0], GeneratedVideo)
        self.assertEqual(
            videos[0].url,
            "https://rr1---sn.googlevideo.com/videoplayback?id=video-test",
        )
        self.assertNotIn("video_gen_chip", videos[0].url)

    def test_parse_candidate_falls_back_to_google_download_video_urls(self):
        client = GeminiClient()
        candidate_data = [
            None,
            ["Your video is ready!\nhttp://googleusercontent.com/generated_video_content/0"],
            None,
            None,
            None,
            None,
            None,
            None,
            [2],
            None,
            None,
            None,
            [
                {
                    "60": [
                        [
                            [
                                [
                                    [
                                        None,
                                        None,
                                        "video.mp4",
                                        None,
                                        None,
                                        None,
                                        None,
                                        [
                                            "",
                                            "https://contribution.usercontent.google.com/download?filename=video.mp4&opi=103135050",
                                        ],
                                    ]
                                ]
                            ]
                        ]
                    ]
                }
            ],
        ]

        _, _, _, _, videos, _ = client._parse_candidate(
            candidate_data, "cid", "rid", "rcid"
        )

        self.assertEqual(len(videos), 1)
        self.assertIsInstance(videos[0], GeneratedVideo)
        self.assertEqual(
            videos[0].url,
            "https://contribution.usercontent.google.com/download?filename=video.mp4&opi=103135050",
        )

    def test_accounts_endpoint_lists_account_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))
            with TestClient(app) as client:
                account = app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                app.state.store.set_state("current_account_id", str(account.id))

                response = client.get("/v1/accounts")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["current_account_id"], account.id)
            self.assertEqual(len(data["accounts"]), 1)
            self.assertEqual(data["accounts"][0]["name"], "one")

    def test_novnc_proxy_uses_same_origin_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))
            FakeNoVncClient.calls = []
            with (
                patch("httpx.AsyncClient", FakeNoVncClient),
                TestClient(app) as client,
            ):
                response = client.get(
                    "/novnc/vnc.html?autoconnect=true&resize=scale&path=websockify"
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
            self.assertEqual(len(FakeNoVncClient.calls), 1)
            method, url, kwargs = FakeNoVncClient.calls[0]
            self.assertEqual(method, "GET")
            self.assertEqual(
                url,
                "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale&path=websockify",
            )
            self.assertIn("headers", kwargs)

    def test_auth_session_returns_vnc_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))

            async def fake_start_session(self):
                return {
                    "id": "auth-1",
                    "vnc_url": "/novnc/vnc.html?autoconnect=true&resize=scale&path=websockify",
                }

            with (
                patch(
                    "gemini_webapi.server.auth_browser.AuthBrowserManager.start_session",
                    fake_start_session,
                ),
                TestClient(app) as client,
            ):
                response = client.post("/v1/auth/session", json={})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["vnc_url"],
                "/novnc/vnc.html?autoconnect=true&resize=scale&path=websockify",
            )

    def test_gemini_generate_passes_media_mode_to_client_and_rotator(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                ServerConfig(
                    database_path=Path(tmp) / "app.db",
                    accounts_file=None,
                    switch_on_uses=40,
                    failure_threshold=3,
                    immediate_switch_status_codes=(429, 503),
                    proxy=None,
                    request_timeout=300,
                    auto_refresh=True,
                    auth_url="https://gemini.google.com/",
                    auth_headless=True,
                    api_keys=(),
                    host="127.0.0.1",
                    port=7860,
                )
            )
            calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_images=[
                                GeneratedImage(
                                    url="https://lh3.googleusercontent.com/generated.png",
                                    title="generated",
                                )
                            ],
                        )
                    ],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch("gemini_webapi.server.app.AsyncSession", FakeAsyncMediaSession),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/gemini/generate",
                    json={
                        "prompt": "make image",
                        "model": "gemini-3.5-flash",
                        "mode": "image",
                    },
                )
                logs = app.state.store.list_request_logs(limit=10)

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            self.assertEqual(response.json()["media_count"], 1)
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertTrue(
                any(log.output_type == "image_generation_attempt" for log in logs)
            )
            self.assertTrue(
                any(
                    log.output_type == "gemini_image" and log.media_count == 1
                    for log in logs
                )
            )

    def test_system_settings_api_keys_are_dynamic_and_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))
            with TestClient(app) as client:
                response = client.patch(
                    "/v1/system-settings",
                    json={
                        "api_keys": ["sk-local-secret"],
                        "object_storage": {
                            "enabled": True,
                            "endpoint": "https://s3.example.test",
                            "region": "auto",
                            "bucket": "media",
                            "access_key_id": "access",
                            "secret_access_key": "secret-value",
                            "prefix": "gemini-web",
                            "public_url": "https://cdn.example.test/media",
                            "force_path_style": True,
                        },
                    },
                )
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["settings"]["api_keys"][0]["masked"], "sk-l...cret")
                fingerprint = data["settings"]["api_keys"][0]["fingerprint"]
                self.assertTrue(fingerprint)
                self.assertEqual(
                    data["settings"]["object_storage"]["secret_access_key"],
                    "secr...alue",
                )
                self.assertTrue(data["object_storage_ready"])

                unauthorized = client.get("/v1/request-logs")
                self.assertEqual(unauthorized.status_code, 401)
                authorized = client.get(
                    "/v1/request-logs",
                    headers={"Authorization": "Bearer sk-local-secret"},
                )
                self.assertEqual(authorized.status_code, 200)

                generated = client.post(
                    "/v1/system-settings/api-keys",
                    headers={"Authorization": "Bearer sk-local-secret"},
                    json={},
                )
                self.assertEqual(generated.status_code, 200)
                generated_key = generated.json()["api_key"]
                self.assertTrue(generated_key.startswith("sk-gemini-"))
                generated_fp = generated.json()["fingerprint"]
                self.assertEqual(
                    client.get(
                        "/v1/request-logs",
                        headers={"Authorization": f"Bearer {generated_key}"},
                    ).status_code,
                    200,
                )
                deleted = client.delete(
                    f"/v1/system-settings/api-keys/{generated_fp}",
                    headers={"Authorization": "Bearer sk-local-secret"},
                )
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(deleted.json()["deleted"], 1)

    def test_admin_login_guards_management_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            with TestClient(app) as client:
                self.assertEqual(client.get("/v1/admin/status").status_code, 200)
                self.assertEqual(client.get("/v1/request-logs").status_code, 401)
                self.assertEqual(
                    client.get(
                        "/v1/models",
                        headers={"Authorization": "Bearer sk-external"},
                    ).status_code,
                    200,
                )
                bad = client.post("/v1/admin/login", json={"password": "bad"})
                self.assertEqual(bad.status_code, 401)
                login = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )
                self.assertEqual(login.status_code, 200)
                self.assertTrue(login.cookies.get("gemini_admin_session"))
                self.assertEqual(client.get("/v1/request-logs").status_code, 200)
                self.assertEqual(client.post("/v1/admin/logout", json={}).status_code, 200)

    def test_model_detail_endpoint_is_openai_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            with TestClient(app) as client:
                response = client.get(
                    "/v1/models/gemini-3.1-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["id"], "gemini-3.1-pro")
                self.assertEqual(response.json()["object"], "model")
                missing = client.get(
                    "/v1/models/gemini-3-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(missing.status_code, 404)

    def test_chat_completions_supports_response_format_json_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            calls = []
            stream_calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text='{"ok":true}<END>hidden')],
                )

            async def fake_generate_content_stream(self, prompt, **kwargs):
                stream_calls.append((prompt, kwargs))
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta='{"ok":')],
                )
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="true}<END>hidden")],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch.object(GeminiClient, "generate_content_stream", fake_generate_content_stream),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                upload = client.post(
                    "/v1/files",
                    headers={"Authorization": "Bearer sk-external"},
                    files={"file": ("demo.txt", b"hello", "text/plain")},
                    data={"purpose": "assistants"},
                )
                file_id = upload.json()["id"]
                response = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "stop": "<END>",
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "返回 JSON"},
                                    {"type": "input_file", "file_id": file_id},
                                ],
                            }
                        ],
                    },
                )
                stream = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "stream": True,
                        "stop": "<END>",
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "answer",
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                    "required": ["ok"],
                                },
                            },
                        },
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "返回 JSON"},
                                    {"type": "input_file", "file_id": file_id},
                                ],
                            }
                        ],
                    },
                )
                invalid = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "response_format": {"type": "xml"},
                        "messages": [{"role": "user", "content": "test"}],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], '{"ok":true}')
            self.assertIn("JSON response mode is enabled.", calls[0][0])
            self.assertIn("Attached file:", calls[0][0])
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertTrue(Path(calls[0][1]["files"][0]).is_file())
            self.assertEqual(stream.status_code, 200)
            self.assertIn("data: [DONE]", stream.text)
            self.assertNotIn("hidden", stream.text)
            self.assertIn("JSON response mode is enabled.", stream_calls[0][0])
            self.assertIn('"required":["ok"]', stream_calls[0][0])
            self.assertEqual(stream_calls[0][1]["files"], calls[0][1]["files"])
            self.assertEqual(invalid.status_code, 400)

    def test_responses_endpoint_is_openai_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            calls = []
            stream_calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="response ok")],
                )

            async def fake_generate_content_stream(self, prompt, **kwargs):
                stream_calls.append((prompt, kwargs))
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="one ")],
                )
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="two")],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch.object(GeminiClient, "generate_content_stream", fake_generate_content_stream),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                upload = client.post(
                    "/v1/files",
                    headers={"Authorization": "Bearer sk-external"},
                    files={"file": ("response.txt", b"hello", "text/plain")},
                    data={"purpose": "assistants"},
                )
                file_id = upload.json()["id"]
                unauthenticated = client.post(
                    "/v1/responses",
                    json={"model": "gemini", "input": "hello"},
                )
                response = client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini-3.5-flash",
                        "instructions": "保持简洁，只输出 JSON",
                        "text": {"format": {"type": "json_object"}},
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "请看图"},
                                    {
                                        "type": "input_image",
                                        "image_url": "https://example.com/a.png",
                                    },
                                    {"type": "input_file", "file_id": file_id},
                                ],
                            }
                        ],
                    },
                )
                stream = client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "hello"},
                                    {"type": "input_file", "file_id": file_id},
                                ],
                            }
                        ],
                        "instructions": "流式也保持 JSON",
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "json_schema": {
                                    "name": "answer",
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                        "required": ["ok"],
                                    },
                                },
                            }
                        },
                        "stream": True,
                    },
                )
                invalid = client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "input": "hello",
                        "text": {"format": {"type": "xml"}},
                    },
                )

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["object"], "response")
            self.assertEqual(data["output_text"], "response ok")
            self.assertEqual(data["output"][0]["content"][0]["type"], "output_text")
            self.assertIn("System: 保持简洁，只输出 JSON", calls[0][0])
            self.assertIn("Image URL: https://example.com/a.png", calls[0][0])
            self.assertIn("Attached file:", calls[0][0])
            self.assertIn("JSON response mode is enabled.", calls[0][0])
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertEqual(stream.status_code, 200)
            self.assertIn("event: response.created", stream.text)
            self.assertIn("event: response.output_text.delta", stream.text)
            self.assertIn('"delta":"one "', stream.text)
            self.assertIn('"delta":"two"', stream.text)
            self.assertIn("event: response.completed", stream.text)
            self.assertIn("data: [DONE]", stream.text)
            self.assertIn("System: 流式也保持 JSON", stream_calls[0][0])
            self.assertIn('"required":["ok"]', stream_calls[0][0])
            self.assertIn("User: hello", stream_calls[0][0])
            self.assertEqual(stream_calls[0][1]["files"], calls[0][1]["files"])
            self.assertEqual(stream_calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(invalid.status_code, 400)

    def test_openai_files_endpoint_reuses_gemini_file_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            with TestClient(app) as client:
                unauthenticated = client.get("/v1/files")
                upload = client.post(
                    "/v1/files",
                    headers={"Authorization": "Bearer sk-external"},
                    files={"file": ("demo.txt", b"hello", "text/plain")},
                    data={"purpose": "assistants"},
                )
                file_id = upload.json()["id"]
                record = app.state.store.get_file(file_id)
                file_exists_before_delete = Path(record.path).is_file()
                listed = client.get(
                    "/v1/files",
                    headers={"Authorization": "Bearer sk-external"},
                )
                fetched = client.get(
                    f"/v1/files/{file_id}",
                    headers={"Authorization": "Bearer sk-external"},
                )
                content = client.get(
                    f"/v1/files/{file_id}/content",
                    headers={"Authorization": "Bearer sk-external"},
                )
                native = client.get(
                    "/v1/gemini/files",
                    headers={"Authorization": "Bearer sk-external"},
                )
                deleted = client.delete(
                    f"/v1/files/{file_id}",
                    headers={"Authorization": "Bearer sk-external"},
                )
                missing = client.get(
                    f"/v1/files/{file_id}",
                    headers={"Authorization": "Bearer sk-external"},
                )

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(upload.status_code, 200)
            self.assertEqual(upload.json()["object"], "file")
            self.assertEqual(upload.json()["filename"], "demo.txt")
            self.assertEqual(upload.json()["bytes"], 5)
            self.assertIsNotNone(record)
            self.assertTrue(file_exists_before_delete)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["object"], "list")
            self.assertEqual(listed.json()["data"][0]["id"], file_id)
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()["id"], file_id)
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.content, b"hello")
            self.assertEqual(native.status_code, 200)
            self.assertEqual(native.json()["files"][0]["id"], file_id)
            self.assertEqual(deleted.status_code, 200)
            self.assertTrue(deleted.json()["deleted"])
            self.assertFalse(Path(record.path).exists())
            self.assertEqual(missing.status_code, 404)

    def test_openai_image_generation_endpoint_returns_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_images=[
                                GeneratedImage(
                                    url="https://lh3.googleusercontent.com/generated.png",
                                    title="generated",
                                )
                            ],
                        )
                    ],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch("gemini_webapi.server.app.AsyncSession", FakeAsyncMediaSession),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                unauthenticated = client.post(
                    "/v1/images/generations",
                    json={"model": "gemini", "prompt": "make image"},
                )
                response = client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini-3.5-flash",
                        "prompt": "make image",
                    },
                )
                media_url = response.json()["data"][0]["url"]
                content = client.get(media_url)
                media_list_without_key = client.get("/v1/gemini/media")
                media_list_with_key = client.get(
                    "/v1/gemini/media",
                    headers={"Authorization": "Bearer sk-external"},
                )
                unsupported = client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "prompt": "make image",
                        "response_format": "b64_json",
                    },
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["data"][0]["url"].startswith("/v1/gemini/media/"))
            self.assertEqual(data["data"][0]["revised_prompt"], "make image")
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.content, b"image-bytes")
            self.assertEqual(media_list_without_key.status_code, 401)
            self.assertEqual(media_list_with_key.status_code, 200)
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertEqual(unsupported.status_code, 400)
            self.assertTrue(
                any(
                    log.endpoint == "/v1/images/generations"
                    and log.output_type == "gemini_image"
                    and log.media_count == 1
                    for log in logs
                )
            )

    def test_openai_image_edits_endpoint_passes_uploaded_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_images=[
                                GeneratedImage(
                                    url="https://lh3.googleusercontent.com/edited.png",
                                    title="edited",
                                )
                            ],
                        )
                    ],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch("gemini_webapi.server.app.AsyncSession", FakeAsyncMediaSession),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                unauthenticated = client.post(
                    "/v1/images/edits",
                    data={"prompt": "edit image", "model": "gemini"},
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                response = client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk-external"},
                    data={
                        "prompt": "edit image",
                        "model": "gemini-3.5-flash",
                    },
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                unsupported = client.post(
                    "/v1/images/edits",
                    headers={"Authorization": "Bearer sk-external"},
                    data={
                        "prompt": "edit image",
                        "response_format": "b64_json",
                    },
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["data"][0]["url"].startswith("/v1/gemini/media/"))
            self.assertEqual(calls[0][0], "edit image")
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertFalse(Path(calls[0][1]["files"][0]).exists())
            self.assertEqual(unsupported.status_code, 400)
            self.assertTrue(
                any(
                    log.endpoint == "/v1/images/edits"
                    and log.output_type == "gemini_image"
                    and log.media_count == 1
                    for log in logs
                )
            )

    def test_openai_image_variations_endpoint_passes_uploaded_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config = ServerConfig(
                database_path=config.database_path,
                accounts_file=config.accounts_file,
                switch_on_uses=config.switch_on_uses,
                failure_threshold=config.failure_threshold,
                immediate_switch_status_codes=config.immediate_switch_status_codes,
                proxy=config.proxy,
                request_timeout=config.request_timeout,
                auto_refresh=config.auto_refresh,
                auth_url=config.auth_url,
                auth_headless=config.auth_headless,
                api_keys=("sk-external",),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_images=[
                                GeneratedImage(
                                    url="https://lh3.googleusercontent.com/variant.png",
                                    title="variant",
                                )
                            ],
                        )
                    ],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch("gemini_webapi.server.app.AsyncSession", FakeAsyncMediaSession),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                unauthenticated = client.post(
                    "/v1/images/variations",
                    data={"model": "gemini"},
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                response = client.post(
                    "/v1/images/variations",
                    headers={"Authorization": "Bearer sk-external"},
                    data={"model": "gemini-3.1-pro"},
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                unsupported = client.post(
                    "/v1/images/variations",
                    headers={"Authorization": "Bearer sk-external"},
                    data={"response_format": "b64_json"},
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["data"][0]["url"].startswith("/v1/gemini/media/"))
            self.assertEqual(calls[0][0], "基于上传图片生成新的图片变体。")
            self.assertEqual(calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertFalse(Path(calls[0][1]["files"][0]).exists())
            self.assertEqual(unsupported.status_code, 400)
            self.assertTrue(
                any(
                    log.endpoint == "/v1/images/variations"
                    and log.output_type == "gemini_image"
                    and log.media_count == 1
                    for log in logs
                )
            )

    def test_console_media_generation_can_store_media_to_object_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_images=[
                                GeneratedImage(
                                    url="https://lh3.googleusercontent.com/demo.png",
                                    title="generated",
                                )
                            ],
                        )
                    ],
                )

            async def fake_upload(**kwargs):
                return {
                    "url": "https://cdn.example.test/tmp-assets/gemini-web/image.png",
                    "key": "tmp-assets/gemini-web/image.png",
                    "size": len(kwargs["data"]),
                    "content_type": kwargs["content_type"],
                }

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch("gemini_webapi.server.app.httpx.Client", FakeMediaHTTPClient),
                patch("gemini_webapi.server.app.upload_s3_compatible", fake_upload),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                app.state.store.set_json_state(
                    "system_settings",
                    {
                        "api_keys": [],
                        "object_storage": {
                            "enabled": True,
                            "endpoint": "https://s3.example.test",
                            "region": "auto",
                            "bucket": "media",
                            "access_key_id": "access",
                            "secret_access_key": "secret",
                            "prefix": "gemini-web",
                            "public_url": "https://cdn.example.test",
                            "force_path_style": True,
                        },
                    },
                )
                response = client.post(
                    "/v1/gemini/generate",
                    json={
                        "prompt": "make image",
                        "mode": "image",
                        "store_media": True,
                    },
                )

                self.assertEqual(response.status_code, 200)
                media = client.get("/v1/gemini/media").json()["media"][0]
                self.assertTrue(media["stored"])
                self.assertEqual(
                    media["url"],
                    "https://cdn.example.test/tmp-assets/gemini-web/image.png",
                )
                self.assertEqual(media["content_url"], media["url"])
                self.assertEqual(
                    media["metadata"]["original_url"],
                    "https://lh3.googleusercontent.com/demo.png",
                )

    def test_api_media_generation_keeps_proxy_when_store_media_not_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_images=[
                                GeneratedImage(
                                    url="https://lh3.googleusercontent.com/demo.png",
                                    title="generated",
                                )
                            ],
                        )
                    ],
                )

            async def fail_upload(**kwargs):
                raise AssertionError("API calls must not upload media to object storage")

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                patch("gemini_webapi.server.app.httpx.Client", FakeMediaHTTPClient),
                patch("gemini_webapi.server.app.upload_s3_compatible", fail_upload),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                app.state.store.set_json_state(
                    "system_settings",
                    {
                        "api_keys": [],
                        "object_storage": {
                            "enabled": True,
                            "endpoint": "https://s3.example.test",
                            "region": "auto",
                            "bucket": "media",
                            "access_key_id": "access",
                            "secret_access_key": "secret",
                            "prefix": "gemini-web",
                            "public_url": "https://cdn.example.test",
                            "force_path_style": True,
                        },
                    },
                )
                response = client.post(
                    "/v1/gemini/generate",
                    json={
                        "prompt": "make image",
                        "mode": "image",
                    },
                )

                self.assertEqual(response.status_code, 200)
                media = client.get("/v1/gemini/media").json()["media"][0]
                self.assertFalse(media["stored"])
                self.assertEqual(media["url"], "https://lh3.googleusercontent.com/demo.png")

    def test_gemini_generate_media_mode_requires_media_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                ServerConfig(
                    database_path=Path(tmp) / "app.db",
                    accounts_file=None,
                    switch_on_uses=40,
                    failure_threshold=3,
                    immediate_switch_status_codes=(429, 503),
                    proxy=None,
                    request_timeout=300,
                    auto_refresh=True,
                    auth_url="https://gemini.google.com/",
                    auth_headless=True,
                    api_keys=(),
                    host="127.0.0.1",
                    port=7860,
                )
            )

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text='{"ok":true}')],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/gemini/generate",
                    json={
                        "prompt": "make image",
                        "model": "gemini-3.5-flash",
                        "mode": "image",
                    },
                )
                logs = app.state.store.list_request_logs(limit=10)

            self.assertEqual(response.status_code, 502)
            response_body = response.json()
            error_detail = response_body.get("detail") or response_body.get("error", "")
            if isinstance(error_detail, dict):
                error_detail = error_detail.get("message", "")
            self.assertIn("没有可用的图片结果", error_detail)
            self.assertTrue(
                any(
                    log.output_type == "gemini_image"
                    and not log.ok
                    and "没有可用的图片结果" in (log.error or "")
                    for log in logs
                )
            )

    def test_generate_passes_video_mode_to_client_and_rotator(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                ServerConfig(
                    database_path=Path(tmp) / "app.db",
                    accounts_file=None,
                    switch_on_uses=40,
                    failure_threshold=3,
                    immediate_switch_status_codes=(429, 503),
                    proxy=None,
                    request_timeout=300,
                    auto_refresh=True,
                    auth_url="https://gemini.google.com/",
                    auth_headless=True,
                    api_keys=(),
                    host="127.0.0.1",
                    port=7860,
                )
            )
            calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="ok",
                            generated_videos=[
                                GeneratedVideo(
                                    url="https://example.invalid/generated.mp4",
                                    title="generated",
                                )
                            ],
                        )
                    ],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content", fake_generate_content),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/generate",
                    json={
                        "prompt": "make video",
                        "model": "gemini",
                        "mode": "video",
                    },
                )
                logs = app.state.store.list_request_logs(limit=10)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["text"], "ok")
            self.assertEqual(calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(calls[0][1]["generation_mode"], "video")
            self.assertTrue(
                any(log.output_type == "video_generation_attempt" for log in logs)
            )

    def test_clear_account_media_cooldowns_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                ServerConfig(
                    database_path=Path(tmp) / "app.db",
                    accounts_file=None,
                    switch_on_uses=40,
                    failure_threshold=3,
                    immediate_switch_status_codes=(429, 503),
                    proxy=None,
                    request_timeout=300,
                    auto_refresh=True,
                    auth_url="https://gemini.google.com/",
                    auth_headless=True,
                    api_keys=(),
                    host="127.0.0.1",
                    port=7860,
                )
            )
            with TestClient(app) as client:
                account = app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                blocked_until = (
                    datetime.now(timezone.utc) + timedelta(hours=5)
                ).isoformat().replace("+00:00", "Z")
                for kind in ("image", "video"):
                    app.state.store.set_media_cooldown(
                        account_id=account.id,
                        kind=kind,
                        blocked_until=blocked_until,
                        reason=f"{kind} limit",
                    )

                response = client.post(
                    f"/v1/accounts/{account.id}/media-cooldowns/clear",
                    json={"kind": "image"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["cleared"], ["image"])
                self.assertIsNone(app.state.store.get_media_cooldown(account.id, "image"))
                self.assertIsNotNone(app.state.store.get_media_cooldown(account.id, "video"))

                response = client.post(
                    f"/v1/accounts/{account.id}/media-cooldowns/clear",
                    json={},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["cleared"], ["video"])
                self.assertIsNone(app.state.store.get_media_cooldown(account.id, "video"))

                response = client.post(
                    f"/v1/accounts/{account.id}/media-cooldowns/clear",
                    json={"kind": "bad"},
                )
                self.assertEqual(response.status_code, 400)

                response = client.post(
                    "/v1/accounts/999/media-cooldowns/clear",
                    json={},
                )
                self.assertEqual(response.status_code, 404)

    def test_media_cooldowns_summary_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                ServerConfig(
                    database_path=Path(tmp) / "app.db",
                    accounts_file=None,
                    switch_on_uses=40,
                    failure_threshold=3,
                    immediate_switch_status_codes=(429, 503),
                    proxy=None,
                    request_timeout=300,
                    auto_refresh=True,
                    auth_url="https://gemini.google.com/",
                    auth_headless=True,
                    api_keys=(),
                    host="127.0.0.1",
                    port=7860,
                )
            )
            with TestClient(app) as client:
                active = app.state.store.upsert_account(
                    secure_1psid="psid-active",
                    cookies={"__Secure-1PSID": "psid-active"},
                    name="active",
                )
                expired = app.state.store.upsert_account(
                    secure_1psid="psid-expired",
                    cookies={"__Secure-1PSID": "psid-expired"},
                    name="expired",
                )
                app.state.store.set_account_validation(
                    expired.id,
                    expired=True,
                    status="UNAUTHENTICATED",
                    message="expired",
                )
                blocked_until = (
                    datetime.now(timezone.utc) + timedelta(hours=5)
                ).isoformat().replace("+00:00", "Z")
                app.state.store.set_media_cooldown(
                    account_id=active.id,
                    kind="video",
                    blocked_until=blocked_until,
                    reason="limit",
                )
                app.state.store.set_media_cooldown(
                    account_id=expired.id,
                    kind="video",
                    blocked_until=blocked_until,
                    reason="expired account limit",
                )

                response = client.get("/v1/media-cooldowns")
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["active_account_count"], 1)
                by_kind = {item["kind"]: item for item in data["summary"]}
                self.assertEqual(by_kind["video"]["total"], 1)
                self.assertEqual(by_kind["video"]["blocked"], 1)
                self.assertEqual(by_kind["video"]["available"], 0)
                self.assertEqual(by_kind["video"]["next"]["account_id"], active.id)
                self.assertEqual(by_kind["image"]["blocked"], 0)

                response = client.post(
                    "/v1/media-cooldowns/clear",
                    json={"kind": "video"},
                )
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["cleared"], 2)
                by_kind = {item["kind"]: item for item in data["summary"]}
                self.assertEqual(by_kind["video"]["blocked"], 0)

                response = client.post(
                    "/v1/media-cooldowns/clear",
                    json={"kind": "bad"},
                )
                self.assertEqual(response.status_code, 400)

    def test_request_validation_runs_before_account_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                ServerConfig(
                    database_path=Path(tmp) / "app.db",
                    accounts_file=None,
                    switch_on_uses=40,
                    failure_threshold=3,
                    immediate_switch_status_codes=(429, 503),
                    proxy=None,
                    request_timeout=300,
                    auto_refresh=True,
                    auth_url="https://gemini.google.com/",
                    auth_headless=True,
                    api_keys=(),
                    host="127.0.0.1",
                    port=7860,
                )
            )
            cases = [
                (
                    "/v1/gemini/generate",
                    {"prompt": "test", "model": "gemini", "mode": "bad"},
                    "mode must be one of",
                ),
                (
                    "/v1/gemini/stream",
                    {"prompt": "test", "model": "gemini", "mode": "bad"},
                    "mode must be one of",
                ),
                (
                    "/v1/generate",
                    {"prompt": "test", "model": "gemini", "mode": "bad"},
                    "mode must be one of",
                ),
                (
                    "/v1/gemini/generate",
                    {"prompt": "test", "model": "gemini-3-pro"},
                    "no longer exposed",
                ),
                (
                    "/v1/chat/completions",
                    {
                        "model": "gemini-3-pro",
                        "messages": [{"role": "user", "content": "test"}],
                    },
                    "no longer exposed",
                ),
            ]

            with TestClient(app) as client:
                for path, payload, expected_message in cases:
                    # 账号池为空时，参数错误仍应先返回 400，避免被未授权 401 掩盖。
                    with self.subTest(path=path):
                        response = client.post(path, json=payload)
                        self.assertEqual(response.status_code, 400)
                        self.assertIn(
                            expected_message, response.json()["error"]["message"]
                        )


if __name__ == "__main__":
    unittest.main()
