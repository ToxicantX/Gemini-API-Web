from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _request(
    base_url: str,
    path: str,
    *,
    api_key: str | None = None,
    method: str = "GET",
) -> tuple[int, dict, dict[str, str]]:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        response_headers = dict(exc.headers.items())
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        data = {}
    return status, data, response_headers


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_smoke(base_url: str, api_key: str | None) -> list[str]:
    results: list[str] = []

    health_status, health, _ = _request(base_url, "/health")
    _require(health_status == 200, f"/health returned {health_status}")
    _require(health.get("ok") is True, "/health did not return ok=true")
    _require("models" in health, "/health missing models")
    results.append("health ok")

    unauth_status, unauth, unauth_headers = _request(base_url, "/v1/models")
    if health.get("auth", {}).get("api_key_required"):
        _require(unauth_status == 401, "/v1/models should require an API key")
        _require("x-request-id" in {key.lower(): value for key, value in unauth_headers.items()}, "401 response missing X-Request-ID")
        _require("error" in unauth, "401 response missing OpenAI error body")
        results.append("api key protection ok")
    else:
        _require(unauth_status == 200, f"/v1/models returned {unauth_status}")
        results.append("open local api ok")

    if api_key:
        models_status, models, headers = _request(base_url, "/v1/models", api_key=api_key)
        _require(models_status == 200, f"/v1/models with API key returned {models_status}")
        _require(models.get("object") == "list", "/v1/models did not return an OpenAI list")
        _require(any(item.get("id") == "gemini" for item in models.get("data", [])), "/v1/models missing gemini")
        _require("x-request-id" in {key.lower(): value for key, value in headers.items()}, "authorized response missing X-Request-ID")
        results.append("authorized models ok")

    media_status, media, _ = _request(base_url, "/v1/media-cooldowns", api_key=api_key)
    if health.get("auth", {}).get("api_key_required") and not api_key:
        _require(media_status == 401, "/v1/media-cooldowns should require an API key")
        results.append("media cooldown protection ok")
    else:
        _require(media_status == 200, f"/v1/media-cooldowns returned {media_status}")
        _require(media.get("ok") is True, "/v1/media-cooldowns missing ok=true")
        results.append("media cooldown summary ok")

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deployed Gemini API Web service.")
    parser.add_argument("--base-url", default="http://localhost:7860")
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    try:
        results = run_smoke(args.base_url, args.api_key or None)
    except Exception as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    for item in results:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
