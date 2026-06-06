import json
import tempfile
import unittest
from unittest.mock import patch
from io import BytesIO
from io import StringIO
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
    def test_cli_help_describes_full_cors_probe_scope(self):
        with patch("sys.argv", ["smoke_deploy.py", "--help"]), patch(
            "sys.stdout",
            new_callable=StringIO,
        ) as stdout:
            with self.assertRaises(SystemExit) as raised:
                smoke_deploy.main()

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--cors-probes", help_text)
        self.assertIn("preflight and actual response", help_text)

    def test_smoke_uses_configurable_request_timeout(self):
        timeouts = []

        def fake_urlopen(request, timeout):
            timeouts.append(timeout)
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
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            smoke_deploy.run_smoke("http://service", None, timeout=45)

        self.assertEqual(timeouts, [45, 45, 45])

    def test_smoke_can_check_health_probe_aliases(self):
        seen = []

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
            if path in {"/healthz", "/readyz", "/livez"}:
                seen.append((request.get_method(), path))
                if request.get_method() == "HEAD":
                    return FakeHTTPResponse(
                        200,
                        headers={"X-Request-ID": f"req-head-{path[1:]}"},
                        body="",
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": False},
                    },
                    {"X-Request-ID": f"req-get-{path[1:]}"},
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
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                health_probes=True,
            )

        self.assertEqual(
            seen,
            [
                ("GET", "/healthz"),
                ("HEAD", "/healthz"),
                ("GET", "/readyz"),
                ("HEAD", "/readyz"),
                ("GET", "/livez"),
                ("HEAD", "/livez"),
            ],
        )
        self.assertIn("health probes ok", results)

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

    def test_smoke_validates_media_cooldown_summary_shape(self):
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
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "active_account_count": 2,
                        "summary": [
                            {
                                "kind": "image",
                                "label": "图片",
                                "total": 2,
                                "blocked": 1,
                                "available": 1,
                                "next": {
                                    "account_id": 1,
                                    "remaining_seconds": 300,
                                },
                            }
                        ],
                    },
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke("http://service", None)

        self.assertIn("media cooldown summary ok", results)

    def test_smoke_rejects_invalid_media_cooldown_summary_shape(self):
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
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "summary": [
                            {
                                "kind": "video",
                                "total": 1,
                                "blocked": 0,
                                "available": 2,
                            }
                        ],
                    },
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke("http://service", None)

        self.assertIn("available count exceeds total", str(raised.exception))

    def test_smoke_can_use_x_api_key_header(self):
        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            auth = request.headers.get("X-api-key")
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
            self.assertEqual(auth, "sk-test")
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                api_key_header="x-api-key",
            )

        self.assertIn("authorized models ok", results)
        self.assertIn("media cooldown summary ok", results)

    def test_smoke_can_check_cors_preflight(self):
        seen_preflight = False
        seen_actual = False

        def fake_urlopen(request, timeout):
            nonlocal seen_actual, seen_preflight
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
            if path == "/v1/chat/completions" and request.get_method() == "OPTIONS":
                seen_preflight = True
                self.assertEqual(request.headers["Origin"], "https://panel.example.com")
                self.assertEqual(
                    request.headers["Access-control-request-method"],
                    "POST",
                )
                self.assertEqual(request.headers["X-request-id"], "smoke-cors-preflight")
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Access-Control-Allow-Origin": "https://panel.example.com",
                        "Access-Control-Allow-Headers": "authorization, content-type",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "X-Request-ID": "smoke-cors-preflight",
                    },
                    body="",
                )
            if path == "/v1/models":
                if request.headers.get("Origin") == "https://panel.example.com":
                    seen_actual = True
                    return FakeHTTPResponse(
                        200,
                        {"object": "list", "data": [{"id": "gemini"}]},
                        {
                            "Access-Control-Allow-Origin": "https://panel.example.com",
                            "Access-Control-Expose-Headers": "X-Request-ID",
                            "X-Request-ID": "req-cors-models",
                        },
                    )
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                cors_probes=True,
                cors_origin="https://panel.example.com",
            )

        self.assertTrue(seen_preflight)
        self.assertTrue(seen_actual)
        self.assertIn("cors preflight ok", results)
        self.assertIn("cors actual response ok", results)

    def test_smoke_rejects_cors_preflight_without_authorization_header(self):
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
            if path == "/v1/chat/completions" and request.get_method() == "OPTIONS":
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Access-Control-Allow-Origin": "https://panel.example.com",
                        "Access-Control-Allow-Headers": "content-type",
                        "Access-Control-Allow-Methods": "POST",
                        "X-Request-ID": "smoke-cors-preflight",
                    },
                    body="",
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
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    cors_probes=True,
                    cors_origin="https://panel.example.com",
                )

        self.assertIn("missing authorization", str(raised.exception))

    def test_smoke_rejects_cors_preflight_without_request_id(self):
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
            if path == "/v1/chat/completions" and request.get_method() == "OPTIONS":
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Access-Control-Allow-Origin": "https://panel.example.com",
                        "Access-Control-Allow-Headers": "authorization, content-type",
                        "Access-Control-Allow-Methods": "POST",
                    },
                    body="",
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
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    cors_probes=True,
                    cors_origin="https://panel.example.com",
                )

        self.assertIn("preflight missing X-Request-ID", str(raised.exception))

    def test_smoke_rejects_cors_actual_response_without_exposed_request_id(self):
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
            if path == "/v1/chat/completions" and request.get_method() == "OPTIONS":
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Access-Control-Allow-Origin": "https://panel.example.com",
                        "Access-Control-Allow-Headers": "authorization, content-type",
                        "Access-Control-Allow-Methods": "POST",
                        "X-Request-ID": "smoke-cors-preflight",
                    },
                    body="",
                )
            if path == "/v1/models":
                if request.headers.get("Origin") == "https://panel.example.com":
                    return FakeHTTPResponse(
                        200,
                        {"object": "list", "data": [{"id": "gemini"}]},
                        {
                            "Access-Control-Allow-Origin": "https://panel.example.com",
                            "X-Request-ID": "req-cors-models",
                        },
                    )
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    cors_probes=True,
                    cors_origin="https://panel.example.com",
                )

        self.assertIn("does not expose X-Request-ID", str(raised.exception))

    def test_smoke_can_probe_supported_auth_headers(self):
        seen_headers = []

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
            if path == "/v1/models":
                auth_headers = {
                    key.lower(): value
                    for key, value in request.headers.items()
                    if key.lower() in {"authorization", "x-api-key", "api-key", "openai-api-key"}
                }
                if not auth_headers:
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
                seen_headers.append(auth_headers)
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                    {"X-Request-ID": "req-models"},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                auth_header_probes=True,
            )

        self.assertEqual(
            seen_headers,
            [
                {"authorization": "Bearer sk-test"},
                {"authorization": "Bearer sk-test"},
                {"x-api-key": "sk-test"},
                {"api-key": "sk-test"},
                {"openai-api-key": "sk-test"},
            ],
        )
        self.assertIn("auth header probes ok", results)

    def test_smoke_can_check_native_read_only_probes(self):
        seen = []

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
            if path == "/v1/gemini/gems":
                seen.append(path)
                return FakeHTTPResponse(200, {"gems": []})
            if path == "/v1/gemini/jobs":
                seen.append(path)
                return FakeHTTPResponse(200, {"jobs": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                native_probes=True,
            )

        self.assertEqual(seen, ["/v1/gemini/gems", "/v1/gemini/jobs"])
        self.assertIn("native probes ok", results)

    def test_smoke_checks_native_probe_protection_without_api_key(self):
        seen = []

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
            seen.append(path)
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
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                native_probes=True,
            )

        self.assertEqual(
            seen,
            [
                "/v1/models",
                "/v1/media-cooldowns",
                "/v1/gemini/gems",
                "/v1/gemini/jobs",
            ],
        )
        self.assertIn("native probe protection ok", results)

    def test_smoke_resets_api_key_header_after_failure(self):
        calls = []

        def failing_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            calls.append((path, dict(request.headers)))
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

        with patch("urllib.request.urlopen", failing_urlopen):
            with self.assertRaises(AssertionError):
                smoke_deploy.run_smoke(
                    "http://service",
                    "sk-test",
                    api_key_header="x-api-key",
                )

        def succeeding_urlopen(request, timeout):
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
            if path == "/v1/models":
                if not request.headers.get("Authorization"):
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
                self.assertEqual(request.headers.get("Authorization"), "Bearer sk-test")
                self.assertIsNone(request.headers.get("X-api-key"))
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/v1/media-cooldowns":
                self.assertEqual(request.headers.get("Authorization"), "Bearer sk-test")
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", succeeding_urlopen):
            results = smoke_deploy.run_smoke("http://service", "sk-test")

        self.assertTrue(any("X-api-key" in headers for _, headers in calls))
        self.assertIn("authorized models ok", results)

    def test_smoke_can_check_endpoint_probes(self):
        seen_heads = []

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
                if request.get_method() == "HEAD":
                    seen_heads.append(path)
                    return FakeHTTPResponse(
                        200,
                        headers={"X-Request-ID": "req-models-head"},
                        body="",
                    )
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                )
            if path == "/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                    {"X-Request-ID": "req-models-alias"},
                )
            if path == "/models/gemini":
                self.assertEqual(request.get_method(), "HEAD")
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": "req-model-alias-head"},
                    body="",
                )
            if path == "/v1/engines":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                    {"X-Request-ID": "req-engines"},
                )
            if path == "/v1/engines/gemini":
                self.assertEqual(request.get_method(), "HEAD")
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": "req-engine-head"},
                    body="",
                )
            if path == "/v1":
                if request.get_method() == "HEAD":
                    seen_heads.append(path)
                    return FakeHTTPResponse(
                        200,
                        headers={"X-Request-ID": "req-v1-root-head"},
                        body="",
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "object": "api.root",
                        "endpoints": {"models": "/v1/models"},
                    },
                    {"X-Request-ID": "req-v1-root"},
                )
            if path == "/v1/models/gemini":
                return FakeHTTPResponse(
                    200,
                    {"id": "gemini", "object": "model"},
                    {"X-Request-ID": "req-model-detail"},
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                probe_endpoints=True,
            )

        self.assertEqual(seen_heads, ["/v1", "/v1/models"])
        self.assertIn("endpoint probes ok", results)

    def test_smoke_checks_endpoint_probe_api_key_protection(self):
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
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                probe_endpoints=True,
            )

        self.assertIn("endpoint probe protection ok", results)

    def test_smoke_can_check_openai_compatible_error_responses(self):
        seen = []

        def openai_error(status, error_type, request_id, message):
            return {
                "error": {
                    "message": message,
                    "type": error_type,
                    "code": status,
                    "request_id": request_id,
                },
                "request_id": request_id,
            }

        def fake_urlopen(request, timeout):
            path = request.full_url.replace("http://service", "")
            auth = request.headers.get("Authorization")
            seen.append((request.get_method(), path, bool(auth)))
            if path == "/health":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "models": ["gemini"],
                        "auth": {"api_key_required": True},
                    },
                )
            if path == "/v1/models" and not auth:
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {"X-Request-ID": "req-unauth"},
                    BytesIO(
                        json.dumps(
                            openai_error(
                                401,
                                "authentication_error",
                                "req-unauth",
                                "Invalid or missing API key.",
                            )
                        ).encode("utf-8")
                    ),
                )
            if path == "/v1/models":
                return FakeHTTPResponse(
                    200,
                    {"object": "list", "data": [{"id": "gemini"}]},
                    {"X-Request-ID": "req-models"},
                )
            if path == "/v1/not-a-real-smoke-endpoint":
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "Not Found",
                    {"X-Request-ID": "req-missing"},
                    BytesIO(
                        json.dumps(
                            openai_error(
                                404,
                                "invalid_request_error",
                                "req-missing",
                                "The requested endpoint was not found.",
                            )
                        ).encode("utf-8")
                    ),
                )
            if path == "/v1/chat/completions" and request.get_method() == "GET":
                raise urllib.error.HTTPError(
                    request.full_url,
                    405,
                    "Method Not Allowed",
                    {"X-Request-ID": "req-method"},
                    BytesIO(
                        json.dumps(
                            openai_error(
                                405,
                                "invalid_request_error",
                                "req-method",
                                "The requested method is not allowed.",
                            )
                        ).encode("utf-8")
                    ),
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                error_probes=True,
            )

        self.assertIn(("GET", "/v1/not-a-real-smoke-endpoint", True), seen)
        self.assertIn(("GET", "/v1/chat/completions", True), seen)
        self.assertIn("error probes ok", results)

    def test_smoke_can_check_file_head_probes(self):
        seen_heads = []

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
            if path in {"/v1/files", "/v1/gemini/files"}:
                self.assertEqual(request.get_method(), "HEAD")
                seen_heads.append(path)
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": f"req-head-{path.rsplit('/', 1)[-1]}"},
                    body="",
                )
            if path == "/v1/media-cooldowns":
                return FakeHTTPResponse(200, {"ok": True, "summary": []})
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                file_probes=True,
            )

        self.assertEqual(seen_heads, ["/v1/files", "/v1/gemini/files"])
        self.assertIn("file probes ok", results)

    def test_smoke_can_check_file_lifecycle(self):
        uploaded_file_id = "file-smoke-1"
        seen_delete = False

        def fake_urlopen(request, timeout):
            nonlocal seen_delete
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
            if path == "/v1/files" and request.get_method() == "POST":
                body = request.data.decode("utf-8", errors="replace")
                self.assertIn('name="purpose"', body)
                self.assertIn("assistants", body)
                self.assertIn('name="file"; filename="smoke.txt"', body)
                return FakeHTTPResponse(
                    200,
                    {
                        "id": uploaded_file_id,
                        "object": "file",
                        "filename": "smoke.txt",
                        "bytes": 11,
                        "purpose": "assistants",
                    },
                    {"X-Request-ID": "req-file-upload"},
                )
            if path == "/v1/files" and request.get_method() == "GET":
                return FakeHTTPResponse(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": uploaded_file_id,
                                "object": "file",
                                "filename": "smoke.txt",
                                "bytes": 11,
                                "purpose": "assistants",
                            }
                        ],
                    },
                    {"X-Request-ID": "req-file-list"},
                )
            if path == f"/v1/files/{uploaded_file_id}" and request.get_method() == "GET":
                if seen_delete:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        404,
                        "Not Found",
                        {"X-Request-ID": "req-file-missing"},
                        BytesIO(json.dumps({"error": {"message": "not found"}}).encode("utf-8")),
                    )
                return FakeHTTPResponse(
                    200,
                    {"id": uploaded_file_id, "object": "file", "purpose": "assistants"},
                    {"X-Request-ID": "req-file-detail"},
                )
            if path == f"/v1/files/{uploaded_file_id}" and request.get_method() == "HEAD":
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": "req-file-detail-head"},
                    body="",
                )
            if path == f"/v1/files/{uploaded_file_id}/content" and request.get_method() == "GET":
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": "req-file-content"},
                    body="hello smoke",
                )
            if path == f"/v1/files/{uploaded_file_id}/content" and request.get_method() == "HEAD":
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": "req-file-content-head"},
                    body="",
                )
            if path == "/v1/gemini/files":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "files": [
                            {
                                "id": uploaded_file_id,
                                "filename": "smoke.txt",
                                "size": 11,
                            }
                        ],
                    },
                    {"X-Request-ID": "req-native-files"},
                )
            if path == f"/v1/files/{uploaded_file_id}" and request.get_method() == "DELETE":
                seen_delete = True
                return FakeHTTPResponse(
                    200,
                    {"id": uploaded_file_id, "object": "file", "deleted": True},
                    {"X-Request-ID": "req-file-delete"},
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            smoke_file = f"{tmp}\\smoke.txt"
            with open(smoke_file, "w", encoding="utf-8") as handle:
                handle.write("hello smoke")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    "sk-test",
                    file_smoke_path=smoke_file,
                )

        self.assertTrue(seen_delete)
        self.assertIn("file lifecycle ok", results)

    def test_smoke_checks_file_lifecycle_api_key_protection(self):
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

        with tempfile.TemporaryDirectory() as tmp:
            smoke_file = f"{tmp}\\smoke.txt"
            with open(smoke_file, "w", encoding="utf-8") as handle:
                handle.write("hello smoke")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    file_smoke_path=smoke_file,
                )

        self.assertIn("file lifecycle protection ok", results)

    def test_smoke_checks_file_probe_api_key_protection(self):
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
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                file_probes=True,
            )

        self.assertIn("file probe protection ok", results)

    def test_smoke_can_check_media_history_shape(self):
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
            if path == "/v1/gemini/media?limit=5":
                return FakeHTTPResponse(
                    200,
                    {
                        "media": [
                            {
                                "kind": "image",
                                "url": "https://cdn.example.test/image.png",
                                "content_url": "/v1/gemini/media/token/content",
                            }
                        ]
                    },
                    {"X-Request-ID": "req-media-history"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                media_history=True,
            )

        self.assertIn("media history ok", results)

    def test_smoke_can_probe_media_content_links(self):
        seen_head = []

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
            if not auth and path not in {"/v1/gemini/media/token/content", "/v1/gemini/media/absolute/content"}:
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
            if path == "/v1/gemini/media?limit=5":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "media": [
                            {
                                "kind": "image",
                                "url": "https://lh3.googleusercontent.com/demo.png",
                                "content_url": "/v1/gemini/media/token/content",
                            },
                            {
                                "kind": "video",
                                "url": "https://googlevideo.com/demo.mp4",
                                "content_url": "http://service/v1/gemini/media/absolute/content",
                            },
                        ],
                    },
                    {"X-Request-ID": "req-media-history"},
                )
            if path in {"/v1/gemini/media/token/content", "/v1/gemini/media/absolute/content"}:
                self.assertEqual(request.get_method(), "HEAD")
                seen_head.append(path)
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Content-Type": "image/png" if "token" in path else "video/mp4",
                        "X-Request-ID": "req-media-head",
                    },
                    body="",
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                media_history=True,
                media_content_probes=True,
                media_content_probe_limit=2,
            )

        self.assertEqual(
            seen_head,
            ["/v1/gemini/media/token/content", "/v1/gemini/media/absolute/content"],
        )
        self.assertIn("media content probes ok (2)", results)

    def test_smoke_skips_media_content_probe_without_records(self):
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
            if path == "/v1/gemini/media?limit=5":
                return FakeHTTPResponse(
                    200,
                    {"ok": True, "media": []},
                    {"X-Request-ID": "req-media-history"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                media_history=True,
                media_content_probes=True,
            )

        self.assertIn("media history ok", results)
        self.assertIn("media content probes skipped: no media records", results)

    def test_smoke_checks_media_history_api_key_protection(self):
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
            results = smoke_deploy.run_smoke(
                "http://service",
                None,
                media_history=True,
            )

        self.assertIn("media history protection ok", results)

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
            if path == "/v1/admin/logout":
                self.assertIn(
                    "gemini_admin_session=abc.def",
                    request.headers.get("Cookie", ""),
                )
                return FakeHTTPResponse(
                    200,
                    {"ok": True},
                    {
                        "Set-Cookie": "gemini_admin_session=\"\"; Max-Age=0; Path=/",
                    },
                )
            if path == "/v1/request-logs":
                if "gemini_admin_session=abc.def" not in request.headers.get("Cookie", ""):
                    raise urllib.error.HTTPError(
                        request.full_url,
                        401,
                        "Unauthorized",
                        {"X-Request-ID": "req-admin-401"},
                        BytesIO(
                            json.dumps(
                                {"ok": False, "detail": "Admin login required."}
                            ).encode("utf-8")
                        ),
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

        self.assertIn("admin login and boundary ok", results)

    def test_smoke_rejects_api_key_on_admin_management_endpoints(self):
        seen_admin_api_key = False

        def fake_urlopen(request, timeout):
            nonlocal seen_admin_api_key
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
            if path == "/v1/models":
                if not request.headers.get("Authorization"):
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
                        "username_required": False,
                        "authenticated": False,
                    },
                )
            if path == "/v1/admin/login":
                return FakeHTTPResponse(
                    200,
                    {"ok": True, "enabled": True, "authenticated": True},
                    {
                        "Set-Cookie": "gemini_admin_session=abc.def; HttpOnly; Path=/",
                    },
                )
            if path == "/v1/admin/logout":
                self.assertIn(
                    "gemini_admin_session=abc.def",
                    request.headers.get("Cookie", ""),
                )
                return FakeHTTPResponse(
                    200,
                    {"ok": True},
                    {
                        "Set-Cookie": "gemini_admin_session=\"\"; Max-Age=0; Path=/",
                    },
                )
            if path == "/v1/request-logs":
                if "gemini_admin_session=abc.def" in request.headers.get("Cookie", ""):
                    return FakeHTTPResponse(200, {"logs": []})
                if request.headers.get("Authorization") == "Bearer sk-test":
                    seen_admin_api_key = True
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {"X-Request-ID": "req-admin-401"},
                    BytesIO(
                        json.dumps(
                            {"ok": False, "detail": "Admin login required."}
                        ).encode("utf-8")
                    ),
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                admin_password="pass",
            )

        self.assertTrue(seen_admin_api_key)
        self.assertIn("admin login and boundary ok", results)

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
                        "created": 1,
                        "model": "gemini-3.5-flash",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
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
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
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

    def test_smoke_rejects_chat_completion_without_usage(self):
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
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
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
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    chat_prompt="ping",
                )

        self.assertIn("chat response missing usage", str(raised.exception))

    def test_smoke_can_check_chat_tool_calls_shape(self):
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
                self.assertEqual(body["model"], "gemini")
                self.assertEqual(body["tools"][0]["function"]["name"], "echo_tool")
                self.assertEqual(
                    body["tool_choice"]["function"]["name"],
                    "echo_tool",
                )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "chatcmpl-tool-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_tool_probe",
                                            "type": "function",
                                            "function": {
                                                "name": "echo_tool",
                                                "arguments": '{"message":"smoke"}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
                    },
                    {"X-Request-ID": "req-tool"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                chat_tool_probe=True,
            )

        self.assertIn("chat tool_calls ok", results)

    def test_smoke_rejects_chat_tool_calls_with_invalid_arguments(self):
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
                        "id": "chatcmpl-tool-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_tool_probe",
                                            "type": "function",
                                            "function": {
                                                "name": "echo_tool",
                                                "arguments": "not-json",
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
                    },
                    {"X-Request-ID": "req-tool"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    chat_tool_probe=True,
                )

        self.assertIn("arguments are not valid JSON", str(raised.exception))

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
                    self.assertEqual(body["stream_options"], {"include_usage": True})
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream; charset=utf-8",
                            "X-Request-ID": "req-stream",
                        },
                        body=(
                            'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1,"model":"gemini","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
                            'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1,"model":"gemini","choices":[{"index":0,"delta":{"content":"p"},"finish_reason":null}]}\n\n'
                            'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1,"model":"gemini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                            'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1,"model":"gemini","choices":[],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}\n\n'
                            "data: [DONE]\n\n"
                        ),
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
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
                        body='data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1,"model":"gemini","choices":[{"index":0,"delta":{"content":"p"},"finish_reason":null}]}\n\n',
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
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

    def test_smoke_rejects_stream_without_final_finish_reason_chunk(self):
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
                        body=(
                            'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1,"model":"gemini","choices":[{"index":0,"delta":{"content":"p"},"finish_reason":null}]}\n\n'
                            "data: [DONE]\n\n"
                        ),
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gemini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
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

        self.assertIn("missing final finish_reason", str(raised.exception))

    def test_smoke_can_check_image_generation_shape(self):
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
            if path == "/v1/images/generations":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["model"], "gpt-image-2")
                self.assertEqual(body["prompt"], "draw a small icon")
                self.assertEqual(body["response_format"], "url")
                return FakeHTTPResponse(
                    200,
                    {
                        "created": 1,
                        "data": [
                            {
                                "url": "http://service/v1/gemini/media/token/content",
                                "revised_prompt": "draw a small icon",
                            }
                        ],
                    },
                    {"X-Request-ID": "req-image"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                image_prompt="draw a small icon",
                image_model="gpt-image-2",
            )

        self.assertIn("image generation ok", results)

    def test_smoke_can_check_image_edit_shape(self):
        seen_edit = False

        def fake_urlopen(request, timeout):
            nonlocal seen_edit
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
            if path == "/v1/images/edits":
                seen_edit = True
                body = request.data.decode("utf-8", errors="replace")
                self.assertIn('name="model"', body)
                self.assertIn("gpt-image-2", body)
                self.assertIn('name="prompt"', body)
                self.assertIn("make it blue", body)
                self.assertIn('name="image"; filename="source.png"', body)
                self.assertIn('name="mask"; filename="mask.png"', body)
                return FakeHTTPResponse(
                    200,
                    {
                        "created": 1,
                        "data": [
                            {
                                "url": "http://service/v1/gemini/media/edit-token/content",
                                "revised_prompt": "make it blue",
                            }
                        ],
                    },
                    {"X-Request-ID": "req-image-edit"},
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            source_file = f"{tmp}\\source.png"
            mask_file = f"{tmp}\\mask.png"
            with open(source_file, "wb") as handle:
                handle.write(b"\x89PNG\r\n")
            with open(mask_file, "wb") as handle:
                handle.write(b"\x89PNG\r\n")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    "sk-test",
                    image_model="gpt-image-2",
                    image_edit_file=source_file,
                    image_edit_prompt="make it blue",
                    image_edit_mask_file=mask_file,
                )

        self.assertTrue(seen_edit)
        self.assertIn("image edit ok", results)

    def test_smoke_can_check_image_variation_shape(self):
        seen_variation = False

        def fake_urlopen(request, timeout):
            nonlocal seen_variation
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
            if path == "/v1/images/variations":
                seen_variation = True
                body = request.data.decode("utf-8", errors="replace")
                self.assertIn('name="model"', body)
                self.assertIn("gemini", body)
                self.assertIn('name="image"; filename="source.png"', body)
                return FakeHTTPResponse(
                    200,
                    {
                        "created": 1,
                        "data": [
                            {
                                "url": "http://service/v1/gemini/media/variation-token/content",
                                "revised_prompt": "variation",
                            }
                        ],
                    },
                    {"X-Request-ID": "req-image-variation"},
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            source_file = f"{tmp}\\source.png"
            with open(source_file, "wb") as handle:
                handle.write(b"\x89PNG\r\n")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    image_variation_file=source_file,
                )

        self.assertTrue(seen_variation)
        self.assertIn("image variation ok", results)

    def test_smoke_requires_image_edit_prompt(self):
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
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            source_file = f"{tmp}\\source.png"
            with open(source_file, "wb") as handle:
                handle.write(b"\x89PNG\r\n")
            with patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(AssertionError) as raised:
                    smoke_deploy.run_smoke(
                        "http://service",
                        None,
                        image_edit_file=source_file,
                    )

        self.assertIn("--image-edit-prompt", str(raised.exception))

    def test_smoke_can_check_audio_transcription_shape(self):
        seen_audio = False

        def fake_urlopen(request, timeout):
            nonlocal seen_audio
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
            if path == "/v1/audio/transcriptions":
                seen_audio = True
                body = request.data.decode("utf-8", errors="replace")
                self.assertIn('name="model"', body)
                self.assertIn("gemini-3.5-flash", body)
                self.assertIn('name="file"; filename="sample.wav"', body)
                return FakeHTTPResponse(
                    200,
                    {"text": "transcribed text"},
                    {"X-Request-ID": "req-audio-transcription"},
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            audio_file = f"{tmp}\\sample.wav"
            with open(audio_file, "wb") as handle:
                handle.write(b"RIFF....WAVEfmt ")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    "sk-test",
                    audio_transcription_file=audio_file,
                    audio_model="gemini-3.5-flash",
                )

        self.assertTrue(seen_audio)
        self.assertIn("audio transcription ok", results)

    def test_smoke_can_check_audio_translation_shape(self):
        seen_audio = False

        def fake_urlopen(request, timeout):
            nonlocal seen_audio
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
            if path == "/v1/audio/translations":
                seen_audio = True
                body = request.data.decode("utf-8", errors="replace")
                self.assertIn('name="response_format"', body)
                self.assertIn("srt", body)
                self.assertIn('name="file"; filename="sample.mp3"', body)
                return FakeHTTPResponse(
                    200,
                    headers={"X-Request-ID": "req-audio-translation"},
                    body="1\n00:00:00,000 --> 00:00:00,000\ntranslated text\n",
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            audio_file = f"{tmp}\\sample.mp3"
            with open(audio_file, "wb") as handle:
                handle.write(b"ID3")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    audio_translation_file=audio_file,
                    audio_response_format="srt",
                )

        self.assertTrue(seen_audio)
        self.assertIn("audio translation ok", results)

    def test_smoke_can_check_audio_verbose_json_shape(self):
        seen_audio = False

        def fake_urlopen(request, timeout):
            nonlocal seen_audio
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
            if path == "/v1/audio/transcriptions":
                seen_audio = True
                body = request.data.decode("utf-8", errors="replace")
                self.assertIn("verbose_json", body)
                return FakeHTTPResponse(
                    200,
                    {"text": "transcribed text", "segments": []},
                    {"X-Request-ID": "req-audio-translation"},
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            audio_file = f"{tmp}\\sample.wav"
            with open(audio_file, "wb") as handle:
                handle.write(b"RIFF")
            with patch("urllib.request.urlopen", fake_urlopen):
                results = smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    audio_transcription_file=audio_file,
                    audio_response_format="verbose_json",
                )

        self.assertTrue(seen_audio)
        self.assertIn("audio transcription ok", results)

    def test_smoke_rejects_audio_response_without_text(self):
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
            if path == "/v1/audio/transcriptions":
                return FakeHTTPResponse(
                    200,
                    {"text": ""},
                    {"X-Request-ID": "req-audio"},
                )
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            audio_file = f"{tmp}\\sample.wav"
            with open(audio_file, "wb") as handle:
                handle.write(b"RIFF")
            with patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(AssertionError) as raised:
                    smoke_deploy.run_smoke(
                        "http://service",
                        None,
                        audio_transcription_file=audio_file,
                    )

        self.assertIn("audio transcription response missing text", str(raised.exception))

    def test_smoke_rejects_image_generation_without_data(self):
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
            if path == "/v1/images/generations":
                return FakeHTTPResponse(
                    200,
                    {"created": 1, "data": []},
                    {"X-Request-ID": "req-image"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    image_prompt="draw",
                )

        self.assertIn("image response missing data", str(raised.exception))

    def test_smoke_can_check_responses_api_shape(self):
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
            if path == "/v1/responses":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["model"], "gemini-3.1-pro")
                self.assertEqual(body["input"], "ping")
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "resp_test",
                        "object": "response",
                        "output_text": "pong",
                        "output": [
                            {
                                "id": "msg_test",
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "pong",
                                    }
                                ],
                            }
                        ],
                    },
                    {"X-Request-ID": "req-responses"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                responses_prompt="ping",
                responses_model="gemini-3.1-pro",
            )

        self.assertIn("responses api ok", results)

    def test_smoke_rejects_responses_without_output(self):
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
            if path == "/v1/responses":
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "resp_test",
                        "object": "response",
                        "output_text": "",
                        "output": [],
                    },
                    {"X-Request-ID": "req-responses"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    responses_prompt="ping",
                )

        self.assertIn("responses body missing output", str(raised.exception))

    def test_smoke_can_check_responses_stream_shape(self):
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
            if path == "/v1/responses":
                body = json.loads(request.data.decode("utf-8"))
                if body.get("stream"):
                    seen_stream = True
                    self.assertEqual(body["stream_options"], {"include_usage": True})
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream; charset=utf-8",
                            "X-Request-ID": "req-responses-stream",
                        },
                        body=(
                            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"p"}\n\n'
                            'event: response.completed\ndata: {"type":"response.completed","response":{"object":"response","usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0}}}\n\n'
                            "data: [DONE]\n\n"
                        ),
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "resp_test",
                        "object": "response",
                        "output_text": "pong",
                        "output": [
                            {
                                "id": "msg_test",
                                "type": "message",
                                "content": [{"type": "output_text", "text": "pong"}],
                            }
                        ],
                    },
                    {"X-Request-ID": "req-responses"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                responses_prompt="ping",
                responses_stream=True,
            )

        self.assertTrue(seen_stream)
        self.assertIn("responses api ok", results)
        self.assertIn("responses stream ok", results)

    def test_smoke_rejects_responses_stream_without_completed_event(self):
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
            if path == "/v1/responses":
                body = json.loads(request.data.decode("utf-8"))
                if body.get("stream"):
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream",
                            "X-Request-ID": "req-responses-stream",
                        },
                        body='event: response.output_text.delta\ndata: {"delta":"p"}\n\ndata: [DONE]\n\n',
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "resp_test",
                        "object": "response",
                        "output_text": "pong",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "pong"}],
                            }
                        ],
                    },
                    {"X-Request-ID": "req-responses"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    responses_prompt="ping",
                    responses_stream=True,
                )

        self.assertIn("missing response.completed", str(raised.exception))

    def test_smoke_can_check_completions_api_shape(self):
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
            if path == "/v1/completions":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["model"], "gemini-3.5-flash")
                self.assertEqual(body["prompt"], "complete this")
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "cmpl-test",
                        "object": "text_completion",
                        "choices": [
                            {
                                "index": 0,
                                "text": " done",
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {"X-Request-ID": "req-completion"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                completion_prompt="complete this",
                completion_model="gemini-3.5-flash",
            )

        self.assertIn("completions api ok", results)

    def test_smoke_rejects_completion_without_choices(self):
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
            if path == "/v1/completions":
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "cmpl-test",
                        "object": "text_completion",
                        "choices": [],
                    },
                    {"X-Request-ID": "req-completion"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    completion_prompt="complete this",
                )

        self.assertIn("completion body missing choices", str(raised.exception))

    def test_smoke_can_check_completions_stream_shape(self):
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
            if path == "/v1/completions":
                body = json.loads(request.data.decode("utf-8"))
                if body.get("stream"):
                    seen_stream = True
                    self.assertEqual(body["stream_options"], {"include_usage": True})
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream; charset=utf-8",
                            "X-Request-ID": "req-completion-stream",
                        },
                        body=(
                            'data: {"object":"text_completion.chunk","choices":[{"text":" done"}]}\n\n'
                            'data: {"object":"text_completion.chunk","choices":[],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}\n\n'
                            "data: [DONE]\n\n"
                        ),
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "cmpl-test",
                        "object": "text_completion",
                        "choices": [{"index": 0, "text": " done"}],
                    },
                    {"X-Request-ID": "req-completion"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                completion_prompt="complete this",
                completion_stream=True,
            )

        self.assertTrue(seen_stream)
        self.assertIn("completions api ok", results)
        self.assertIn("completions stream ok", results)

    def test_smoke_rejects_completion_stream_without_done_marker(self):
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
            if path == "/v1/completions":
                body = json.loads(request.data.decode("utf-8"))
                if body.get("stream"):
                    return FakeHTTPResponse(
                        200,
                        headers={
                            "Content-Type": "text/event-stream",
                            "X-Request-ID": "req-completion-stream",
                        },
                        body='data: {"object":"text_completion.chunk","choices":[]}\n\n',
                    )
                return FakeHTTPResponse(
                    200,
                    {
                        "id": "cmpl-test",
                        "object": "text_completion",
                        "choices": [{"index": 0, "text": " done"}],
                    },
                    {"X-Request-ID": "req-completion"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    completion_prompt="complete this",
                    completion_stream=True,
                )

        self.assertIn("completion stream missing [DONE]", str(raised.exception))

    def test_smoke_can_check_gemini_native_generate_shape(self):
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
            if path == "/v1/gemini/generate":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["model"], "gemini-3.1-pro")
                self.assertEqual(body["prompt"], "native ping")
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "account": 1,
                        "model": "gemini-3.1-pro",
                        "metadata": {"request_id": "req-gemini"},
                        "output": {
                            "text": "native pong",
                            "thoughts": "",
                            "images": [],
                            "videos": [],
                            "media": [],
                        },
                    },
                    {"X-Request-ID": "req-gemini"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                gemini_prompt="native ping",
                gemini_model="gemini-3.1-pro",
            )

        self.assertIn("gemini generate ok", results)

    def test_smoke_rejects_empty_gemini_native_output(self):
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
            if path == "/v1/gemini/generate":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "account": 1,
                        "model": "gemini",
                        "metadata": {},
                        "output": {
                            "text": "",
                            "images": [],
                            "videos": [],
                            "media": [],
                        },
                    },
                    {"X-Request-ID": "req-gemini"},
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    gemini_prompt="native ping",
                )

        self.assertIn("gemini generate output is empty", str(raised.exception))

    def test_smoke_can_check_gemini_native_stream_shape(self):
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
            if path == "/v1/gemini/generate":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "account": 1,
                        "model": "gemini",
                        "metadata": {"request_id": "req-gemini"},
                        "output": {
                            "text": "native pong",
                            "images": [],
                            "videos": [],
                            "media": [],
                        },
                    },
                    {"X-Request-ID": "req-gemini"},
                )
            if path == "/v1/gemini/stream":
                body = json.loads(request.data.decode("utf-8"))
                seen_stream = True
                self.assertEqual(body["model"], "gemini")
                self.assertEqual(body["prompt"], "native ping")
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Content-Type": "text/event-stream; charset=utf-8",
                        "X-Request-ID": "req-gemini-stream",
                    },
                    body=(
                        'data: {"type":"delta","text_delta":"na","thoughts_delta":""}\n\n'
                        'data: {"type":"final","ok":true,"account":1,"model":"gemini","metadata":{"request_id":"req-gemini-stream"},"output":{"text":"native pong","images":[],"videos":[],"media":[]}}\n\n'
                        "data: [DONE]\n\n"
                    ),
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            results = smoke_deploy.run_smoke(
                "http://service",
                "sk-test",
                gemini_prompt="native ping",
                gemini_stream=True,
            )

        self.assertTrue(seen_stream)
        self.assertIn("gemini generate ok", results)
        self.assertIn("gemini stream ok", results)

    def test_smoke_rejects_gemini_native_stream_without_final_chunk(self):
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
            if path == "/v1/gemini/generate":
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "account": 1,
                        "model": "gemini",
                        "metadata": {},
                        "output": {
                            "text": "native pong",
                            "images": [],
                            "videos": [],
                            "media": [],
                        },
                    },
                    {"X-Request-ID": "req-gemini"},
                )
            if path == "/v1/gemini/stream":
                return FakeHTTPResponse(
                    200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "X-Request-ID": "req-gemini-stream",
                    },
                    body='data: {"type":"delta","text_delta":"na"}\n\ndata: [DONE]\n\n',
                )
            raise AssertionError(path)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(AssertionError) as raised:
                smoke_deploy.run_smoke(
                    "http://service",
                    None,
                    gemini_prompt="native ping",
                    gemini_stream=True,
                )

        self.assertIn("gemini stream missing final chunk", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
