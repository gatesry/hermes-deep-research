"""Mechanical evidence checks and the report renderer.

Provenance here is independent of the model's support verdict: a finding survives
only when every quote occurs in the retained source text and any derived number
recomputes from its cited inputs. Behaviour follows OpenClaw's portable Deep
Research engine (MIT); see NOTICE.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

MIN_QUOTE_CHARS = 20
OPERATIONS = ("sum", "difference", "ratio", "percent-change")

_WS = re.compile(r"\s+")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"(?<=\d)[,\u00a0\u202f'](?=\d{3}(?!\d))")


def collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    """Exact substring, or exact after collapsing whitespace runs on both sides."""
    if not quote:
        return False
    return quote in text or collapse(quote) in collapse(text)


def _numbers_in(text: str) -> list[float]:
    return [float(tok) for tok in _NUMBER.findall(_THOUSANDS.sub("", text))]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _decimals(value: float) -> int:
    """Decimal places as printed, e.g. 0.33 -> 2, 1.5e-05 -> 6, 2e+20 -> 0."""
    exponent = Decimal(repr(float(value))).normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _matches(value: float, expected: float) -> bool:
    if abs(value - expected) <= 1e-9 * max(1.0, abs(expected)):
        return True
    # Accept the rounding the model printed (half a unit in its last decimal, max 6).
    places = _decimals(value)
    if places > 6 or abs(value) >= 1e15:  # beyond printed-rounding territory; demand the exact match above
        return False
    return abs(value - expected) <= 0.5 * 10 ** (-places) + 1e-12


def calculation_problem(calc: dict, evidence: list[dict]) -> str | None:
    inputs = calc.get("inputs")
    op = calc.get("operation")
    if not isinstance(op, str) or op not in OPERATIONS:
        return "Unsupported calculation operation."
    if not isinstance(inputs, list) or len(inputs) != 2 or not all(isinstance(i, dict) for i in inputs):
        return "A calculation requires two finite inputs."
    if not all(_is_number(i.get("value")) for i in inputs) or not _is_number(calc.get("value")):
        return "A calculation requires two finite inputs."
    for item in inputs:
        quote, unit, as_of = (str(item.get(k) or "") for k in ("quote", "unit", "asOf"))
        cited = any(e.get("sourceId") == item.get("sourceId") and e.get("quote") == quote for e in evidence)
        if (
            not cited
            or not unit
            or not as_of
            or not any(n == float(item["value"]) for n in _numbers_in(quote))
            or unit.lower() not in quote.lower()
            or as_of not in quote
        ):
            return "Calculation input is missing a cited value, unit, or date."
    left, right = inputs
    if left["unit"] != right["unit"] or left["asOf"] != right["asOf"] or calc.get("asOf") != left["asOf"]:
        return "Calculation inputs use different units or dates."
    a, b = float(left["value"]), float(right["value"])
    try:
        expected = {
            "sum": lambda: a + b,
            "difference": lambda: a - b,
            "ratio": lambda: a / b,
            "percent-change": lambda: (b - a) / a * 100,
        }[op]()
    except ZeroDivisionError:
        return "Calculation result or unit does not match the stated derivation."
    expected_unit = {"ratio": "ratio", "percent-change": "%"}.get(op, left["unit"])
    if calc.get("unit") != expected_unit or not math.isfinite(expected) or not _matches(float(calc["value"]), expected):
        return "Calculation result or unit does not match the stated derivation."
    return None


def evidence_problem(finding: dict, sources: list[dict]) -> str | None:
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "A finding needs at least one quoted source."
    by_id = {s["id"]: s for s in sources}
    for item in evidence:
        if not isinstance(item, dict):
            return "Quote does not occur in the recorded source."
        quote, source_id = item.get("quote"), item.get("sourceId")
        if not isinstance(quote, str) or not isinstance(source_id, str):
            return "Quote does not occur in the recorded source."
        if len(_WS.sub("", quote)) < MIN_QUOTE_CHARS:
            return f"Quote is shorter than {MIN_QUOTE_CHARS} non-whitespace characters."
        source = by_id.get(source_id)
        if source is None or not quote_in_text(quote, source["text"]):
            return "Quote does not occur in the recorded source."
    calc = finding.get("calculation")
    if calc is None:
        return None
    if not isinstance(calc, dict):
        return "A calculation requires two finite inputs."
    return calculation_problem(calc, evidence)


def finding_key(statement: str) -> str:
    return collapse(statement)


def plain(value: Any, limit: int = 1000) -> str:
    return collapse(str(value))[:limit]


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>|#!~])")


def autolink(url: str) -> str:
    """A CommonMark autolink: no escapes inside, so percent-encode what could end or break it."""
    safe = re.sub(r"[\x00-\x20<>\x7f]", lambda m: "%{:02X}".format(ord(m.group())), str(url)[:2048])
    return f"<{safe}>"


def md(value: Any, limit: int = 1000) -> str:
    """Escape model/source text: only the renderer may emit citations or structure."""
    text = plain(value, limit).replace("&", "&amp;")
    text = re.sub(r"\[([SP]\d+)\]", r"(\1)", text)
    return _MD_SPECIAL.sub(r"\\\1", text)


def _fmt(value: float) -> str:
    return f"{value:g}" if abs(value) < 1e15 else repr(value)


VERIFIED_NOTE = ("_Verified here means each quote appears in its fetched source and a separate model audit "
                 "judged it supportive. That is not proof that a claim is true, or that the research is complete._")


def render_report(question: str, findings: list[dict], sources: list[dict], gaps: list[str],
                  disagreements: list[str] | None = None) -> dict:
    parts: list[str] = [f"# {md(question, 8000)}\n\n## Findings"]
    citations: list[dict] = []
    # Last line of defence: a citation must name a source that was actually retained.
    known = {s["id"] for s in sources}
    kept = [f for f in findings if f.get("evidence") and all(e.get("sourceId") in known for e in f["evidence"])]
    gaps = list(gaps) + [f"Citation outside the retained sources omitted: {f['statement']}"
                         for f in findings if f not in kept]
    findings = kept

    def size() -> int:
        return len("".join(parts).encode("utf-8"))

    if not findings:
        parts.append("\n- No source-backed findings.")
    for f in findings:
        parts.append(f"\n- {md(f['statement'])}")
        calc = f.get("calculation")
        if calc:
            left, right = calc["inputs"]
            line = (
                f"checked: {calc['operation']} of {_fmt(left['value'])} {left['unit']} and "
                f"{_fmt(right['value'])} {right['unit']} = {_fmt(calc['value'])} {calc['unit']} ({calc['asOf']})"
            )
            parts.append(f" ({md(line, 400)})")
        for e in f["evidence"]:
            parts.append(" ")
            start = size()
            parts.append(f"[{e['sourceId']}]")
            citations.append({"sourceId": e["sourceId"], "startByte": start, "endByte": size()})
    parts.append("\n\n## Disagreements\n")
    parts.append("\n".join(f"- {md(d)}" for d in disagreements) if disagreements else "- None found.")
    parts.append("\n\n## Unresolved\n")
    parts.append("\n".join(f"- {md(g)}" for g in gaps) if gaps else "- None recorded.")
    parts.append("\n\n## Sources")
    for s in sources:
        where = f" — {autolink(s['url'])}" if s.get("url") else " (supplied document)"
        parts.append(f"\n- [{s['id']}] {md(s['title'], 256)}{where}")
    parts.append("\n\n---\n" + VERIFIED_NOTE)
    return {"report": "".join(parts) + "\n", "citations": citations}
