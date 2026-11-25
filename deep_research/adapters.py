"""Bridge to Hermes: model calls through ctx.llm, search/fetch through the user's own web tools.

Fetching goes through Hermes' ``web_extract``, so its URL-safety checks (private/metadata
addresses, redirects) and website policy apply unchanged; this plugin opens no sockets itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .contract import PAGE_MAX_CHARS

AUDIT_TASK = "deep_research_audit"
_FOOTER = re.compile(r"\s*\n─{8} \[TRUNCATED\] ─{8}.*\Z", re.S)
_OMITTED = "\n\n[... middle omitted — see footer ...]\n\n"


class PageUnavailable(RuntimeError):
    """The tool worked but this one page could not be read."""


def _reason(value) -> str:
    """A short, path-free version of a Hermes error message, safe to show in a report."""
    text = " ".join(str(value or "").split())
    text = re.sub(r"(?:~|[A-Za-z]:)?(?:[\\/][\w.@~-]+){2,}[\\/]?", "<path>", text)
    return text[:200]


class ToolUnavailable(RuntimeError):
    pass


def clean_extract(content: str) -> str:
    """Drop Hermes' truncation footer (it names a local cache path) and mark the omitted middle."""
    text = _FOOTER.sub("", content or "")
    return text.replace(_OMITTED, "\n[…]\n")


class HermesIO:
    def __init__(self, ctx: Any, max_output_tokens: int = 4096):
        self.ctx = ctx
        self.max_output_tokens = max_output_tokens

    def complete(self, system: str, prompt: str, *, audit: bool = False, timeout: float | None = None,
                 kind: str = "") -> dict:
        result = self.ctx.llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=self.max_output_tokens, timeout=timeout, purpose=f"deep_research {kind}".strip(),
            task=AUDIT_TASK if audit else None,
        )
        usage = getattr(result, "usage", None)
        total = getattr(usage, "total_tokens", 0) or 0
        provider, model = getattr(result, "provider", "") or "", getattr(result, "model", "") or ""
        return {"text": result.text, "tokens": total if total > 0 else None,
                "model": f"{provider}/{model}" if provider and model else (model or "unobserved")}

    def _dispatch(self, name: str, args: dict) -> dict:
        raw = self.ctx.dispatch_tool(name, args)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            raise ToolUnavailable(f"{name} returned non-JSON output") from None
        if not isinstance(data, dict):
            raise ToolUnavailable(f"{name} returned an unexpected shape")
        if data.get("error") and not data.get("results") and not data.get("data"):
            reason = _reason(data.get("error"))
            raise ToolUnavailable(f"{name} failed: {reason}" if reason else f"{name} failed")
        return data

    def search(self, query: str) -> list[dict]:
        data = self._dispatch("web_search", {"query": query, "limit": 5})
        if data.get("success") is False:
            raise ToolUnavailable("web_search failed")
        hits = (data.get("data") or {}).get("web") or []
        return [{"url": h.get("url"), "title": h.get("title") or ""} for h in hits if isinstance(h, dict)]

    def fetch(self, url: str) -> dict:
        data = self._dispatch("web_extract", {"urls": [url], "char_limit": PAGE_MAX_CHARS})
        results = data.get("results") or []
        if not results or not isinstance(results[0], dict):
            raise PageUnavailable("web_extract returned no result")
        page = results[0]
        if page.get("error"):
            raise PageUnavailable("web_extract could not read the page")
        return {"url": page.get("final_url") or page.get("url") or url, "title": page.get("title") or url,
                "text": clean_extract(page.get("content") or "")}
