import threading
import time
import unittest

from deep_research.contract import (
    Budget,
    BudgetExhausted,
    ContractError,
    DeadlineExceeded,
    canonical_request,
    canonical_url,
    domain_allowed,
    normalize_request,
    url_key,
    parse_args,
)


class RequestTests(unittest.TestCase):
    def test_defaults(self):
        req = normalize_request({"question": "Why is the sky blue?"})
        self.assertEqual(req["engine"], "portable")
        self.assertEqual(
            req["budget"],
            {"modelCalls": 8, "searches": 6, "fetches": 12, "parallelism": 2, "durationMs": 180_000},
        )
        self.assertTrue(req["sources"]["web"])

    def test_depth_presets_and_overrides(self):
        quick = normalize_request({"question": "q", "depth": "quick"})
        self.assertEqual(quick["depth"], "quick")
        self.assertEqual(quick["budget"]["modelCalls"], 4)
        deep = normalize_request({"question": "q", "depth": "deep", "budget": {"fetches": 10}})
        self.assertEqual(deep["budget"]["fetches"], 10)
        self.assertEqual(deep["budget"]["modelCalls"], 8)
        self.assertEqual(deep["budget"]["searches"], 12)
        self.assertEqual(normalize_request({"question": "q"})["depth"], "standard")
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "depth": "extreme"})
        for preset in ("quick", "standard", "deep"):
            budget = normalize_request({"question": "q", "depth": preset})["budget"]
            self.assertGreaterEqual(budget["modelCalls"], 3)  # plan + extract + audit

    def test_maximums_enforced(self):
        for key, too_big in (("modelCalls", 31), ("searches", 21), ("fetches", 41), ("parallelism", 5),
                             ("durationMs", 900_001)):
            with self.assertRaises(ContractError, msg=key):
                normalize_request({"question": "q", "budget": {key: too_big}})

    def test_maximums_accepted(self):
        req = normalize_request({"question": "q", "budget": {"modelCalls": 30, "searches": 20, "fetches": 40,
                                                             "parallelism": 4, "durationMs": 900_000}})
        self.assertEqual(req["budget"]["durationMs"], 900_000)

    def test_zero_and_bool_budget_rejected(self):
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "budget": {"searches": 0}})
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "budget": {"searches": True}})

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "colour": "red"})
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "sources": {"files": []}})

    def test_documents_disable_web_by_default(self):
        req = normalize_request({"question": "q", "sources": {"documents": [{"title": "t", "text": "body"}]}})
        self.assertFalse(req["sources"]["web"])
        req = normalize_request({"question": "q", "sources": {"web": True, "documents": [{"title": "t", "text": "b"}]}})
        self.assertTrue(req["sources"]["web"])

    def test_limits_on_lists_and_text(self):
        with self.assertRaises(ContractError):
            normalize_request({"question": "x" * 8001})
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "sources": {"urls": ["https://a.example"] * 21}})
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "sources": {"documents": [{"title": "t", "text": "x" * 20001}]}})

    def test_google_engine_refused(self):
        with self.assertRaises(ContractError) as err:
            normalize_request({"question": "q", "engine": "google"})
        self.assertIn("portable", str(err.exception))

    def test_canonical_request_stable(self):
        a = normalize_request({"question": "q", "budget": {"searches": 2, "fetches": 3}})
        b = normalize_request({"budget": {"fetches": 3, "searches": 2}, "question": "q"})
        self.assertEqual(canonical_request(a), canonical_request(b))

    def test_domains_normalised(self):
        req = normalize_request({"question": "q", "sources": {"allowDomains": ["Example.COM.", "*.gov"]}})
        self.assertEqual(req["sources"]["allowDomains"], ["example.com", "gov"])


class ArgsTests(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(parse_args({"action": "capabilities"})["action"], "capabilities")
        self.assertEqual(parse_args({"action": "status", "jobId": "abc"})["jobId"], "abc")
        start = parse_args({"action": "start", "idempotencyKey": "k1", "request": {"question": "q"}})
        self.assertEqual(start["request"]["question"], "q")

    def test_bad_args(self):
        for bad in ({}, {"action": "explode"}, {"action": "status"}, {"action": "start", "request": {"question": "q"}},
                    {"action": "start", "idempotencyKey": "k", "request": {"question": "q"}, "extra": 1},
                    {"action": "result", "jobId": "x" * 65}):
            with self.assertRaises(ContractError, msg=str(bad)):
                parse_args(bad)


class DomainTests(unittest.TestCase):
    def test_scheme_and_userinfo(self):
        self.assertFalse(domain_allowed("file:///etc/passwd", [], []))
        self.assertFalse(domain_allowed("ftp://example.com/", [], []))
        self.assertFalse(domain_allowed("https://user:pw@example.com/", [], []))
        self.assertTrue(domain_allowed("https://example.com/a", [], []))

    def test_host_normalisation_cannot_bypass_filters(self):
        deny = normalize_request({"question": "q", "sources": {"denyDomains": ["BÜCHER.de", ".Evil.COM."]}})
        deny = deny["sources"]["denyDomains"]
        self.assertEqual(deny, ["xn--bcher-kva.de", "evil.com"])
        for url in ("https://bücher.de/x", "https://XN--BCHER-KVA.DE/x", "https://www.bücher.de./x",
                    "https://EVIL.com/", "https://evil.com./", "https://a.evil.com/",
                    "https://evil\u3002com/"):  # ideographic full stop is an IDNA label separator
            self.assertFalse(domain_allowed(url, [], deny), url)
        self.assertTrue(domain_allowed("https://not-evil.com/", [], deny))
        allow = ["xn--bcher-kva.de"]
        self.assertTrue(domain_allowed("https://Bücher.DE/x", allow, []))
        self.assertFalse(domain_allowed("https://evil-bücher.de/x", allow, []))

    def test_undecodable_or_percent_hosts_fail_closed(self):
        self.assertFalse(domain_allowed("https://%65vil.com/", [], ["evil.com"]))
        self.assertFalse(domain_allowed("https://xn--/", [], []))
        with self.assertRaises(ContractError):
            normalize_request({"question": "q", "sources": {"allowDomains": ["a..b"]}})

    def test_allow_list_matches_subdomains_only(self):
        allow = ["example.com"]
        self.assertTrue(domain_allowed("https://docs.example.com/x", allow, []))
        self.assertTrue(domain_allowed("https://example.com/x", allow, []))
        self.assertFalse(domain_allowed("https://badexample.com/x", allow, []))
        self.assertFalse(domain_allowed("https://example.com.evil.net/x", allow, []))

    def test_deny_wins(self):
        self.assertFalse(domain_allowed("https://ads.example.com/", ["example.com"], ["ads.example.com"]))
        self.assertTrue(domain_allowed("https://www.example.com/", ["example.com"], ["ads.example.com"]))


class BudgetTests(unittest.TestCase):
    def budget(self, **kw):
        limits = {"modelCalls": 2, "searches": 1, "fetches": 3, "parallelism": 2, "durationMs": 60_000}
        limits.update(kw)
        return Budget(limits, deadline_at=time.time() + limits["durationMs"] / 1000)

    def test_admission_counts_and_exhausts(self):
        b = self.budget()
        b.admit("searches")
        with self.assertRaises(BudgetExhausted):
            b.admit("searches")
        self.assertEqual(b.used["searches"], 1)
        self.assertEqual(b.remaining("modelCalls"), 2)

    def test_deadline(self):
        b = Budget({"modelCalls": 2, "searches": 1, "fetches": 3, "parallelism": 2, "durationMs": 1},
                   deadline_at=time.time() - 1)
        with self.assertRaises(DeadlineExceeded):
            b.admit("fetches")
        self.assertEqual(b.used["fetches"], 0)

    def test_threaded_admission_never_overshoots(self):
        b = self.budget(fetches=40)
        admitted = []

        def worker():
            for _ in range(20):
                try:
                    b.admit("fetches")
                    admitted.append(1)
                except BudgetExhausted:
                    return

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(admitted), 40)
        self.assertEqual(b.used["fetches"], 40)


if __name__ == "__main__":
    unittest.main()


class UrlCanonicalTests(unittest.TestCase):
    def test_fragment_case_and_default_port_removed(self):
        self.assertEqual(
            canonical_url("HTTPS://Docs.Example.COM.:443/a/b?id=7&utm_source=x&gclid=z#part"),
            "https://docs.example.com/a/b?id=7&utm_source=x&gclid=z")  # query is fetched exactly as given

    def test_meaningful_query_order_and_case_preserved(self):
        self.assertEqual(canonical_url("https://e.example/p?b=2&A=1"), "https://e.example/p?b=2&A=1")
        self.assertEqual(canonical_url("https://e.example/Path"), "https://e.example/Path")

    def test_idna_host_and_ip_literals(self):
        self.assertEqual(canonical_url("https://Bücher.example/x"), "https://xn--bcher-kva.example/x")
        self.assertEqual(canonical_url("http://[2001:DB8::0001]:80/"), "http://[2001:db8::1]/")
        self.assertEqual(canonical_url("http://192.0.2.1:8080/x"), "http://192.0.2.1:8080/x")

    def test_unusable_urls_rejected(self):
        for bad in ["ftp://e.example/x", "https://user:pw@e.example/", "javascript:alert(1)", "https:///nohost",
                    "https://e%78ample.com/", "not a url", "https://e.example:99999/"]:
            self.assertIsNone(canonical_url(bad), bad)

    def test_equivalent_spellings_share_a_key(self):
        variants = ["https://e.example/doc", "https://E.example/doc#x", "https://e.example:443/doc",
                    "https://e.example/doc?utm_campaign=1&fbclid=2"]
        self.assertEqual(len({url_key(v) for v in variants}), 1)
        self.assertEqual(url_key("https://e.example"), url_key("https://e.example/"))

    def test_scheme_slash_and_empty_query_segments_stay_distinct(self):
        # These can name different resources, so they are never folded together.
        self.assertNotEqual(url_key("http://e.example/doc"), url_key("https://e.example/doc"))
        self.assertNotEqual(url_key("https://e.example/doc"), url_key("https://e.example/doc/"))
        self.assertEqual(canonical_url("https://e.example/doc?a=1&&b=2"), "https://e.example/doc?a=1&&b=2")

    def test_distinct_documents_keep_distinct_keys(self):
        self.assertNotEqual(url_key("https://e.example/doc"), url_key("https://e.example/doc2"))
        self.assertNotEqual(url_key("https://e.example/doc?id=1"), url_key("https://e.example/doc?id=2"))
        self.assertNotEqual(url_key("https://e.example/doc"), url_key("https://e.example:8443/doc"))
        self.assertNotEqual(url_key("https://e.example/doc"), url_key("https://sub.e.example/doc"))
