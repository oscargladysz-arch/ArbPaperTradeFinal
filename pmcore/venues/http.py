"""Shared HTTP client: TLS always verified (rule 7), retries with backoff, gentle rate limit.

`ca_bundle` lets a corporate or cloud proxy's CA be trusted. It never disables verification.
"""

from __future__ import annotations

import json
import logging
import random
import time
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class RateLimiter:
    """Simple token-interval limiter: at most `rps` requests per second."""

    def __init__(self, rps: float) -> None:
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        delta = now - self._last
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last = time.monotonic()


class JsonClient:
    def __init__(
        self,
        base_url: str,
        *,
        rps: float = 5.0,
        max_retries: int = 6,
        timeout_s: float = 30.0,
        ca_bundle: str | None = None,
        user_agent: str = "ref-mm/0.0.1 (research; read-only)",
        headers: dict[str, str] | None = None,
    ) -> None:
        verify: bool | str = ca_bundle if ca_bundle else True
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            verify=verify,
            headers={"User-Agent": user_agent, "Accept": "application/json", **(headers or {})},
            follow_redirects=False,
        )
        self._limiter = RateLimiter(rps)
        self._max_retries = max_retries
        self.base_url = base_url

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JsonClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            self._limiter.wait()
            try:
                resp = self._client.get(path, params=clean)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise
                self._sleep(attempt, f"transport error {exc!r}")
                continue
            if resp.status_code in RETRYABLE_STATUS:
                attempt += 1
                if attempt > self._max_retries:
                    raise HttpError(resp.status_code, str(resp.url), resp.text)
                retry_after = resp.headers.get("Retry-After")
                extra = float(retry_after) if retry_after and retry_after.isdigit() else 0.0
                self._sleep(attempt, f"status {resp.status_code}", extra)
                continue
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, str(resp.url), resp.text)
            try:
                # Floats become Decimal (rule 5): Polymarket serves prices and sizes as JSON numbers.
                return json.loads(resp.text, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                raise HttpError(
                    resp.status_code, str(resp.url), f"non-JSON body: {resp.text[:200]}"
                ) from exc

    def _sleep(self, attempt: int, why: str, extra: float = 0.0) -> None:
        delay = min(60.0, (2.0 ** (attempt - 1)) + random.uniform(0, 0.5) + extra)
        log.warning("retry %d after %s; sleeping %.1fs", attempt, why, delay)
        time.sleep(delay)
