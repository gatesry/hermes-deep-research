import json
import time
import unittest

from deep_research import passages
from deep_research.contract import CORPUS_MAX_BYTES, EVIDENCE_VIEW_BYTES, Budget, normalize_request
from deep_research.engine import UNTRUSTED_MARKER, Engine
from tests.fakes import FakeIO, MemorySteps


def run(request, io):
    req = normalize_request(request)
    budget = Budget(req["budget"], deadline_at=time.time() + 60)
    engine = Engine(req, io, MemorySteps(), budget, documents_at="2026-01-01T00:00:00Z")
    return engine.run(), engine


def prompts(io, kind):
    return [json.loads(p.split(UNTRUSTED_MARKER, 1)[1]) for k, p in io.prompts if k == kind]


def view_bytes(data):
    return sum(len(p.encode("utf-8")) for s in data["sources"] for p in s["passages"])


def filler(seed, n):
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    out, i = [], seed
    while sum(len(w) + 1 for w in out) < n:
        out.append(words[i % len(words)] + str(i % 97))
        i += 7
        if i % 11 == 0:
            out[-1] += "."
    return " ".join(out)[:n]


class WindowTests(unittest.TestCase):
    def test_windows_are_contiguous_exact_slices(self):
        text = "Para one.\n\n" + filler(1, 5000) + "\nline\n" + "x" * 3000
        spans = passages.windows(text)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        for (a, b), (c, _) in zip(spans, spans[1:]):
            self.assertEqual(b, c)
            self.assertLessEqual(b - a, 1_200)
        self.assertEqual("".join(text[a:b] for a, b in spans), text)

    def test_short_text_is_one_window(self):
        self.assertEqual(passages.windows("short"), [(0, 5)])
        self.assertEqual(passages.windows(""), [])

    def test_merge_joins_adjacent_windows_only(self):
        text = "".join(f"{i}" * 900 for i in range(5))
        spans = passages.windows(text)
        merged = passages.merge(text, spans, [0, 1, 3])
        self.assertEqual(len(merged), 2)
        self.assertTrue(all(m in text for m in merged))

    def test_locate_handles_whitespace_runs(self):
        text = "First line.\n\n  The   quick brown\nfox jumps."
        a, b = passages.locate("The quick brown fox jumps.", text)
        self.assertEqual(text[a:b], "The   quick brown\nfox jumps.")
        self.assertIsNone(passages.locate("not present at all", text))

    def test_bm25_prefers_matching_windows(self):
        docs = [passages.tokens("nothing relevant here"), passages.tokens("python release date october"),
                passages.tokens("python python python")]
        scores = passages.bm25_scores(docs, passages.tokens("python release date"))
        self.assertEqual(scores[0], 0)
        self.assertGreater(scores[1], scores[2])


class SelectionTests(unittest.TestCase):
    def test_model_sees_ranked_passages_within_budget_from_every_page(self):
        pages = {f"https://p{i}.example/doc": (f"Page {i}", f"Lead {i}. " + filler(i, 11_900)) for i in range(20)}
        pages["https://p7.example/doc"] = ("Page 7", "Lead 7. " + filler(7, 9_000)
                                           + " The zebrafish regeneration rate was 42 percent in the trial. "
                                           + filler(8, 2_800))
        io = FakeIO(pages=pages, hits=[], extraction={"findings": [], "gaps": [], "queries": []})
        result, _ = run({"question": "What was the zebrafish regeneration rate?",
                         "sources": {"web": False, "urls": list(pages)}, "budget": {"fetches": 20}}, io)
        self.assertEqual(len(result["sources"]), 20)  # all retained: 240 KB is under the corpus cap
        data = prompts(io, "extract")[0]
        self.assertLessEqual(view_bytes(data), EVIDENCE_VIEW_BYTES)
        self.assertEqual(len(data["sources"]), 20)  # each page keeps at least its lead window
        joined = " ".join(p for s in data["sources"] for p in s["passages"])
        self.assertIn("zebrafish regeneration rate was 42 percent", joined)
        by_id = {s["id"]: s["text"] for s in result["sources"]}
        for s in data["sources"]:
            self.assertTrue(all(p in by_id[s["id"]] for p in s["passages"]))  # exact slices
        self.assertFalse([g for g in result["gaps"] if "corpus limit" in g])

    def test_audit_sees_the_window_holding_each_quote(self):
        pages = {f"https://p{i}.example/doc": (f"Page {i}", f"Lead {i}. " + filler(i, 11_900)) for i in range(20)}
        hidden = "The obscure maintenance figure was recorded as 913 units"
        text = "Lead 3. " + filler(3, 10_000) + " " + hidden + ". " + filler(4, 1_500)
        pages["https://p3.example/doc"] = ("Page 3", text)
        extraction = {"findings": [{"statement": "913 units were recorded.",
                                    "evidence": [{"sourceId": "S4", "quote": hidden}]}], "gaps": [], "queries": []}
        io = FakeIO(pages=pages, hits=[], extraction=extraction)
        result, _ = run({"question": "zebra", "sources": {"web": False, "urls": list(pages)},
                         "budget": {"fetches": 20, "parallelism": 1}}, io)
        self.assertEqual(result["sources"][3]["url"], "https://p3.example/doc")
        audit = prompts(io, "audit")[0]
        joined = " ".join(p for s in audit["sources"] for p in s["passages"])
        self.assertIn(hidden, joined)
        self.assertLessEqual(view_bytes(audit), EVIDENCE_VIEW_BYTES)
        self.assertEqual(len(result["findings"]), 1)

    def test_follow_up_rounds_add_sources_after_the_view_is_full(self):
        # v0.1 stopped here: five 12 KB pages filled a 60 KB corpus, so round two found "nothing new".
        first = {f"https://r1-{i}.example/doc": (f"R1 {i}", filler(i, 11_990)) for i in range(5)}
        second = {f"https://r2-{i}.example/doc": (f"R2 {i}", "Follow-up answer. " + filler(i + 50, 11_000))
                  for i in range(4)}

        class TwoRoundIO(FakeIO):
            def search(self, query):
                self._count("search")
                pool = second if "follow" in query else first
                return [{"url": u, "title": t} for u, (t, _) in pool.items()]

        io = TwoRoundIO(pages={**first, **second}, plan={"questions": ["widgets"]},
                        extraction={"findings": [], "gaps": ["still missing"], "queries": ["follow up widgets"]})
        result, _ = run({"question": "How many widgets?", "depth": "deep"}, io)
        urls = {s["url"] for s in result["sources"]}
        self.assertTrue(set(second) & urls)
        self.assertGreaterEqual(len(prompts(io, "extract")), 2)
        self.assertEqual(result["usage"]["modelCalls"], io.calls["model"] + io.calls["audit"])
        for data in prompts(io, "extract"):
            self.assertLessEqual(view_bytes(data), EVIDENCE_VIEW_BYTES)

    def test_retained_corpus_still_capped(self):
        docs = [{"title": f"D{i}", "text": "é" * (19_990 - i)} for i in range(10)]
        io = FakeIO(extraction={"findings": [], "gaps": [], "queries": []})
        result, _ = run({"question": "q", "sources": {"documents": docs}}, io)
        total = sum(len(s["text"].encode("utf-8")) for s in result["sources"])
        self.assertLessEqual(total, CORPUS_MAX_BYTES)
        self.assertLessEqual(view_bytes(prompts(io, "extract")[0]), EVIDENCE_VIEW_BYTES)


if __name__ == "__main__":
    unittest.main()
