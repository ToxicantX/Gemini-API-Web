import json
import unittest
from unittest.mock import patch
from io import BytesIO
import urllib.error

from scripts import smoke_deploy


class FakeHTTPResponse:
    def __init__(
        self,
        status: int,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ):
        self.status = status
        self._body = (
            body.encode("utf-8")
            if body is not None
            else json.dumps(data or {}).encode("utf-8")
        )
        self.headers = headers or {"X-Request-ID": "req-test"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class SmokeDeployTests(unittest.TestCase):
    def test_smoke_accepts_api_key_protected_deployment_without_key(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": True},
                    },
                )
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {"X-Request-ID": "req-401"},
                BytesIO(
                    json.dumps(
                        {"error": {"message": "Invalid or missing API key."}}
                    ).encode("utf-8")
                ),
            )

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke("http://service", None)

        self.assertIn("health ok", results)
        self.assertIn("api key protection ok", results)
        self.assertIn("media cooldown protection ok", results)

    def test_smoke_checks_authorized_models_and_media_cooldowns(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            auth = request.headers.get("Authorization")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": True},
                    },
                )
            if not auth:
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {"X-Request-ID": "req-401"},
                    BytesIO(
                        json.dumps(
                            {"error": {"message": "Invalid or missing API key."}}
                        ).encode("utf-8")
                    ),
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke("http://service", "sk-test")

        self.assertIn("authorized models ok", results)
        self.assertIn("media cooldown summary ok", results)

    def test_smoke_reports_health_warnings(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                        "warnings": [
                            "ADMIN_PASSWORD is still using the Docker Compose placeholder value.",
                        ],
                    },
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke("http://service", None)

        self.assertIn("health ok", results)
        self.assertIn(
            "health warning: ADMIN_PASSWORD is still using the Docker Compose placeholder value.",
            results,
        )

    def test_smoke_can_fail_on_health_warnings(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                        "warnings": ["ADMIN_SESSION_SECRET is still using a placeholder."],
                    },
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    fail_on_warnings=True,
                )

        self.assertIn("/health returned 1 warning(s)", str(raised.exception))

    def test_smoke_checks_admin_login_when_credentials_are_provided(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                    },
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            if path == "/v1/admin/status":
                return FakeHTTPResponse(
                    200,
                    {
                        "enabled": True,
                        "username_required": True,
                        "authenticated": False,
                    },
                )
            if path == "/v1/admin/login":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body, {"username": "admin", "password": "pass"})
                return FakeHTTPResponse(
                    200,
                    {"ok": True, "enabled": True, "authenticated": True},
                    {
                        "Set-Cookie": "gemini_admin_session=abc.def; HttpOnly; Path=/",
                    },
                )
            if path == "/v1/request-logs":
                self.assertIn(
                    "gemini_admin_session=abc.def",
                    request.headers.get("Cookie", ""),
                )
                return FakeHTTPResponse(200, {"logs": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                admin_username="admin",
                admin_password="pass",
            )

        self.assertIn("admin login ok", results)

    def test_smoke_requires_admin_username_when_server_requires_it(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                    },
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            if path == "/v1/admin/status":
                return FakeHTTPResponse(
                    200,
                    {
                        "enabled": True,
                        "username_required": True,
                        "authenticated": False,
                    },
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    admin_password="pass",
                )

        self.assertIn("pass --admin-username", str(raised.exception))

    def test_smoke_can_check_real_chat_completion_shape(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            auth = request.headers.get("Authorization")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": True},
                    },
                )
            if not auth:
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {"X-Request-ID": "req-401"},
                    BytesIO(
                        json.dumps(
                            {"error": {"message": "Invalid or missing API key."}}
                        ).encode("utf-8")
                    ),
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            if path == "/v1/chat/completions":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["model"], "gemini-3.5-flash")
                self.assertEqual(body["messages"][0]["content"], "ping")
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {"X-Request-ID": "req-chat"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                chat_prompt="ping",
                chat_model="gemini-3.5-flash",
            )

        self.assertIn("chat completions ok", results)

    def test_smoke_rejects_empty_chat_completion_message(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                    },
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            if path == "/v1/chat/completions":
                return FakeHTTPResponse(
                    200,
                    {
                        "object": "chat.completion",
                        "choices": [{"message": {"role": "assistant"}}],
                    },
                    {"X-Request-ID": "req-chat"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    chat_prompt="ping",
                )

        self.assertIn("missing message content or tool_calls", str(raised.exception))

    def test_smoke_can_check_chat_completion_stream_shape(self):
        seen_stream = False

        def fake_urlopen(request, timeout):
            nonlocal seen_stream
            path = request.full_url.replace("http://service", "")
            auth = request.headers.get("Authorization")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": True},
                    },
                )
            if not auth:
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {"X-Request-ID": "req-401"},
                    BytesIO(
                        json.dumps(
                            {"error": {"message": "Invalid or missing API key."}}
                        ).encode("utf-8")
                    ),
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            if path == "/v1/chat/completions":
                body = json.loads(request.data.decode("utf-8"))
                if body.get("stream"):
                    seen_stream = True
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream; charset=utf-8",
                            "X-Request-ID": "req-stream",
                        },
                        body=(
                            'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"p"}}]}\n\n'
                            "data: [DONE]\n\n"
                        ),
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "pong"},
                            }
                        ],
                    },
                    {"X-Request-ID": "req-chat"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                chat_prompt="ping",
                chat_stream=True,
            )

        self.assertTrue(seen_stream)
        self.assertIn("chat completions ok", results)
        self.assertIn("chat stream ok", results)

    def test_smoke_rejects_stream_without_done_marker(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                    },
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            if path == "/v1/chat/completions":
                body = json.loads(request.data.decode("utf-8"))
                if body.get("stream"):
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream",
                            "X-Request-ID": "req-stream",
                        },
                        body='data: {"object":"chat.completion.chunk","choices":[]}\n\n',
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "object": "chat.completion",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "pong"},
                            }
                        ],
                    },
                    {"X-Request-ID": "req-chat"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    chat_prompt="ping",
                    chat_stream=True,
                )

        self.assertIn("missing [DONE]", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
