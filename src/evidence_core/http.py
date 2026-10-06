"""带缓存、重试、限流的 HTTP 客户端（对应 Q53 重试/限流/缓存/幂等）。

所有数据源统一走这里，好处有三：
- 缓存让重跑评测不重复打外部 API，也让一次运行可复现；
- 限流集中管理，不用每个源各写一套 sleep；
- 失败语义统一（SourceUnavailable），Agent 才能一致地处理 fallback。

数据源与 LLM 共用 request_json；运行内 deadline 同时约束超时、限流与退避。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DEFAULT_TIMEOUT = 25
DEFAULT_RETRIES = 3

_deadline: ContextVar[float | None] = ContextVar("request_deadline", default=None)


class DeadlineExceeded(RuntimeError):
    """运行时间耗尽；不是数据源故障，不允许被降级为成功或 fixture。"""


@contextmanager
def request_deadline(deadline: float):
    previous = _deadline.get()
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def check_deadline() -> float | None:
    deadline = _deadline.get()
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise DeadlineExceeded("运行时间预算已耗尽")
    return remaining


def _bounded_sleep(seconds: float) -> None:
    remaining = check_deadline()
    time.sleep(seconds if remaining is None else min(seconds, remaining))
    check_deadline()


class SourceUnavailable(RuntimeError):
    """数据源不可用：未配置、网络失败、限流、或返回无法解析。

    Agent 捕获它之后走 fallback 链，而不是让整次运行崩掉。
    """

    def __init__(self, source_id: str, reason: str) -> None:
        super().__init__(f"[{source_id}] {reason}")
        self.source_id = source_id
        self.reason = reason


class RateLimiter:
    """最简单的按源间隔限流。"""

    def __init__(self, min_interval_seconds: float = 0.34) -> None:
        self.min_interval = min_interval_seconds
        self._last: dict[str, float] = {}

    def wait(self, key: str) -> None:
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None:
            delta = now - last
            if delta < self.min_interval:
                _bounded_sleep(self.min_interval - delta)
        self._last[key] = time.monotonic()


_limiter = RateLimiter()


def _cache_path(url: str, body: bytes | None) -> Path:
    raw = url + (body.decode("utf-8", "ignore") if body else "")
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _read_cache(path: Path, ttl_seconds: int) -> Any | None:
    if not path.exists():
        return None
    if ttl_seconds > 0 and (time.time() - path.stat().st_mtime) > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def request_json(
    url: str,
    *,
    source_id: str = "http",
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    use_cache: bool = True,
    cache_ttl: int = 7 * 24 * 3600,
) -> Any:
    """发一个 JSON 请求。命中缓存直接返回；全部重试失败抛 SourceUnavailable。"""
    check_deadline()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    cache_file = _cache_path(url, payload)

    if use_cache:
        cached = _read_cache(cache_file, cache_ttl)
        if cached is not None:
            return cached

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            _limiter.wait(source_id)
            req = urllib.request.Request(url, data=payload, method=method)
            req.add_header("Accept", "application/json")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            remaining = check_deadline()
            request_timeout = timeout if remaining is None else min(timeout, remaining)
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            check_deadline()
            if use_cache:
                _write_cache(cache_file, data)
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError) as exc:
            last_error = exc
            check_deadline()
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise SourceUnavailable(source_id, f"HTTP {exc.code} 不可重试：{exc.reason}") from exc
            if attempt + 1 < retries:
                _bounded_sleep(1.5 * (attempt + 1))   # 退避也受运行预算约束

    raise SourceUnavailable(source_id, f"{retries} 次重试后仍失败：{last_error}")


def build_url(base: str, path: str, params: dict[str, Any] | None = None) -> str:
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url
