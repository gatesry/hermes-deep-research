"""Tool contract: argument validation, request defaults, budgets and domain filters.

Pure standard library so the core runs (and is tested) without Hermes installed.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

DEFAULT_BUDGET = {"modelCalls": 8, "searches": 6, "fetches": 12, "parallelism": 2, "durationMs": 180_000}
MAX_BUDGET = {"modelCalls": 30, "searches": 20, "fetches": 40, "parallelism": 4, "durationMs": 900_000}
# Named presets so callers pick an effort level instead of tuning five counters; explicit budget keys win.
DEPTH_PRESETS = {
    "quick": {"modelCalls": 4, "searches": 3, "fetches": 6, "parallelism": 2, "durationMs": 90_000},
    "standard": dict(DEFAULT_BUDGET),
    "deep": {"modelCalls": 20, "searches": 14, "fetches": 30, "parallelism": 4, "durationMs": 600_000},
}
CORPUS_MAX_BYTES = 60_000
PAGE_MAX_CHARS = 12_000
ACTIONS = ("start", "status", "result", "resume", "cancel", "capabilities")
COUNTERS = ("modelCalls", "searches", "fetches")


class ContractError(ValueError):
    """The caller's arguments do not satisfy the tool contract."""


class BudgetExhausted(RuntimeError):
    pass


class DeadlineExceeded(RuntimeError):
    pass


def _text(value: Any, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")
    if len(value) > max_len:
        raise ContractError(f"{name} exceeds {max_len} characters")
    return value


def _only(obj: Any, name: str, allowed: set[str]) -> dict:
    if not isinstance(obj, dict):
        raise ContractError(f"{name} must be an object")
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ContractError(f"{name} has unknown field(s): {', '.join(extra)}")
    return obj


def _text_list(value: Any, name: str, max_items: int, max_len: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise ContractError(f"{name} must be a list of at most {max_items} strings")
    return [_text(v, name, max_len) for v in value]


_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalize_host(value: str) -> str | None:
    """Canonical ASCII host: IDNA-encoded, lower-case, no surrounding dots. None if not a clean DNS name.

    Both the allow/deny lists and every fetched URL go through this, so case, Unicode/punycode
    spellings, alternative label separators and a trailing dot cannot dodge a filter."""
    try:
        text = value.strip().strip(".")
        if not text or "%" in text:
            return None
        ascii_host = text.encode("idna").decode("ascii").lower().strip(".")
        labels = ascii_host.split(".")
        if not all(_LABEL.match(label) for label in labels):
            return None
        for label in labels:  # a punycode label must decode, or it is not a real IDN
            if label.startswith("xn--"):
                label.encode("ascii").decode("idna")
        return ascii_host
    except (UnicodeError, ValueError):
        return None


def _domain(value: str) -> str:
    d = value.strip()
    if d.startswith("*."):
        d = d[2:]
    host = None if any(c in d for c in "/:@ ") else normalize_host(d)
    if host is None:
        raise ContractError(f"invalid domain: {value!r}")
    return host


def normalize_request(raw: Any) -> dict:
    req = _only(raw, "request", {"question", "engine", "output", "asOf", "sources", "depth", "budget"})
    engine = req.get("engine", "portable")
    if engine != "portable":
        raise ContractError("Only the portable engine is available in this plugin; omit engine or use 'portable'.")
    out: dict[str, Any] = {"question": _text(req.get("question"), "question", 8_000), "engine": "portable"}
    if req.get("output") is not None:
        out["output"] = _text(req["output"], "output", 2_000)
    if req.get("asOf") is not None:
        out["asOf"] = _text(req["asOf"], "asOf", 64)

    src = _only(req.get("sources") or {}, "sources", {"web", "urls", "documents", "allowDomains", "denyDomains"})
    documents = src.get("documents") or []
    if not isinstance(documents, list) or len(documents) > 10:
        raise ContractError("sources.documents must be a list of at most 10 documents")
    docs = []
    for d in documents:
        _only(d, "document", {"title", "text"})
        docs.append({"title": _text(d.get("title"), "document title", 256),
                     "text": _text(d.get("text"), "document text", 20_000)})
    web = src.get("web")
    if web is not None and not isinstance(web, bool):
        raise ContractError("sources.web must be a boolean")
    out["sources"] = {
        "web": (not docs) if web is None else web,
        "urls": _text_list(src.get("urls"), "sources.urls", 20, 2_048),
        "documents": docs,
        "allowDomains": [_domain(d) for d in _text_list(src.get("allowDomains"), "sources.allowDomains", 20, 253)],
        "denyDomains": [_domain(d) for d in _text_list(src.get("denyDomains"), "sources.denyDomains", 20, 253)],
    }

    depth = req.get("depth", "standard")
    if depth not in DEPTH_PRESETS:
        raise ContractError(f"depth must be one of: {', '.join(DEPTH_PRESETS)}")
    out["depth"] = depth
    budget = dict(DEPTH_PRESETS[depth])
    for key, value in _only(req.get("budget") or {}, "budget", set(DEFAULT_BUDGET)).items():
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_BUDGET[key]:
            raise ContractError(f"budget.{key} must be an integer from 1 to {MAX_BUDGET[key]}")
        budget[key] = value
    out["budget"] = budget
    return out


def canonical_request(request: dict) -> str:
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_args(args: Any) -> dict:
    if not isinstance(args, dict):
        raise ContractError("arguments must be an object")
    action = args.get("action")
    if action not in ACTIONS:
        raise ContractError(f"action must be one of: {', '.join(ACTIONS)}")
    if action == "capabilities":
        _only(args, "arguments", {"action"})
        return {"action": action}
    if action == "start":
        _only(args, "arguments", {"action", "idempotencyKey", "request"})
        return {"action": action, "idempotencyKey": _text(args.get("idempotencyKey"), "idempotencyKey", 128),
                "request": normalize_request(args.get("request"))}
    _only(args, "arguments", {"action", "jobId"})
    return {"action": action, "jobId": _text(args.get("jobId"), "jobId", 64)}


def host_of(url: str) -> str | None:
    """Normalized host of an http(s) URL without userinfo, else None. IP literals are kept as-is."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or "@" in parts.netloc:
            return None
        raw = parts.hostname or ""
        try:
            return ipaddress.ip_address(raw).compressed
        except ValueError:
            return normalize_host(raw)
    except ValueError:
        return None


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def domain_allowed(url: str, allow: list[str], deny: list[str]) -> bool:
    host = host_of(url)
    if host is None or any(_matches(host, d) for d in deny):
        return False
    return not allow or any(_matches(host, d) for d in allow)


class Budget:
    """Shared, thread-safe call admission plus one global deadline."""

    def __init__(self, limits: dict, deadline_at: float, clock=time.time):
        self.limits: dict[str, int] = {k: int(limits[k]) for k in COUNTERS}
        self.used: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        self.deadline_at = deadline_at
        self.clock = clock
        self._lock = threading.Lock()

    def restore(self, used: dict) -> None:
        with self._lock:
            for k in COUNTERS:
                self.used[k] = int(used.get(k, 0))

    def time_left(self) -> float:
        return self.deadline_at - self.clock()

    def remaining(self, kind: str) -> int:
        with self._lock:
            return self.limits[kind] - self.used[kind]

    def charge(self, kind: str) -> None:
        """Count a replayed receipt: it was paid for before, so it still uses budget."""
        with self._lock:
            self.used[kind] += 1

    def admit(self, kind: str) -> None:
        with self._lock:
            if self.clock() >= self.deadline_at:
                raise DeadlineExceeded("research deadline reached")
            if self.used[kind] >= self.limits[kind]:
                raise BudgetExhausted(f"{kind} budget exhausted")
            self.used[kind] += 1
