"""Read-only transport comparison for Polza's documented GET /models endpoint."""

from __future__ import annotations

import asyncio
import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any

import httpx

from app.config import Settings


def _result(name: str, started: float, **details: Any) -> dict[str, Any]:
    return {"transport": name, "elapsed_ms": round((time.perf_counter() - started) * 1000), **details}


async def _httpx_probe(settings: Settings, trust_env: bool) -> dict[str, Any]:
    started = time.perf_counter()
    endpoint = f"{settings.polza_base_url}/models"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), trust_env=trust_env, follow_redirects=False) as client:
            response = await client.get(endpoint, params={"type": "chat"}, headers={"Authorization": f"Bearer {settings.polza_api_key}"})
        return _result(
            f"httpx trust_env={trust_env}", started, status_code=response.status_code,
            http_version=response.http_version, content_type=response.headers.get("content-type"),
            redirect_location=response.headers.get("location"),
        )
    except httpx.TimeoutException:
        return _result(f"httpx trust_env={trust_env}", started, error_kind="timeout")
    except httpx.ConnectError:
        return _result(f"httpx trust_env={trust_env}", started, error_kind="connection_error")
    except httpx.RequestError as exc:
        return _result(f"httpx trust_env={trust_env}", started, error_kind="request_error", exception=type(exc).__name__)


def _urllib_probe(settings: Settings) -> dict[str, Any]:
    """stdlib urllib uses the normal Python/system proxy discovery and validates TLS by default."""
    started = time.perf_counter()
    request = urllib.request.Request(
        f"{settings.polza_base_url}/models?type=chat",
        headers={"Authorization": f"Bearer {settings.polza_api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:  # noqa: S310 -- fixed documented HTTPS endpoint
            return _result(
                "urllib", started, status_code=response.status, http_version="unknown",
                content_type=response.headers.get("content-type"), redirect_location=response.headers.get("location"),
            )
    except urllib.error.HTTPError as exc:
        return _result("urllib", started, status_code=exc.code, content_type=exc.headers.get("content-type"),
                       redirect_location=exc.headers.get("location"))
    except TimeoutError:
        return _result("urllib", started, error_kind="timeout")
    except OSError as exc:
        return _result("urllib", started, error_kind="os_error", exception=type(exc).__name__)


async def main(transport: str) -> None:
    settings = Settings.from_env()
    if settings.ai_provider != "polza" or not settings.polza_api_key:
        raise ValueError("This diagnostic requires AI_PROVIDER=polza and a configured POLZA_API_KEY")
    if transport == "httpx-env":
        results = [await _httpx_probe(settings, trust_env=True)]
    elif transport == "httpx-no-env":
        results = [await _httpx_probe(settings, trust_env=False)]
    else:
        results = [await asyncio.to_thread(_urllib_probe, settings)]
    print(json.dumps({"endpoint": f"{settings.polza_base_url}/models", "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one authenticated, read-only Polza GET /models transport probe.")
    parser.add_argument("--transport", choices=("httpx-env", "httpx-no-env", "urllib"), required=True)
    args = parser.parse_args()
    asyncio.run(main(args.transport))
