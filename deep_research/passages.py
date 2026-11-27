"""Passage selection: cut retained pages into windows and pick the best ones for each model call.

Whole pages fill the model's evidence budget after a handful of fetches, so later rounds could
never add anything. Instead every retained page is cut into ~900-character windows (exact slices of
the page text, so quotes copied from them still verify), each page keeps its lead window, and the
rest of the budget goes to the windows that rank best under BM25 against the research questions.
Pure standard library and deterministic, so replayed jobs select the same passages.
"""

from __future__ import annotations

import math
import re
from collections import Counter

WINDOW_CHARS = 900
_MIN_CUT = 600
_MAX_CUT = 1_200
PER_SOURCE_FIRST_PASS = 6  # windows per source before the remaining budget is shared freely
SEPARATOR = "\n\n[…]\n\n"
_K1, _B = 1.2, 0.75
_TOKEN = re.compile(r"[0-9a-z]+(?:[.'][0-9a-z]+)*")
_STOP = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the this to was were what "
    "when where which who why will with does did do can should would than then there these those into "
    "about over under between versus vs".split()
)


def windows(text: str, size: int = WINDOW_CHARS) -> list[tuple[int, int]]:
    """Contiguous (start, end) slices covering ``text``, cut at paragraph, line, sentence or word breaks."""
    spans: list[tuple[int, int]] = []
    start, n = 0, len(text)
    lo_off, hi_off = _MIN_CUT * size // WINDOW_CHARS, _MAX_CUT * size // WINDOW_CHARS
    while start < n:
        if n - start <= hi_off:
            spans.append((start, n))
            break
        lo, hi = start + lo_off, start + hi_off
        cut = -1
        for mark in ("\n\n", "\n", ". ", "; ", " "):
            pos = text.rfind(mark, lo, hi)
            if pos != -1:
                cut = pos + len(mark)
                break
        if cut <= start:
            cut = start + size
        spans.append((start, cut))
        start = cut
    return spans


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1 and t not in _STOP]


def bm25_scores(docs: list[list[str]], query: list[str]) -> list[float]:
    if not docs:
        return []
    terms = set(query)
    n = len(docs)
    avg = sum(len(d) for d in docs) / n or 1.0
    df = Counter(t for d in docs for t in set(d) if t in terms)
    weights = Counter(query)
    scores = []
    for doc in docs:
        tf = Counter(t for t in doc if t in terms)
        score = 0.0
        for term, freq in tf.items():
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += weights[term] * idf * freq * (_K1 + 1) / (freq + _K1 * (1 - _B + _B * len(doc) / avg))
        scores.append(score)
    return scores


def select(sources: list[dict], query: str, budget: int, pinned: dict[str, set[int]] | None = None) -> dict[str, list[int]]:
    """Window indices to show per source id, in page order, within ``budget`` UTF-8 bytes of text.

    Order of preference: ``pinned`` windows (for example those holding quotes under audit), each
    source's lead window, then windows by BM25 score with at most PER_SOURCE_FIRST_PASS per source
    on the first pass, then any remaining scored windows, then unscored windows in page order.
    """
    pinned = pinned or {}
    items = []  # (source_index, window_index, byte_size, tokens)
    for si, src in enumerate(sources):
        text = src["text"]
        for wi, (a, b) in enumerate(src["windows"]):
            chunk = text[a:b]
            items.append((si, wi, len(chunk.encode("utf-8")), tokens(src["title"] + " " + chunk if wi == 0 else chunk)))
    scores = bm25_scores([it[3] for it in items], tokens(query))
    chosen: dict[int, set[int]] = {}
    used = 0

    def take(si: int, wi: int, size: int) -> bool:
        nonlocal used
        if wi in chosen.get(si, ()):
            return True
        if used + size > budget:
            return False
        chosen.setdefault(si, set()).add(wi)
        used += size
        return True

    by_key = {(it[0], it[1]): it for it in items}
    ids = [s["id"] for s in sources]
    for si, sid in enumerate(ids):
        for wi in sorted(pinned.get(sid, ())):
            if (si, wi) in by_key:
                take(si, wi, by_key[(si, wi)][2])
    for si in range(len(sources)):
        if (si, 0) in by_key:
            take(si, 0, by_key[(si, 0)][2])
    ranked = sorted(range(len(items)), key=lambda i: (-scores[i], items[i][0], items[i][1]))
    scored = [i for i in ranked if scores[i] > 0]
    for cap in (PER_SOURCE_FIRST_PASS, None):
        for i in scored:
            si, wi, size, _ = items[i]
            if cap is None or len(chosen.get(si, ())) < cap:
                take(si, wi, size)
    for i in sorted((i for i in ranked if scores[i] <= 0), key=lambda i: (items[i][0], items[i][1])):
        take(items[i][0], items[i][1], items[i][2])
    return {ids[si]: sorted(ws) for si, ws in chosen.items()}


def merge(text: str, spans: list[tuple[int, int]], picked: list[int]) -> list[str]:
    """Join adjacent picked windows into contiguous exact passages."""
    out: list[str] = []
    run_start = run_end = None
    for wi in sorted(picked):
        a, b = spans[wi]
        if run_end is not None and a == run_end:
            run_end = b
            continue
        if run_start is not None:
            out.append(text[run_start:run_end])
        run_start, run_end = a, b
    if run_start is not None:
        out.append(text[run_start:run_end])
    return out


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Character span of ``quote`` in ``text``: exact, else after collapsing whitespace runs."""
    pos = text.find(quote)
    if pos != -1:
        return pos, pos + len(quote)
    target = re.sub(r"\s+", " ", quote).strip()
    if not target:
        return None
    # Collapse the text while remembering where each kept character came from.
    kept, origin, prev_space = [], [], True
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_space:
                kept.append(" ")
                origin.append(i)
            prev_space = True
        else:
            kept.append(ch)
            origin.append(i)
            prev_space = False
    pos = "".join(kept).find(target)
    if pos == -1:
        return None
    return origin[pos], origin[pos + len(target) - 1] + 1


def covering(spans: list[tuple[int, int]], start: int, end: int) -> set[int]:
    return {wi for wi, (a, b) in enumerate(spans) if a < end and b > start}
