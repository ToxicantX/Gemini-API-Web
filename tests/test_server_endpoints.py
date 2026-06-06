import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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

    def _assert_sse_headers(self, response) -> None:
        # 流式接口在反向代理后面运行时，需要明确关闭缓冲，避免客户端迟迟收不到增量。
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["connection"], "keep-alive")
        self.assertEqual(response.headers["x-accel-buffering"], "no")

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
                actual = client.get(
                    "/v1/models",
                    headers={
                        "Origin": "https://panel.example.com",
                        "Authorization": "Bearer sk-external",
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
                # 浏览器外部客户端需要读取请求号，才能把前端错误和服务端日志对应起来。
                self.assertEqual(actual.status_code, 200)
                self.assertIn(
                    "x-request-id",
                    actual.headers.get("access-control-expose-headers", "").lower(),
                )
                self.assertTrue(actual.headers["x-request-id"].startswith("req-"))
                self.assertEqual(client.get("/v1/models").status_code, 401)

    def test_health_exposes_deployment_summary_without_auth(self):
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
                admin_username="admin",
                admin_password="admin-pass",
                admin_session_secret="session-secret",
                git_commit="abc1234",
            )
            app = create_app(config)
            with TestClient(app) as client:
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.get("/health")
                healthz = client.get("/healthz")
                readyz = client.head("/readyz")
                livez = client.head("/livez")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(healthz.status_code, 200)
            self.assertEqual(readyz.status_code, 200)
            self.assertEqual(livez.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["version"], "0.1.0")
            self.assertEqual(data["build"]["commit"], "abc1234")
            self.assertIn("gemini-3.1-pro", data["models"])
            self.assertEqual(data["accounts"]["total"], 1)
            self.assertEqual(data["accounts"]["available"], 1)
            self.assertTrue(data["auth"]["admin_enabled"])
            self.assertTrue(data["auth"]["api_key_required"])
            self.assertTrue(data["auth"]["api_key_configured"])
            self.assertEqual(data["warnings"], [])
            self.assertNotIn("psid-one", response.text)
            self.assertFalse(readyz.text)

    def test_body_validation_errors_are_openai_compatible(self):
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
                response = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={"model": "gemini"},
                )

            self.assertEqual(response.status_code, 400)
            data = response.json()
            self.assertEqual(data["error"]["type"], "invalid_request_error")
            self.assertEqual(data["error"]["code"], 400)
            self.assertIn("messages", data["error"]["message"])

    def test_not_found_and_method_errors_are_openai_compatible(self):
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
                missing = client.get(
                    "/v1/not-a-route",
                    headers={"Authorization": "Bearer sk-external"},
                )
                wrong_method = client.get(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                )
                protected_management = client.get(
                    "/v1/accounts/not-a-route",
                    headers={"Authorization": "Bearer sk-external"},
                )

            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json()["error"]["type"], "invalid_request_error")
            self.assertIn("not found", missing.json()["error"]["message"])
            self.assertEqual(wrong_method.status_code, 405)
            self.assertEqual(
                wrong_method.json()["error"]["type"],
                "invalid_request_error",
            )
            self.assertIn("method is not allowed", wrong_method.json()["error"]["message"])
            self.assertEqual(protected_management.status_code, 401)
            self.assertEqual(
                protected_management.json()["detail"],
                "Admin login required.",
            )

    def test_external_responses_include_request_id_header(self):
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
                generated = client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless = client.get(
                    "/models",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless_detail = client.get(
                    "/models/GEMINI",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless_engines = client.get(
                    "/engines",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless_engine_detail = client.get(
                    "/engines/gemini-3.1-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                echoed = client.get(
                    "/v1/models",
                    headers={
                        "Authorization": "Bearer sk-external",
                        "X-Request-ID": "client-request-1",
                    },
                )
                unauthenticated = client.get("/v1/models")
                rootless_unauthenticated = client.get("/models")
                rootless_engines_unauthenticated = client.get("/engines")
                rootless_head = client.head(
                    "/models",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless_engines_head = client.head(
                    "/engines",
                    headers={"Authorization": "Bearer sk-external"},
                )

            self.assertEqual(generated.status_code, 200)
            self.assertEqual(rootless.status_code, 200)
            self.assertEqual(rootless.json()["data"], generated.json()["data"])
            self.assertEqual(rootless_detail.status_code, 200)
            self.assertEqual(rootless_detail.json()["id"], "gemini-3.1-pro")
            self.assertEqual(rootless_engines.status_code, 200)
            self.assertEqual(rootless_engine_detail.status_code, 200)
            self.assertEqual(rootless_engine_detail.json()["id"], "gemini-3.1-pro")
            self.assertTrue(generated.headers["x-request-id"].startswith("req-"))
            self.assertEqual(echoed.status_code, 200)
            self.assertEqual(echoed.headers["x-request-id"], "client-request-1")
            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(rootless_unauthenticated.status_code, 401)
            # 根路径 engines 探测也属于外部调用面，公网部署时必须和 models 一样校验 API Key。
            self.assertEqual(rootless_engines_unauthenticated.status_code, 401)
            self.assertEqual(rootless_head.status_code, 200)
            self.assertEqual(rootless_engines_head.status_code, 200)
            self.assertFalse(rootless_head.text)
            self.assertFalse(rootless_engines_head.text)
            self.assertTrue(unauthenticated.headers["x-request-id"].startswith("req-"))

    def test_v1_root_is_api_key_protected_capability_summary(self):
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
                unauthorized = client.get("/v1")
                authorized = client.get(
                    "/v1",
                    headers={"Authorization": "Bearer sk-external"},
                )
                authorized_slash = client.get(
                    "/v1/",
                    headers={"X-API-Key": "sk-external"},
                )
                unauthorized_head = client.head("/v1")
                head_root = client.head(
                    "/v1",
                    headers={"Authorization": "Bearer sk-external"},
                )
                head_models = client.head(
                    "/v1/models",
                    headers={"Authorization": "Bearer sk-external"},
                )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(authorized.status_code, 200)
            data = authorized.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["object"], "api.root")
            self.assertIn("gemini-3.1-pro", data["models"])
            self.assertEqual(
                data["endpoints"]["chat_completions"],
                "/v1/chat/completions",
            )
            self.assertEqual(
                data["endpoints"]["audio_translations"],
                "/v1/audio/translations",
            )
            self.assertEqual(authorized_slash.status_code, 200)
            self.assertEqual(unauthorized_head.status_code, 401)
            self.assertEqual(head_root.status_code, 200)
            self.assertEqual(head_models.status_code, 200)
            self.assertFalse(head_root.text)
            self.assertFalse(head_models.text)

    def test_openai_request_logs_correlate_with_response_request_id(self):
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

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content(self, prompt, **kwargs):
                return ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="ok")],
                )

            async def fake_generate_content_stream(self, prompt, **kwargs):
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="ok")],
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
                # 请求号需要同时出现在响应头和持久化日志中，方便外部客户端按同一个 id 排障。
                chat = client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer sk-external",
                        "X-Request-ID": "client-chat-1",
                    },
                    json={
                        "model": "gemini",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
                generated = client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={"model": "gemini", "input": "hello"},
                )
                stream = client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer sk-external",
                        "X-Request-ID": "client-chat-stream-1",
                    },
                    json={
                        "model": "gemini",
                        "stream": True,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(chat.status_code, 200)
            self.assertEqual(chat.headers["x-request-id"], "client-chat-1")
            self.assertEqual(stream.status_code, 200)
            self.assertEqual(stream.headers["x-request-id"], "client-chat-stream-1")
            self.assertEqual(generated.status_code, 200)
            generated_request_id = generated.headers["x-request-id"]
            self.assertTrue(generated_request_id.startswith("req-"))

            by_job_id = {log.job_id: log for log in logs}
            self.assertEqual(
                by_job_id["client-chat-1"].endpoint,
                "/v1/chat/completions",
            )
            self.assertFalse(by_job_id["client-chat-1"].stream)
            self.assertEqual(
                by_job_id["client-chat-stream-1"].endpoint,
                "/v1/chat/completions",
            )
            self.assertTrue(by_job_id["client-chat-stream-1"].stream)
            self.assertEqual(by_job_id[generated_request_id].endpoint, "/v1/responses")

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
                patch("gemini_webapi.server.app.httpx.Client", FakeMediaHTTPClient),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/gemini/generate",
                    headers={"X-Request-ID": "client-native-image-1"},
                    json={
                        "prompt": "make image",
                        "model": "gemini-3.5-flash",
                        "mode": "image",
                    },
                )
                logs = app.state.store.list_request_logs(limit=10)
                media_records = app.state.store.list_media_outputs(limit=10, kind="image")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            self.assertEqual(response.headers["x-request-id"], "client-native-image-1")
            self.assertEqual(response.json()["request_id"], "client-native-image-1")
            self.assertEqual(response.json()["media_count"], 1)
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertTrue(
                any(log.output_type == "image_generation_attempt" for log in logs)
            )
            self.assertTrue(
                any(
                    log.job_id == "client-native-image-1"
                    and log.output_type == "gemini_image"
                    and log.media_count == 1
                    for log in logs
                )
            )
            self.assertTrue(
                any(
                    item.request_id == "client-native-image-1"
                    for item in media_records
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
                lowercase_bearer = client.get(
                    "/v1/request-logs",
                    headers={"Authorization": "bearer sk-local-secret"},
                )
                self.assertEqual(lowercase_bearer.status_code, 200)
                x_api_key = client.get(
                    "/v1/request-logs",
                    headers={"X-API-Key": "sk-local-secret"},
                )
                self.assertEqual(x_api_key.status_code, 200)
                api_key = client.get(
                    "/v1/request-logs",
                    headers={"API-Key": "sk-local-secret"},
                )
                self.assertEqual(api_key.status_code, 200)
                openai_api_key = client.get(
                    "/v1/request-logs",
                    headers={"OpenAI-API-Key": "sk-local-secret"},
                )
                self.assertEqual(openai_api_key.status_code, 200)

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

    def test_require_api_key_blocks_external_calls_until_key_exists(self):
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
                api_keys=(),
                host=config.host,
                port=config.port,
                admin_username="admin",
                admin_password="admin-pass",
                admin_session_secret="session-secret",
                require_api_key=True,
            )
            app = create_app(config)
            with TestClient(app) as client:
                health = client.get("/health")
                blocked = client.get("/v1/models")
                login = client.post(
                    "/v1/admin/login",
                    json={"username": "admin", "password": "admin-pass"},
                )
                # 管理员会话仍可进入系统设置生成第一个外部调用 API Key。
                generated = client.post("/v1/system-settings/api-keys", json={})
                api_key = generated.json()["api_key"]
                authorized = client.get(
                    "/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )

            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["auth"]["api_key_required"])
            self.assertFalse(health.json()["auth"]["api_key_configured"])
            self.assertEqual(health.json()["warnings"], [])
            self.assertEqual(blocked.status_code, 401)
            self.assertIn(
                "no API key has been configured",
                blocked.json()["error"]["message"],
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(generated.status_code, 200)
            self.assertTrue(api_key.startswith("sk-gemini-"))
            self.assertEqual(authorized.status_code, 200)

    def test_health_warns_when_api_key_requirement_cannot_be_bootstrapped(self):
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
                api_keys=(),
                host=config.host,
                port=config.port,
                require_api_key=True,
            )
            app = create_app(config)
            with TestClient(app) as client:
                health = client.get("/health")
                blocked = client.get("/v1/models")

            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["auth"]["api_key_required"])
            self.assertFalse(health.json()["auth"]["api_key_configured"])
            self.assertTrue(health.json()["warnings"])
            self.assertTrue(
                any(
                    "bootstrap external access" in warning
                    for warning in health.json()["warnings"]
                )
            )
            self.assertEqual(blocked.status_code, 401)

    def test_health_warns_when_admin_login_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._config(tmp))
            with TestClient(app) as client:
                health = client.get("/health")
                management = client.get("/v1/request-logs")

            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()["auth"]["admin_enabled"])
            self.assertTrue(health.json()["warnings"])
            self.assertTrue(
                any(
                    "ADMIN_PASSWORD is not configured" in warning
                    for warning in health.json()["warnings"]
                )
            )
            self.assertEqual(management.status_code, 200)

    def test_health_warns_when_admin_login_does_not_protect_external_api(self):
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
                api_keys=(),
                host=config.host,
                port=config.port,
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            with TestClient(app) as client:
                health = client.get("/health")
                models = client.get("/v1/models")

            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["auth"]["admin_enabled"])
            self.assertFalse(health.json()["auth"]["api_key_required"])
            self.assertFalse(health.json()["auth"]["api_key_configured"])
            self.assertTrue(health.json()["warnings"])
            self.assertTrue(
                any(
                    "external /v1/* APIs" in warning
                    for warning in health.json()["warnings"]
                )
            )
            self.assertEqual(models.status_code, 200)

    def test_health_warns_when_admin_username_is_missing(self):
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
                health = client.get("/health")

            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["auth"]["admin_enabled"])
            self.assertFalse(health.json()["auth"]["admin_username_configured"])
            # 健康检查要能提示服务器仍处在“只输密码”的兼容模式，方便部署后排查。
            self.assertTrue(
                any(
                    "ADMIN_USERNAME is not configured" in warning
                    for warning in health.json()["warnings"]
                )
            )

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

    def test_admin_login_can_require_username_for_server_deployments(self):
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
                admin_username="admin",
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            with TestClient(app) as client:
                status = client.get("/v1/admin/status")
                missing_username = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )
                bad_username = client.post(
                    "/v1/admin/login",
                    headers={"X-Forwarded-For": "203.0.113.20"},
                    json={"username": "root", "password": "admin-pass"},
                )
                login = client.post(
                    "/v1/admin/login",
                    headers={"X-Forwarded-For": "203.0.113.21"},
                    json={"username": "admin", "password": "admin-pass"},
                )

            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.json()["username_required"])
            # 服务器模式可以额外要求管理员账号，避免只有密码字段时被简单猜测。
            self.assertEqual(missing_username.status_code, 401)
            self.assertEqual(bad_username.status_code, 401)
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.cookies.get("gemini_admin_session"))

    def test_admin_login_rate_limits_repeated_failures(self):
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
                for _ in range(5):
                    response = client.post(
                        "/v1/admin/login",
                        json={"password": "bad"},
                    )
                    self.assertEqual(response.status_code, 401)

                limited = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )
                other_source = client.post(
                    "/v1/admin/login",
                    headers={"X-Forwarded-For": "203.0.113.10"},
                    json={"password": "admin-pass"},
                )

            # 连续输错后同一来源会被短时间限速，避免服务器管理端被简单爆破。
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["detail"], "管理员登录失败次数过多，请稍后再试。")
            self.assertIn("Retry-After", limited.headers)
            self.assertEqual(other_source.status_code, 200)

    def test_admin_login_can_set_secure_cookie_for_https_deployments(self):
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
                admin_cookie_secure=True,
            )
            app = create_app(config)
            with TestClient(app) as client:
                login = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )

            self.assertEqual(login.status_code, 200)
            # 服务器 HTTPS 部署时允许强制 Secure Cookie，防止管理员会话在明文连接中发送。
            self.assertIn("Secure", login.headers["set-cookie"])

    def test_admin_status_and_logout_clear_session_cookie(self):
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
                status_before = client.get("/v1/admin/status")
                login = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )
                status_after = client.get("/v1/admin/status")
                logout = client.post("/v1/admin/logout", json={})
                protected = client.get("/v1/request-logs")

            self.assertEqual(status_before.status_code, 200)
            self.assertTrue(status_before.json()["enabled"])
            self.assertFalse(status_before.json()["authenticated"])
            self.assertEqual(login.status_code, 200)
            self.assertTrue(status_after.json()["authenticated"])
            self.assertEqual(logout.status_code, 200)
            # 登出必须让后续管理接口重新要求登录，避免服务器会话残留。
            self.assertIn("gemini_admin_session", logout.headers["set-cookie"])
            self.assertIn("Max-Age=0", logout.headers["set-cookie"])
            self.assertEqual(protected.status_code, 401)
            self.assertEqual(protected.json()["detail"], "Admin login required.")

    def test_admin_login_allows_native_external_api_key_paths(self):
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
                native_paths = [
                    ("GET", "/v1/gemini/gems", None),
                    ("GET", "/v1/gemini/jobs", None),
                    ("GET", "/v1/gemini/deep-research/job-missing/status", None),
                    ("POST", "/v1/gemini/deep-research/plan", {"prompt": "research"}),
                    (
                        "POST",
                        "/v1/gemini/gems",
                        {"name": "reviewer", "prompt": "review code"},
                    ),
                    (
                        "PATCH",
                        "/v1/gemini/gems/gem-missing",
                        {"name": "reviewer", "prompt": "review code"},
                    ),
                    ("DELETE", "/v1/gemini/gems/gem-missing", None),
                ]
                for method, path, payload in native_paths:
                    with self.subTest(method=method, path=path):
                        response = client.request(method, path, json=payload)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(
                            response.json()["error"]["message"],
                            "Invalid or missing API key.",
                        )

                gems = client.get(
                    "/v1/gemini/gems",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(gems.status_code, 200)
                self.assertIn("gems", gems.json())

                jobs = client.get(
                    "/v1/gemini/jobs",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(jobs.status_code, 200)
                self.assertIn("jobs", jobs.json())

                missing_job = client.get(
                    "/v1/gemini/deep-research/job-missing/status",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(missing_job.status_code, 404)

                protected_management = client.get("/v1/accounts")
                self.assertEqual(protected_management.status_code, 401)
                self.assertEqual(
                    protected_management.json()["detail"],
                    "Admin login required.",
                )

    def test_external_api_key_cannot_access_admin_management_paths(self):
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
                admin_username="admin",
                admin_password="admin-pass",
                admin_session_secret="session-secret",
            )
            app = create_app(config)
            with TestClient(app) as client:
                management_requests = [
                    ("GET", "/v1/request-logs", None),
                    ("GET", "/v1/settings", None),
                    ("PATCH", "/v1/settings", {"switch_on_uses": 20}),
                    ("GET", "/v1/system-settings", None),
                    ("PATCH", "/v1/system-settings", {"api_keys": []}),
                    ("POST", "/v1/system-settings/api-keys", {}),
                    ("GET", "/v1/accounts", None),
                    (
                        "POST",
                        "/v1/accounts",
                        {"name": "one", "secure_1psid": "psid-one"},
                    ),
                    ("POST", "/v1/accounts/switch", {"account_id": 1}),
                    ("POST", "/v1/accounts/validate", {}),
                    ("POST", "/v1/auth/session", {}),
                ]
                for method, path, payload in management_requests:
                    with self.subTest(method=method, path=path):
                        response = client.request(
                            method,
                            path,
                            headers={"Authorization": "Bearer sk-external"},
                            json=payload,
                        )
                        # API Key 只用于外部模型调用；后台状态、账号和授权操作必须走管理员会话。
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(
                            response.json()["detail"],
                            "Admin login required.",
                        )

    def test_admin_login_guards_novnc_websocket(self):
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
                with self.assertRaises(WebSocketDisconnect) as raised:
                    with client.websocket_connect("/novnc/websockify"):
                        pass
                self.assertEqual(raised.exception.code, 1008)

                login = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )
                self.assertEqual(login.status_code, 200)
                with patch("websockets.connect", side_effect=RuntimeError("stop")) as connect:
                    with client.websocket_connect("/novnc/websockify"):
                        pass
                connect.assert_called_once_with("ws://127.0.0.1:6080/websockify")

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
                alias = client.get(
                    "/v1/models/GEMINI",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(alias.status_code, 200)
                self.assertEqual(alias.json()["id"], "gemini-3.1-pro")
                missing = client.get(
                    "/v1/models/gemini-3-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                unauthenticated_head = client.head("/v1/models/gemini-3.1-pro")
                head_detail = client.head(
                    "/v1/models/gemini-3.1-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless_head_detail = client.head(
                    "/models/gemini-3.1-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                missing_head = client.head(
                    "/v1/models/gemini-3-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(unauthenticated_head.status_code, 401)
                self.assertEqual(head_detail.status_code, 200)
                self.assertEqual(rootless_head_detail.status_code, 200)
                self.assertEqual(missing_head.status_code, 404)
                self.assertFalse(head_detail.text)
                self.assertFalse(rootless_head_detail.text)

    def test_legacy_engines_endpoint_is_openai_compatible(self):
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
                unauthenticated = client.get("/v1/engines")
                engines = client.get(
                    "/v1/engines",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless = client.get(
                    "/engines",
                    headers={"Authorization": "Bearer sk-external"},
                )
                detail = client.get(
                    "/v1/engines/gemini",
                    headers={"Authorization": "Bearer sk-external"},
                )
                rootless_detail = client.get(
                    "/engines/gemini-3.5-flash",
                    headers={"Authorization": "Bearer sk-external"},
                )
                head = client.head(
                    "/v1/engines/gemini",
                    headers={"Authorization": "Bearer sk-external"},
                )
                missing = client.get(
                    "/v1/engines/gemini-3-pro",
                    headers={"Authorization": "Bearer sk-external"},
                )

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(engines.status_code, 200)
            self.assertEqual(engines.json()["object"], "list")
            self.assertEqual(engines.json()["data"][0]["object"], "engine")
            self.assertTrue(engines.json()["data"][0]["ready"])
            self.assertEqual(rootless.json()["data"], engines.json()["data"])
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["id"], "gemini-3.1-pro")
            self.assertEqual(rootless_detail.json()["id"], "gemini-3.5-flash")
            self.assertEqual(head.status_code, 200)
            self.assertFalse(head.text)
            self.assertEqual(missing.status_code, 404)

    def test_legacy_completions_endpoint_is_openai_compatible(self):
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
                    candidates=[Candidate(rcid="rcid", text="hello<END>hidden")],
                )

            async def fake_generate_content_stream(self, prompt, **kwargs):
                stream_calls.append((prompt, kwargs))
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="one ")],
                )
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="two<END>hidden")],
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
                unauthenticated = client.post(
                    "/v1/completions",
                    json={"model": "gemini", "prompt": "hello"},
                )
                response = client.post(
                    "/v1/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini-3.5-flash",
                        "prompt": "hello",
                        "suffix": "finish it",
                        "max_tokens": 16,
                        "stop": "<END>",
                    },
                )
                stream = client.post(
                    "/v1/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "prompt": ["stream me"],
                        "stream": True,
                        "stream_options": {"include_usage": True},
                        "stop": "<END>",
                    },
                )
                too_many = client.post(
                    "/v1/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={"prompt": "hello", "n": 2},
                )

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["object"], "text_completion")
            self.assertEqual(data["choices"][0]["text"], "hello")
            self.assertEqual(data["usage"]["total_tokens"], 0)
            self.assertIn("finish it", calls[0][0])
            self.assertIn("approximately 16 tokens", calls[0][0])
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(stream.status_code, 200)
            self._assert_sse_headers(stream)
            self.assertIn('"object":"text_completion.chunk"', stream.text)
            self.assertIn('"text":"one "', stream.text)
            self.assertIn('"text":"two"', stream.text)
            self.assertNotIn("hidden", stream.text)
            self.assertIn('"choices":[]', stream.text)
            self.assertIn('"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}', stream.text)
            self.assertIn("stream me", stream_calls[0][0])
            self.assertEqual(stream_calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(too_many.status_code, 400)

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
                        "max_completion_tokens": 32,
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
                        "stream_options": {"include_usage": True},
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
            self.assertIn("approximately 32 tokens", calls[0][0])
            self.assertIn("Attached file:", calls[0][0])
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertTrue(Path(calls[0][1]["files"][0]).is_file())
            self.assertEqual(stream.status_code, 200)
            self._assert_sse_headers(stream)
            self.assertIn("data: [DONE]", stream.text)
            self.assertIn('"choices":[]', stream.text)
            self.assertIn('"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}', stream.text)
            self.assertNotIn("hidden", stream.text)
            self.assertIn("JSON response mode is enabled.", stream_calls[0][0])
            self.assertIn('"required":["ok"]', stream_calls[0][0])
            self.assertEqual(stream_calls[0][1]["files"], calls[0][1]["files"])
            self.assertEqual(invalid.status_code, 400)

    def test_chat_completions_accepts_legacy_functions(self):
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
                            text='{"tool_calls":[{"name":"get_weather","arguments":{"city":"北京"}}]}',
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
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "messages": [{"role": "user", "content": "北京天气怎么样？"}],
                        "functions": [
                            {
                                "name": "get_weather",
                                "description": "Get current weather for a city.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"],
                                },
                            }
                        ],
                        "function_call": {"name": "get_weather"},
                    },
                )

            self.assertEqual(response.status_code, 200)
            message = response.json()["choices"][0]["message"]
            self.assertIsNone(message["content"])
            self.assertEqual(
                message["tool_calls"][0]["function"]["name"],
                "get_weather",
            )
            self.assertEqual(message["function_call"]["name"], "get_weather")
            self.assertEqual(
                message["function_call"]["arguments"],
                '{"city":"北京"}',
            )
            self.assertEqual(response.json()["choices"][0]["finish_reason"], "tool_calls")
            self.assertIn("You must call the tool named get_weather.", calls[0][0])

    def test_chat_completions_stream_errors_emit_sse_error_event(self):
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

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content_stream(self, prompt, **kwargs):
                raise RuntimeError("stream boom")
                yield

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content_stream", fake_generate_content_stream),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "stream": True,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self._assert_sse_headers(response)
            # 流式错误使用 SSE error 事件，方便 EventSource/SDK 直接监听。
            self.assertIn("event: error", response.text)
            self.assertIn("stream boom", response.text)
            self.assertIn("data: [DONE]", response.text)

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
                        "stream_options": {"include_usage": True},
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
            self._assert_sse_headers(stream)
            self.assertIn("event: response.created", stream.text)
            self.assertIn("event: response.output_text.delta", stream.text)
            self.assertIn('"delta":"one "', stream.text)
            self.assertIn('"delta":"two"', stream.text)
            self.assertIn("event: response.completed", stream.text)
            self.assertIn('"usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0}', stream.text)
            self.assertIn("data: [DONE]", stream.text)
            self.assertIn("System: 流式也保持 JSON", stream_calls[0][0])
            self.assertIn('"required":["ok"]', stream_calls[0][0])
            self.assertIn("User: hello", stream_calls[0][0])
            self.assertEqual(stream_calls[0][1]["files"], calls[0][1]["files"])
            self.assertEqual(stream_calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(invalid.status_code, 400)

    def test_responses_endpoint_supports_function_tools(self):
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
                            text='{"tool_calls":[{"name":"search_docs","arguments":{"query":"部署"}}]}',
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
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "input": "查一下部署说明",
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "search_docs",
                                    "description": "Search local deployment docs.",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"query": {"type": "string"}},
                                        "required": ["query"],
                                    },
                                },
                            }
                        ],
                        "tool_choice": "required",
                        "parallel_tool_calls": False,
                    },
                )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["output_text"], "")
            self.assertEqual(data["output"][0]["type"], "function_call")
            self.assertEqual(data["output"][0]["name"], "search_docs")
            self.assertEqual(data["output"][0]["arguments"], '{"query":"部署"}')
            self.assertEqual(data["output"][0]["id"], data["output"][0]["call_id"])
            # Responses API 也需要把工具约束传给 Gemini，外部工具执行器才能拿到结构化调用。
            self.assertIn("Tool calling is available.", calls[0][0])
            self.assertIn("You must call one of the available tools.", calls[0][0])
            self.assertIn("Return at most one tool call.", calls[0][0])

    def test_responses_stream_supports_function_tools(self):
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
            stream_calls = []

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content_stream(self, prompt, **kwargs):
                stream_calls.append((prompt, kwargs))
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="",
                            text_delta='{"tool_calls":[{"name":"search_docs",',
                        )
                    ],
                )
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[
                        Candidate(
                            rcid="rcid",
                            text="",
                            text_delta='"arguments":{"query":"部署"}}]}',
                        )
                    ],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content_stream", fake_generate_content_stream),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "input": "查一下部署说明",
                        "stream": True,
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "search_docs",
                                    "description": "Search local deployment docs.",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"query": {"type": "string"}},
                                        "required": ["query"],
                                    },
                                },
                            }
                        ],
                        "tool_choice": "required",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self._assert_sse_headers(response)
            self.assertIn("event: response.created", response.text)
            self.assertIn("event: response.output_item.added", response.text)
            self.assertIn('"type":"function_call"', response.text)
            self.assertIn('"name":"search_docs"', response.text)
            self.assertIn('"arguments":"{\\"query\\":\\"部署\\"}"', response.text)
            self.assertIn("event: response.completed", response.text)
            self.assertIn('"output_text":""', response.text)
            self.assertIn("data: [DONE]", response.text)
            # 工具流式结果应在结束时作为 function_call 输出，避免外部客户端把工具 JSON 当普通文本展示。
            self.assertNotIn("event: response.output_text.delta", response.text)
            self.assertIn("Tool calling is available.", stream_calls[0][0])

    def test_responses_accepts_function_call_output_input(self):
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
                    candidates=[Candidate(rcid="rcid", text="工具结果已处理。")],
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
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "input": [
                            {
                                "role": "user",
                                "content": "根据工具结果总结部署状态",
                            },
                            {
                                "type": "function_call_output",
                                "call_id": "call_123",
                                "output": {"status": "ok", "port": 7860},
                            },
                        ],
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["output_text"], "工具结果已处理。")
            self.assertIn("User: 根据工具结果总结部署状态", calls[0][0])
            # Responses 工具执行后的 function_call_output 要进入上下文，否则第二轮无法继续。
            self.assertIn('Tool result (call_123): {"status":"ok","port":7860}', calls[0][0])

    def test_responses_accepts_output_text_input_items(self):
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
                    candidates=[Candidate(rcid="rcid", text="ok")],
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
                    "/v1/responses",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "input": [
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "上一轮已经确认管理端可登录。",
                                    }
                                ],
                            },
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "继续总结外部调用状态。"}
                                ],
                            },
                        ],
                    },
                )

            self.assertEqual(response.status_code, 200)
            # 外部 Responses 客户端可能把上一轮 output_text 放回 input；这里不能丢上下文。
            self.assertIn("Assistant: 上一轮已经确认管理端可登录。", calls[0][0])
            self.assertIn("User: 继续总结外部调用状态。", calls[0][0])

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
                    files={
                        "file": ("demo.txt", b"hello", "text/plain"),
                        "purpose": (None, "fine-tune"),
                    },
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
                listed_head = client.head(
                    "/v1/files",
                    headers={"Authorization": "Bearer sk-external"},
                )
                fetched_head = client.head(
                    f"/v1/files/{file_id}",
                    headers={"Authorization": "Bearer sk-external"},
                )
                native_head = client.head(
                    "/v1/gemini/files",
                    headers={"Authorization": "Bearer sk-external"},
                )
                content = client.get(
                    f"/v1/files/{file_id}/content",
                    headers={"Authorization": "Bearer sk-external"},
                )
                content_head = client.head(
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
            self.assertEqual(upload.json()["purpose"], "fine-tune")
            self.assertIsNotNone(record)
            self.assertEqual(record.purpose, "fine-tune")
            self.assertTrue(file_exists_before_delete)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["object"], "list")
            self.assertEqual(listed.json()["data"][0]["id"], file_id)
            self.assertEqual(listed.json()["data"][0]["purpose"], "fine-tune")
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()["id"], file_id)
            self.assertEqual(fetched.json()["purpose"], "fine-tune")
            # 部分外部客户端会用 HEAD 探测文件接口是否存在，应只返回状态和头部。
            self.assertEqual(listed_head.status_code, 200)
            self.assertEqual(fetched_head.status_code, 200)
            self.assertEqual(native_head.status_code, 200)
            self.assertFalse(listed_head.content)
            self.assertFalse(fetched_head.content)
            self.assertFalse(native_head.content)
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.content, b"hello")
            self.assertEqual(content_head.status_code, 200)
            self.assertEqual(content_head.headers["content-type"], "text/plain; charset=utf-8")
            self.assertFalse(content_head.content)
            self.assertEqual(native.status_code, 200)
            self.assertEqual(native.json()["files"][0]["id"], file_id)
            self.assertEqual(deleted.status_code, 200)
            self.assertTrue(deleted.json()["deleted"])
            self.assertFalse(Path(record.path).exists())
            self.assertEqual(missing.status_code, 404)

    def test_openai_audio_transcriptions_endpoint_passes_uploaded_audio(self):
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
                    candidates=[Candidate(rcid="rcid", text="你好，世界。")],
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
                unauthenticated = client.post(
                    "/v1/audio/transcriptions",
                    data={"model": "gemini"},
                    files={"file": ("voice.mp3", b"mp3-bytes", "audio/mpeg")},
                )
                response = client.post(
                    "/v1/audio/transcriptions",
                    headers={"Authorization": "Bearer sk-external"},
                    data={
                        "model": "gemini-3.5-flash",
                        "prompt": "这是一次问候",
                        "language": "zh",
                        "response_format": "text",
                    },
                    files={"file": ("voice.mp3", b"mp3-bytes", "audio/mpeg")},
                )
                srt_response = client.post(
                    "/v1/audio/transcriptions",
                    headers={"Authorization": "Bearer sk-external"},
                    data={"response_format": "srt"},
                    files={"file": ("voice.mp3", b"mp3-bytes", "audio/mpeg")},
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "你好，世界。")
            self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
            self.assertIn("请转写上传的音频文件", calls[0][0])
            self.assertIn("音频语言提示：zh", calls[0][0])
            self.assertIn("上下文提示：这是一次问候", calls[0][0])
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertFalse(Path(calls[0][1]["files"][0]).exists())
            self.assertEqual(srt_response.status_code, 200)
            self.assertIn("00:00:00,000 --> 00:00:00,000", srt_response.text)
            self.assertIn("你好，世界。", srt_response.text)
            self.assertTrue(
                any(
                    log.endpoint == "/v1/audio/transcriptions"
                    and log.output_type == "audio_transcription"
                    for log in logs
                )
            )

    def test_openai_audio_translations_endpoint_passes_uploaded_audio(self):
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
                    candidates=[Candidate(rcid="rcid", text="Hello, world.")],
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
                unauthenticated = client.post(
                    "/v1/audio/translations",
                    data={"model": "gemini"},
                    files={"file": ("voice.mp3", b"mp3-bytes", "audio/mpeg")},
                )
                response = client.post(
                    "/v1/audio/translations",
                    headers={"Authorization": "Bearer sk-external"},
                    data={
                        "model": "gemini-3.1-pro",
                        "prompt": "口语问候",
                        "language": "zh",
                        "response_format": "verbose_json",
                    },
                    files={"file": ("voice.mp3", b"mp3-bytes", "audio/mpeg")},
                )
                vtt_response = client.post(
                    "/v1/audio/translations",
                    headers={"Authorization": "Bearer sk-external"},
                    data={"response_format": "vtt"},
                    files={"file": ("voice.mp3", b"mp3-bytes", "audio/mpeg")},
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["task"], "translate")
            self.assertEqual(data["text"], "Hello, world.")
            self.assertEqual(data["segments"], [])
            self.assertEqual(vtt_response.status_code, 200)
            self.assertTrue(vtt_response.text.startswith("WEBVTT"))
            self.assertIn("Hello, world.", vtt_response.text)
            self.assertIn("请将上传音频中的内容翻译成英文", calls[0][0])
            self.assertIn("上下文提示：口语问候", calls[0][0])
            self.assertEqual(calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertFalse(Path(calls[0][1]["files"][0]).exists())
            self.assertTrue(
                any(
                    log.endpoint == "/v1/audio/translations"
                    and log.output_type == "audio_translation"
                    for log in logs
                )
            )

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
                patch("gemini_webapi.server.app.httpx.Client", FakeMediaHTTPClient),
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
                    headers={
                        "Authorization": "Bearer sk-external",
                        "X-Request-ID": "client-image-1",
                    },
                    json={
                        "model": "gemini-3.5-flash",
                        "prompt": "make image",
                    },
                )
                media_url = response.json()["data"][0]["url"]
                content = client.get(media_url)
                content_head = client.head(media_url)
                media_list_without_key = client.get("/v1/gemini/media")
                media_list_with_key = client.get(
                    "/v1/gemini/media",
                    headers={"Authorization": "Bearer sk-external"},
                )
                base64_response = client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "prompt": "make image",
                        "response_format": "b64_json",
                    },
                )
                too_many = client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer sk-external"},
                    json={
                        "model": "gemini",
                        "prompt": "make image",
                        "n": 2,
                    },
                )
                logs = app.state.store.list_request_logs(limit=20)
                media_records = app.state.store.list_media_outputs(limit=20, kind="image")

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-request-id"], "client-image-1")
            data = response.json()
            self.assertTrue(
                data["data"][0]["url"].startswith("http://testserver/v1/gemini/media/")
            )
            self.assertEqual(data["data"][0]["revised_prompt"], "make image")
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.content, b"image-bytes")
            self.assertEqual(content_head.status_code, 200)
            self.assertEqual(content_head.headers["content-type"], "image/png")
            self.assertFalse(content_head.content)
            self.assertEqual(media_list_without_key.status_code, 401)
            self.assertEqual(media_list_with_key.status_code, 200)
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertEqual(base64_response.status_code, 200)
            self.assertEqual(
                base64_response.json()["data"][0]["b64_json"],
                "aW1hZ2UtYnl0ZXM=",
            )
            self.assertEqual(too_many.status_code, 400)
            self.assertIn("n>1", too_many.json()["error"]["message"])
            self.assertTrue(
                any(
                    log.endpoint == "/v1/images/generations"
                    and log.job_id == "client-image-1"
                    and log.output_type == "gemini_image"
                    and log.media_count == 1
                    for log in logs
                )
            )
            self.assertTrue(
                any(
                    item.request_id == "client-image-1"
                    for item in media_records
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
                patch("gemini_webapi.server.app.httpx.Client", FakeMediaHTTPClient),
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
                base64_response = client.post(
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
            self.assertTrue(
                response.json()["data"][0]["url"].startswith(
                    "http://testserver/v1/gemini/media/"
                )
            )
            self.assertEqual(calls[0][0], "edit image")
            self.assertEqual(calls[0][1]["model"], "gemini-3.5-flash")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertFalse(Path(calls[0][1]["files"][0]).exists())
            self.assertEqual(base64_response.status_code, 200)
            self.assertEqual(
                base64_response.json()["data"][0]["b64_json"],
                "aW1hZ2UtYnl0ZXM=",
            )
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
                patch("gemini_webapi.server.app.httpx.Client", FakeMediaHTTPClient),
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
                base64_response = client.post(
                    "/v1/images/variations",
                    headers={"Authorization": "Bearer sk-external"},
                    data={"response_format": "b64_json"},
                    files={"image": ("source.png", b"png-bytes", "image/png")},
                )
                logs = app.state.store.list_request_logs(limit=20)

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.json()["data"][0]["url"].startswith(
                    "http://testserver/v1/gemini/media/"
                )
            )
            self.assertEqual(calls[0][0], "基于上传图片生成新的图片变体。")
            self.assertEqual(calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(calls[0][1]["generation_mode"], "image")
            self.assertEqual(len(calls[0][1]["files"]), 1)
            self.assertFalse(Path(calls[0][1]["files"][0]).exists())
            self.assertEqual(base64_response.status_code, 200)
            self.assertEqual(
                base64_response.json()["data"][0]["b64_json"],
                "aW1hZ2UtYnl0ZXM=",
            )
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
                    headers={"X-Request-ID": "client-legacy-video-1"},
                    json={
                        "prompt": "make video",
                        "model": "gemini",
                        "mode": "video",
                    },
                )
                logs = app.state.store.list_request_logs(limit=10)
                media_records = app.state.store.list_media_outputs(limit=10, kind="video")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["text"], "ok")
            self.assertEqual(response.headers["x-request-id"], "client-legacy-video-1")
            self.assertEqual(response.json()["request_id"], "client-legacy-video-1")
            self.assertEqual(response.json()["media_count"], 1)
            self.assertEqual(calls[0][1]["model"], "gemini-3.1-pro")
            self.assertEqual(calls[0][1]["generation_mode"], "video")
            self.assertTrue(
                any(log.output_type == "video_generation_attempt" for log in logs)
            )
            self.assertTrue(
                any(
                    log.job_id == "client-legacy-video-1"
                    and log.output_type == "gemini_video"
                    and log.media_count == 1
                    for log in logs
                )
            )
            self.assertTrue(
                any(
                    item.request_id == "client-legacy-video-1"
                    and item.kind == "video"
                    for item in media_records
                )
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

    def test_media_cooldowns_are_available_to_external_api_keys(self):
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
                account = app.state.store.upsert_account(
                    secure_1psid="psid-active",
                    cookies={"__Secure-1PSID": "psid-active"},
                    name="active",
                )
                app.state.store.set_media_cooldown(
                    account_id=account.id,
                    kind="image",
                    blocked_until=(
                        datetime.now(timezone.utc) + timedelta(hours=5)
                    ).isoformat().replace("+00:00", "Z"),
                    reason="limit",
                )

                unauthenticated = client.get("/v1/media-cooldowns")
                authorized = client.get(
                    "/v1/media-cooldowns",
                    headers={"Authorization": "Bearer sk-external"},
                )
                cleared = client.post(
                    "/v1/media-cooldowns/clear",
                    headers={"Authorization": "Bearer sk-external"},
                    json={"kind": "image"},
                )
                account_scoped = client.post(
                    f"/v1/accounts/{account.id}/media-cooldowns/clear",
                    headers={"Authorization": "Bearer sk-external"},
                    json={"kind": "image"},
                )
                login = client.post(
                    "/v1/admin/login",
                    json={"password": "admin-pass"},
                )
                admin_cleared = client.post(
                    "/v1/media-cooldowns/clear",
                    json={"kind": "image"},
                )

            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(authorized.status_code, 200)
            by_kind = {item["kind"]: item for item in authorized.json()["summary"]}
            self.assertEqual(by_kind["image"]["blocked"], 1)
            # 清理媒体冷却会改变自用额度保护状态，不能仅凭外部 API Key 操作。
            self.assertEqual(cleared.status_code, 401)
            self.assertEqual(login.status_code, 200)
            self.assertEqual(admin_cleared.status_code, 200)
            self.assertEqual(admin_cleared.json()["cleared"], 1)
            # 单账号冷却清理同样属于管理操作，不能仅凭外部 API Key 操作具体账号。
            self.assertEqual(account_scoped.status_code, 401)

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

    def test_native_gemini_stream_uses_proxy_friendly_sse_headers(self):
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

            async def fake_init(self, *args, **kwargs):
                self.client = FakeSession()
                self.account_status = AccountStatus.AVAILABLE

            async def fake_close(self):
                self.client = None

            async def fake_generate_content_stream(self, prompt, **kwargs):
                yield ModelOutput(
                    metadata=["cid", "rid"],
                    candidates=[Candidate(rcid="rcid", text="", text_delta="hello")],
                )

            with (
                patch.object(GeminiClient, "init", fake_init),
                patch.object(GeminiClient, "close", fake_close),
                patch.object(GeminiClient, "generate_content_stream", fake_generate_content_stream),
                TestClient(app) as client,
            ):
                app.state.store.upsert_account(
                    secure_1psid="psid-one",
                    cookies={"__Secure-1PSID": "psid-one"},
                    name="one",
                )
                response = client.post(
                    "/v1/gemini/stream",
                    headers={
                        "Authorization": "Bearer sk-external",
                        "X-Request-ID": "client-native-stream-1",
                    },
                    json={"model": "gemini", "prompt": "stream"},
                )
                logs = app.state.store.list_request_logs(limit=10)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-request-id"], "client-native-stream-1")
            self._assert_sse_headers(response)
            self.assertIn('"type":"delta"', response.text)
            self.assertIn('"text_delta":"hello"', response.text)
            self.assertIn('"request_id":"client-native-stream-1"', response.text)
            self.assertIn("data: [DONE]", response.text)
            self.assertTrue(
                any(
                    log.job_id == "client-native-stream-1"
                    and log.endpoint == "/v1/gemini/stream"
                    and log.stream
                    for log in logs
                )
            )


if __name__ == "__main__":
    unittest.main()
