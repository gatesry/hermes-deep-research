import unittest

from deep_research.evidence import (
    calculation_problem,
    evidence_problem,
    quote_in_text,
    render_report,
)

SOURCE_TEXT = (
    "Acme Corp annual report.\n"
    "Revenue was 1,200 million USD in 2024, up from\n"
    "1,000 million USD in 2023.\n"
    "Headcount reached 5,400 employees as of 2024-12-31.\n"
    "Headcount was 4,500 employees as of 2024-12-31 in the Europe division alone.\n"
)
SOURCES = [{"id": "S1", "title": "Acme report", "url": "https://acme.example/report", "text": SOURCE_TEXT}]


def finding(statement, quotes, calculation=None):
    out = {"statement": statement, "evidence": [{"sourceId": s, "quote": q} for s, q in quotes]}
    if calculation:
        out["calculation"] = calculation
    return out


class QuoteTests(unittest.TestCase):
    def test_exact_quote_accepted(self):
        f = finding("Revenue grew", [("S1", "Revenue was 1,200 million USD in 2024")])
        self.assertIsNone(evidence_problem(f, SOURCES))

    def test_whitespace_reflowed_quote_accepted(self):
        q = "Revenue was 1,200 million USD in 2024, up from 1,000 million USD in 2023."
        self.assertTrue(quote_in_text(q, SOURCE_TEXT))
        self.assertIsNone(evidence_problem(finding("x", [("S1", q)]), SOURCES))

    def test_paraphrase_rejected(self):
        f = finding("Revenue grew", [("S1", "Revenue was about 1.2 billion dollars in 2024")])
        self.assertIsNotNone(evidence_problem(f, SOURCES))

    def test_short_quote_rejected(self):
        f = finding("x", [("S1", "Revenue was")])
        self.assertIn("20", evidence_problem(f, SOURCES))

    def test_unknown_source_rejected(self):
        f = finding("x", [("S9", "Revenue was 1,200 million USD in 2024")])
        self.assertIsNotNone(evidence_problem(f, SOURCES))

    def test_no_evidence_rejected(self):
        self.assertIsNotNone(evidence_problem({"statement": "x", "evidence": []}, SOURCES))

    def test_case_change_rejected(self):
        f = finding("x", [("S1", "REVENUE WAS 1,200 MILLION USD IN 2024")])
        self.assertIsNotNone(evidence_problem(f, SOURCES))


Q_2024 = "Revenue was 1,200 million USD in 2024"
Q_2023 = "1,000 million USD in 2023."


def calc(op, value, unit, left=None, right=None, as_of="2024"):
    left = left or {"value": 1000, "unit": "million USD", "asOf": "2024", "sourceId": "S1", "quote": Q_2024}
    right = right or {"value": 1200, "unit": "million USD", "asOf": "2024", "sourceId": "S1", "quote": Q_2024}
    return {"operation": op, "inputs": [left, right], "value": value, "unit": unit, "asOf": as_of}


HEAD_A = "Headcount reached 5,400 employees as of 2024-12-31."
HEAD_B = "Headcount was 4,500 employees as of 2024-12-31 in the Europe division alone."


def head_calc(op, value, unit):
    a = {"value": 5400, "unit": "employees", "asOf": "2024-12-31", "sourceId": "S1", "quote": HEAD_A}
    b = {"value": 4500, "unit": "employees", "asOf": "2024-12-31", "sourceId": "S1", "quote": HEAD_B}
    return finding("calc", [("S1", HEAD_A), ("S1", HEAD_B)],
                   {"operation": op, "inputs": [a, b], "value": value, "unit": unit, "asOf": "2024-12-31"})


class CalculationTests(unittest.TestCase):
    def test_sum(self):
        self.assertIsNone(evidence_problem(head_calc("sum", 9900, "employees"), SOURCES))

    def test_difference(self):
        self.assertIsNone(evidence_problem(head_calc("difference", 900, "employees"), SOURCES))

    def test_ratio(self):
        self.assertIsNone(evidence_problem(head_calc("ratio", 1.2, "ratio"), SOURCES))

    def test_percent_change_rounded_value_accepted(self):
        # (4500 - 5400) / 5400 * 100 = -16.666...; the model printed two decimals.
        self.assertIsNone(evidence_problem(head_calc("percent-change", -16.67, "%"), SOURCES))

    def test_wrong_value_rejected(self):
        self.assertIn("does not match", evidence_problem(head_calc("sum", 9800, "employees"), SOURCES))

    def test_rounding_too_coarse_rejected(self):
        self.assertIsNotNone(evidence_problem(head_calc("percent-change", -16.0, "%"), SOURCES))

    def test_wrong_result_unit_rejected(self):
        self.assertIsNotNone(evidence_problem(head_calc("ratio", 1.2, "employees"), SOURCES))

    def test_value_missing_from_quote_rejected(self):
        f = head_calc("sum", 9901, "employees")
        f["calculation"]["inputs"][0]["value"] = 5401
        self.assertIn("value, unit, or date", evidence_problem(f, SOURCES))

    def test_date_missing_from_quote_rejected(self):
        f = finding("x", [("S1", Q_2024)], calc("sum", 2400, "million USD", as_of="2024",
                    left={"value": 1200, "unit": "million USD", "asOf": "2025", "sourceId": "S1", "quote": Q_2024}))
        self.assertIsNotNone(evidence_problem(f, SOURCES))

    def test_mixed_dates_rejected(self):
        left = {"value": 1000, "unit": "million USD", "asOf": "2023", "sourceId": "S1", "quote": Q_2023}
        right = {"value": 1200, "unit": "million USD", "asOf": "2024", "sourceId": "S1", "quote": Q_2024}
        f = finding("x", [("S1", Q_2023), ("S1", Q_2024)], calc("percent-change", 20, "%", left, right))
        self.assertIn("different units or dates", evidence_problem(f, SOURCES))

    def test_input_quote_must_be_cited_evidence(self):
        f = head_calc("sum", 9900, "employees")
        f["evidence"] = f["evidence"][:1]
        self.assertIsNotNone(evidence_problem(f, SOURCES))

    def test_division_by_zero_rejected(self):
        a = {"value": 0, "unit": "x", "asOf": "d", "sourceId": "S1", "quote": "q"}
        problem = calculation_problem(
            {"operation": "ratio", "inputs": [a, dict(a)], "value": 0, "unit": "ratio", "asOf": "d"},
            [{"sourceId": "S1", "quote": "q"}])
        self.assertIsNotNone(problem)

    def test_unknown_operation_rejected(self):
        self.assertIsNotNone(evidence_problem(head_calc("product", 1, "employees"), SOURCES))

    def test_non_numeric_value_rejected(self):
        f = head_calc("sum", 9900, "employees")
        f["calculation"]["inputs"][0]["value"] = "5400"
        self.assertIsNotNone(evidence_problem(f, SOURCES))


class RenderTests(unittest.TestCase):
    def test_report_sections_and_citation_ranges(self):
        findings = [finding("Revenue *grew* [S2] to 1,200", [("S1", Q_2024)])]
        out = render_report("What happened to `revenue`?", findings, SOURCES, ["Margin unknown"])
        report = out["report"]
        self.assertTrue(report.startswith("# What happened to \\`revenue\\`?"))
        self.assertIn("## Findings", report)
        self.assertIn("## Unresolved\n- Margin unknown", report)
        self.assertIn("## Sources\n- [S1] Acme report", report)
        # Model text cannot forge citation markers or Markdown emphasis.
        self.assertIn("(S2)", report)
        self.assertIn("\\*grew\\*", report)
        data = report.encode("utf-8")
        (cit,) = out["citations"]
        self.assertEqual(data[cit["startByte"]:cit["endByte"]], b"[S1]")

    def test_unresolved_always_present(self):
        out = render_report("Q", [], SOURCES, [])
        self.assertIn("## Unresolved\n- None recorded.", out["report"])

    def test_renderer_refuses_citations_outside_retained_corpus(self):
        ghost = {"statement": "Ghost claim", "evidence": [{"sourceId": "S99", "quote": "x" * 30}]}
        out = render_report("Q", [ghost], SOURCES, [])
        self.assertNotIn("Ghost claim", out["report"].split("## Disagreements")[0])
        self.assertNotIn("[S99]", out["report"])
        self.assertEqual(out["citations"], [])
        self.assertIn("Citation outside the retained sources omitted: Ghost claim", out["report"])

    def test_disagreements_section_always_present(self):
        out = render_report("Q", [], SOURCES, [])
        self.assertIn("## Disagreements\n- None found.", out["report"])
        out = render_report("Q", [], SOURCES, [], disagreements=["S1 says 5, S2 says *7*"])
        self.assertIn("## Disagreements\n- S1 says 5, S2 says \\*7\\*", out["report"])
        self.assertLess(out["report"].index("## Disagreements"), out["report"].index("## Unresolved"))

    def test_calculation_rendered(self):
        out = render_report("Q", [head_calc("sum", 9900, "employees")], SOURCES, [])
        self.assertIn("checked: sum of 5400 employees and 4500 employees = 9900 employees (2024-12-31)", out["report"])

    def test_link_in_source_title_neutralised(self):
        src = [{"id": "S1", "title": "[click](javascript:alert(1))", "url": "https://a.example/", "text": ""}]
        report = render_report("Q", [], src, [])["report"]
        self.assertNotIn("[click](", report)


if __name__ == "__main__":
    unittest.main()
