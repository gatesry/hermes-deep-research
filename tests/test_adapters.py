import importlib.util
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace

from deep_research.adapters import AUDIT_TASK, HermesIO, ToolUnavailable, clean_extract

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeLlm:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        return SimpleNamespace(text='{"ok": true}', provider="prov", model="m1",
                               usage=SimpleNamespace(total_tokens=42))


class FakeCtx:
    def __init__(self, tool_replies=None):
        self.llm = FakeLlm()
        self.tool_replies = tool_replies or {}
        self.dispatched = []
        self.tools = {}
        self.aux = {}
        self.settings = {}
        self.unload = []

    def on_unload(self, callback):
        self.unload.append(callback)

    def dispatch_tool(self, name, args):
        self.dispatched.append((name, args))
        return self.tool_replies[name]

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kw}

    def register_auxiliary_task(self, key, **kw):
        self.aux[key] = kw

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class AdapterTests(unittest.TestCase):
    def test_complete_routes_audit_to_registered_task(self):
        ctx = FakeCtx()
        io = HermesIO(ctx, max_output_tokens=1000)
        out = io.complete("sys", "prompt", audit=True, timeout=30, kind="audit")
        self.assertEqual(out, {"text": '{"ok": true}', "tokens": 42, "model": "prov/m1"})
        messages, kw = ctx.llm.calls[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(kw["task"], AUDIT_TASK)
        self.assertEqual(kw["max_tokens"], 1000)
        io.complete("sys", "prompt", kind="plan")
        self.assertIsNone(ctx.llm.calls[1][1]["task"])

    def test_search_parses_hermes_shape(self):
        reply = json.dumps({"success": True, "data": {"web": [{"url": "https://x.example/a", "title": "A",
                                                                "description": "snippet is not evidence"}]}})
        ctx = FakeCtx({"web_search": reply})
        self.assertEqual(HermesIO(ctx).search("q"), [{"url": "https://x.example/a", "title": "A"}])
        self.assertEqual(ctx.dispatched[0], ("web_search", {"query": "q", "limit": 5}))

    def test_search_failure_raises(self):
        ctx = FakeCtx({"web_search": json.dumps({"success": False, "error": "no key"})})
        with self.assertRaises(ToolUnavailable):
            HermesIO(ctx).search("q")

    def test_fetch_strips_truncation_footer_and_local_path(self):
        content = ("head text\n\n[... middle omitted — see footer ...]\n\ntail text\n\n"
                   "──────── [TRUNCATED] ────────\nShowing 9 chars\nFull text saved to: /home/someone/cache/web/x.md\n"
                   "─────────────────────────────")
        ctx = FakeCtx({"web_extract": json.dumps({"results": [{"url": "https://x.example/a", "title": "A",
                                                               "content": content, "error": None}]})})
        page = HermesIO(ctx).fetch("https://x.example/a")
        self.assertEqual(page["text"], "head text\n[…]\ntail text")
        self.assertNotIn("/home/", page["text"])
        self.assertEqual(ctx.dispatched[0][1]["char_limit"], 12000)

    def test_fetch_error_raises(self):
        ctx = FakeCtx({"web_extract": json.dumps({"results": [{"url": "u", "content": "", "error": "blocked"}]})})
        from deep_research.adapters import PageUnavailable
        with self.assertRaises(PageUnavailable):  # one page failed; the extractor itself still works
            HermesIO(ctx).fetch("u")

    def test_clean_extract_leaves_normal_text(self):
        self.assertEqual(clean_extract("plain page"), "plain page")


class RegistrationTests(unittest.TestCase):
    def load_plugin(self):
        spec = importlib.util.spec_from_file_location("hermes_deep_research_under_test", ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_register_declares_tool_and_audit_task(self):
        ctx = FakeCtx()
        self.load_plugin().register(ctx)
        self.assertEqual(set(ctx.tools), {"deep_research"})
        self.assertEqual(ctx.tools["deep_research"]["schema"]["name"], "deep_research")
        self.assertIn(AUDIT_TASK, ctx.aux)
        # Capabilities never touches storage, so it works without a Hermes host.
        out = json.loads(ctx.tools["deep_research"]["handler"]({"action": "capabilities"}))
        self.assertEqual(out["engines"], ["portable"])

    def test_unload_callback_registered_and_safe_without_a_manager(self):
        ctx = FakeCtx()
        self.load_plugin().register(ctx)
        self.assertEqual(len(ctx.unload), 1)
        ctx.unload[0]()  # no manager was created; must not open storage or raise
        out = json.loads(ctx.tools["deep_research"]["handler"]({"action": "status", "jobId": "x"},
                                                                conversation_id="c"))
        self.assertIn("unload", out["error"])  # no new manager after unload

    def test_plugin_has_no_network_client_of_its_own(self):
        # All fetching must go through Hermes' web tools so their SSRF/redirect policy applies.
        banned = ("import socket", "urllib.request", "http.client", "import requests", "import httpx",
                  "import aiohttp", "urlopen(")
        for path in [ROOT / "__init__.py", *sorted((ROOT / "deep_research").glob("*.py"))]:
            text = path.read_text()
            for needle in banned:
                self.assertNotIn(needle, text, f"{path.name} uses {needle}")

    def test_manifest_matches_registration(self):
        text = (ROOT / "plugin.yaml").read_text()
        self.assertIn("provides_tools:\n  - deep_research", text)
        self.assertIn("license: MIT", text)


if __name__ == "__main__":
    unittest.main()


class AdapterReviewFixTests(unittest.TestCase):
    def test_tool_level_failure_keeps_a_short_reason(self):
        from deep_research.adapters import HermesIO, PageUnavailable, ToolUnavailable
        ctx = FakeCtx({"web_extract": json.dumps({"success": False,
                       "error": "ddgs is a search-only backend; see /Users/someone/.hermes/config.yaml"})})
        with self.assertRaises(ToolUnavailable) as caught:
            HermesIO(ctx).fetch("https://a.example/")
        self.assertIn("search-only backend", str(caught.exception))
        self.assertNotIn("/Users/someone", str(caught.exception))
        ctx = FakeCtx({"web_extract": json.dumps({"results": [{"url": "https://a.example/", "error": "404"}]})})
        with self.assertRaises(PageUnavailable):
            HermesIO(ctx).fetch("https://a.example/")

    def test_schema_accepts_what_the_contract_accepts(self):
        from deep_research.tool import SCHEMA
        props = SCHEMA["parameters"]["properties"]["request"]["properties"]
        self.assertIn("engine", props)

    def test_boolean_settings_fall_back_to_defaults(self):
        module = RegistrationTests.load_plugin(self)
        ctx = FakeCtx()
        ctx.settings = {"max_active_jobs": True}
        self.assertEqual(module._int_setting(ctx, "max_active_jobs", 4, 1, 16), 4)
