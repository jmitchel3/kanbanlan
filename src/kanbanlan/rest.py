"""Conditional REST reads that cost nothing while GitHub's answer is unchanged.

GitHub does not count a REST request answered ``304 Not Modified`` against
the primary rate limit, and GraphQL has no equivalent. Every page read here
sends the ETag of the copy it holds, so a stable queue is revalidated for
free. The cache is only ever a copy to revalidate, never an answer on its
own: every read still asks GitHub, so a lost or corrupt cache can cost
requests but can never produce a stale result.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from kanbanlan.runner import CommandError, CommandResult, RateLimitError, is_rate_limit_failure

REST_CACHE_FILENAME = "rest_cache.json"
REST_CACHE_SCHEMA_VERSION = 1

# Pages already known from the previous read are revalidated side by side.
# Each is one gh process, so this bounds processes, not quota.
PAGE_CONCURRENCY = 6

# Guards against a Link chain that never ends.
MAX_PAGES = 500

# GitHub's page size when a path does not ask for one.
DEFAULT_PER_PAGE = 30


@dataclass
class RestStats:
    """What one batch of reads cost, for the snapshot's rate_limit block."""

    requests: int = 0
    not_modified: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    def rate_limit(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "resource": "core",
            "requests": self.requests,
            "not_modified": self.not_modified,
            # Only GraphQL documents spend GraphQL points.
            "cost": 0,
        }
        for name, key in (
            ("x-ratelimit-remaining", "remaining"),
            ("x-ratelimit-limit", "limit"),
            ("x-ratelimit-used", "used"),
        ):
            try:
                value[key] = int(self.headers[name])
            except (KeyError, ValueError):
                pass
        reset = _reset_at(self.headers)
        if reset:
            value["resetAt"] = reset
        return value


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: Any


class ConditionalReader:
    """Read paginated REST collections with persisted per-page ETags."""

    def __init__(self, runner: Any, path: Path | None, *, hostname: str = "github.com"):
        self.runner = runner
        self.path = path
        self.hostname = hostname
        self._lock = threading.Lock()
        self._entries: dict[str, Any] = self._load()
        self._dirty = False
        self.stats = RestStats()

    def collection(self, first: str) -> list[Any]:
        """Return every item of a paginated collection, in page order.

        Pages the previous read followed are revalidated concurrently; the
        chain is then checked link by link against what GitHub answered, and
        anything past a changed link is read in order, so pages that moved
        never contribute to the result.
        """

        known = self._known_chain(first)
        with ThreadPoolExecutor(max_workers=PAGE_CONCURRENCY) as executor:
            responses = list(executor.map(self.page, known))
        items: list[Any] = []
        position = 0
        current: str | None = first
        while current is not None:
            if position >= MAX_PAGES:
                raise RuntimeError(f"GitHub returned more than {MAX_PAGES} pages for {first}")
            if position < len(known) and known[position] == current:
                response = responses[position]
            else:
                response = self.page(current)
            if not isinstance(response.body, list):
                raise RuntimeError(f"GitHub returned an unexpected page for {current}")
            items.extend(response.body)
            current = _next_link(response.headers)
            position += 1
        return items

    def page(self, path: str) -> Response:
        """Read one page, sending the cached ETag when there is one.

        A ``304`` carries no Link header, and GitHub's ETag covers the body
        only, so a full last page that gains a successor still answers
        ``304``. Its cached Link would then end the chain before the new
        page, dropping whatever was created there; such a page is always
        read in full instead.
        """

        with self._lock:
            entry = self._entries.get(path)
        args = ["gh", "api", "--include"]
        if self.hostname and self.hostname != "github.com":
            args.extend(["--hostname", self.hostname])
        etag = entry.get("etag") if isinstance(entry, dict) else None
        if etag and _next_link(entry.get("headers") or {}) is None:
            if _full_page(path, entry.get("body")):
                etag = None
        if etag:
            args.extend(["-H", f"If-None-Match: {etag}"])
        args.append(path)
        result = self.runner.run(args, check=False, retry=True)
        status, headers, body_text = _parse_included(result.stdout)
        with self._lock:
            self.stats.requests += 1
            if headers:
                self.stats.headers = headers
        if status == 304 and isinstance(entry, dict) and "body" in entry:
            with self._lock:
                self.stats.not_modified += 1
            return Response(304, {**entry.get("headers", {}), **headers}, entry["body"])
        if status == 200 and result.returncode == 0:
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"GitHub returned invalid JSON for {path}") from exc
            kept = {name: headers[name] for name in ("etag", "link") if name in headers}
            with self._lock:
                if "etag" in headers:
                    self._entries[path] = {
                        "etag": headers["etag"],
                        "headers": kept,
                        "body": body,
                        "fetched_at": _now(),
                    }
                else:
                    self._entries.pop(path, None)
                self._dirty = True
            return Response(200, headers, body)
        if is_rate_limit_failure(result) or status in (429,) or _exhausted(status, headers):
            raise RateLimitError(_failure_detail(result, path), reset_at=_reset_at(headers))
        if status == 304:
            # A 304 for a copy this process does not hold (another session
            # rewrote the cache): forget the ETag and ask for the page itself.
            with self._lock:
                self._entries.pop(path, None)
            return self.page(path)
        raise CommandError(result)

    def save(self) -> None:
        """Persist the ETags and bodies read; a failed write only costs requests."""

        if self.path is None or not self._dirty:
            return
        with self._lock:
            payload = {"schema_version": REST_CACHE_SCHEMA_VERSION, "entries": self._entries}
        from kanbanlan.github import _write_json_atomic

        try:
            _write_json_atomic(self.path, payload)
        except OSError:
            return
        self._dirty = False

    def _known_chain(self, first: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = first
        with self._lock:
            while current and current not in seen and len(chain) < MAX_PAGES:
                chain.append(current)
                seen.add(current)
                entry = self._entries.get(current)
                if not isinstance(entry, dict):
                    break
                current = _next_link(entry.get("headers") or {})
        return chain

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict) or data.get("schema_version") != REST_CACHE_SCHEMA_VERSION:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}


def _parse_included(stdout: str) -> tuple[int | None, dict[str, str], str]:
    """Split ``gh api --include`` output into status, headers, and body."""

    text = stdout.replace("\r\n", "\n")
    head, separator, body = text.partition("\n\n")
    lines = head.split("\n")
    if not separator or not lines or not lines[0].startswith("HTTP/"):
        return None, {}, stdout
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None, {}, stdout
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


def _next_link(headers: dict[str, str]) -> str | None:
    for part in (headers.get("link") or "").split(","):
        target, _, params = part.partition(";")
        if 'rel="next"' in params:
            return api_path(target.strip().strip("<>"))
    return None


def _full_page(path: str, body: Any) -> bool:
    """Report whether a page holds as many items as the path asks for."""

    if not isinstance(body, list) or not body:
        return False
    values = parse_qs(urlsplit(path).query).get("per_page")
    try:
        per_page = int(values[-1]) if values else DEFAULT_PER_PAGE
    except ValueError:
        return False
    return len(body) >= per_page


def api_path(url: str) -> str:
    """Turn an absolute API URL from a Link header into a ``gh api`` path."""

    parts = urlsplit(url)
    path = parts.path.lstrip("/")
    path = path.removeprefix("api/v3/")
    return f"{path}?{parts.query}" if parts.query else path


def _exhausted(status: int | None, headers: dict[str, str]) -> bool:
    return status == 403 and headers.get("x-ratelimit-remaining") == "0"


def _reset_at(headers: dict[str, str]) -> str | None:
    try:
        epoch = int(headers["x-ratelimit-reset"])
    except (KeyError, ValueError):
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _failure_detail(result: CommandResult, path: str) -> str:
    detail = result.stderr.strip() or f"exit {result.returncode}"
    return f"gh api {path}: {detail}"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
