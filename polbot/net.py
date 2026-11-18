"""Tiny stdlib HTTP helper with retries.

polbot's read paths (Gamma + public CLOB endpoints) need no authentication,
so we avoid heavyweight dependencies and talk plain HTTPS with urllib.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger("polbot.net")

_UA = "polbot/0.1 (+https://github.com/analyshd-crypto/polbot)"


class HttpError(Exception):
    """HTTP error surfaced after retries (or non-retryable 4xx)."""

    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")


class NotFound(HttpError):
    """HTTP 404."""


class HttpClient:
    """GET JSON with timeout, retry and exponential backoff."""

    def __init__(
        self,
        timeout: float = 15.0,
        retry_attempts: int = 3,
        retry_backoff_s: float = 0.75,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_s = retry_backoff_s
        self._sleep = sleep

    def get_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean)}"
        last_exc: Optional[BaseException] = None
        for attempt in range(self.retry_attempts):
            try:
                return self._get_once(url)
            except NotFound:
                raise
            except HttpError as exc:  # 429 / 5xx -> retry
                last_exc = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
            if attempt < self.retry_attempts - 1:
                delay = self.retry_backoff_s * (2**attempt)
                log.debug("retry %s in %.2fs (%s)", url, delay, last_exc)
                self._sleep(delay)
        raise last_exc  # type: ignore[misc]

    def get_json_or_none(self, url: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Like :meth:`get_json` but returns ``None`` on 404."""
        try:
            return self.get_json(url, params)
        except NotFound:
            return None

    # ------------------------------------------------------------------ --
    def _get_once(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 404:
                raise NotFound(404, url, body)
            raise HttpError(exc.code, url, body)
        if not raw:
            return None
        return json.loads(raw)
