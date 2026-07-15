"""Bounded JSON-only HTTP client for provider adapters."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping


class ProviderError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object):
        return None


class JsonHttpClient:
    def __init__(self, *, timeout: int = 30, max_bytes: int = 5_000_000, attempts: int = 3) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.attempts = attempts
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, body: object | None = None) -> Any:
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        safe_headers = {"Accept": "application/json", "User-Agent": "boho-analytics-platform/0.1"}
        safe_headers.update(headers or {})
        if encoded is not None:
            safe_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=safe_headers, method=method)
        last: ProviderError | None = None
        for attempt in range(self.attempts):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in {"application/json", "text/json"}:
                        raise ProviderError("invalid-response", "provider returned a non-JSON response")
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise ProviderError("response-too-large", "provider response exceeded the configured limit")
                    try:
                        return json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ProviderError("invalid-response", "provider returned invalid JSON") from exc
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                category = "authentication" if exc.code in {401, 403} else "rate-limit" if exc.code == 429 else "provider-http"
                last = ProviderError(category, f"provider request failed with HTTP {exc.code}", retryable=retryable)
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = ProviderError("network", f"provider request failed: {type(exc).__name__}", retryable=True)
            if last is None or not last.retryable or attempt + 1 >= self.attempts:
                break
            time.sleep(0.2 * (2 ** attempt))
        assert last is not None
        raise last
