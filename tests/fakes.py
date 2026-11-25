"""Network-free fakes for the research engine and job manager."""

from __future__ import annotations

import json
import threading

from deep_research.engine import UNTRUSTED_MARKER

PAGE_A = (
    "Widget sales report. In 2025 the company sold 1,200 widgets in total across all regions. "
    "Sales in 2024 were 1,000 widgets in total across all regions."
)
PAGE_B = "Independent review: widget customers reported high satisfaction in the 2025 survey of 400 buyers."


class FakeIO:
    """Scripted model + search + fetch. Model replies are chosen by prompt kind."""

    def __init__(self, pages=None, hits=None, audit_verdict="supported", extraction=None, plan=None,
                 model_error=None, fetch_gate=None):
        self.pages = pages if pages is not None else {
            "https://a.example/report": ("Report A", PAGE_A),
            "https://b.example/review": ("Review B", PAGE_B),
        }
        self.hits = hits if hits is not None else [
            {"url": "https://a.example/report", "title": "Report A"},
            {"url": "https://b.example/review", "title": "Review B"},
        ]
        self.audit_verdict = audit_verdict
        self.extraction = extraction
        self.plan = plan or {"questions": ["widget sales 2025"]}
        self.model_error = model_error
        self.fetch_gate = fetch_gate  # threading.Event to block fetches
        self.calls = {"model": 0, "audit": 0, "search": 0, "fetch": 0}
        self.prompts = []
        self._lock = threading.Lock()

    def _count(self, key):
        with self._lock:
            self.calls[key] += 1

    def complete(self, system, prompt, *, audit=False, timeout=None, kind=""):
        self._count("audit" if audit else "model")
        self.prompts.append((kind, prompt))
        if self.model_error:
            raise self.model_error
        if kind == "plan":
            body = self.plan
        elif kind == "extract":
            body = self.extraction if self.extraction is not None else self._default_extraction(prompt)
        else:
            data = json.loads(prompt.split(UNTRUSTED_MARKER, 1)[1])
            body = {"verdicts": [{"index": i, "verdict": self.audit_verdict, "reason": "ok"}
                                 for i in range(len(data["findings"]))], "unanswered": []}
        return {"text": "```json\n" + json.dumps(body) + "\n```", "tokens": 100, "model": "fake/model"}

    @staticmethod
    def _default_extraction(prompt):
        data = json.loads(prompt.split(UNTRUSTED_MARKER, 1)[1])
        sid = next((s["id"] for s in data["sources"] if s["title"] == "Report A"), "S1")
        q1 = "In 2025 the company sold 1,200 widgets in total"
        q0 = "Sales in 2024 were 1,000 widgets in total"
        return {
            "findings": [
                {"statement": "The company sold 1,200 widgets in 2025.",
                 "evidence": [{"sourceId": sid, "quote": q1}]},
                {"statement": "Sales were made up.", "evidence": [{"sourceId": sid, "quote": "this text is not in any source at all"}]},
                {"statement": "2025 widget sales were 200 higher than 2024.",
                 "evidence": [{"sourceId": sid, "quote": q1}, {"sourceId": sid, "quote": q0}],
                 "calculation": {"operation": "difference", "value": 200, "unit": "widgets", "asOf": "2025",
                                 "inputs": [
                                     {"value": 1200, "unit": "widgets", "asOf": "2025", "sourceId": sid, "quote": q1},
                                     {"value": 1000, "unit": "widgets", "asOf": "2025", "sourceId": sid, "quote": q0},
                                 ]}},
            ],
            "gaps": [],
            "queries": [],
        }

    def search(self, query):
        self._count("search")
        return list(self.hits)

    def fetch(self, url):
        self._count("fetch")
        if self.fetch_gate is not None:
            self.fetch_gate.wait(5)
        if url not in self.pages:
            raise RuntimeError("404")
        title, text = self.pages[url]
        return {"url": url, "title": title, "text": text}


class MemorySteps:
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return self.rows.get(key)

    def put(self, key, state, value=None):
        self.rows[key] = {"state": state, "value": value}
