"""HIVE-335 — tests for the reflect-and-store write path.

The load-bearing test proves the HIVE-336 filter is wired into the write path: given a
reflector that emits one verbatim patch copy and one genuine transferable note, exactly
one is stored and one is blocked (spike check (b) at the integration level). The
reflector is stubbed so no model call happens. Stdlib only.
"""
from __future__ import annotations

import unittest

from reflect_and_store import (
    build_reflection_prompt,
    parse_candidates,
    reflect_and_store,
)

GOLD_PATCH = """diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1520,6 +1520,9 @@ class DateTimeField(DateField):
+                if settings.USE_TZ and timezone.is_naive(value):
+                    warnings.warn("DateTimeField received a naive datetime while time zone "
+                                  "support is active.", RuntimeWarning)
+                    value = timezone.make_aware(value, default_timezone)
"""

VERBATIM = (
    "The fix: if settings.USE_TZ and timezone.is_naive(value): "
    'warnings.warn("DateTimeField received a naive datetime while time zone '
    'support is active.", RuntimeWarning) value = timezone.make_aware(value, default_timezone)'
)
GENERAL = (
    "For django timezone bugs, guard naive datetimes at the field-coercion boundary and "
    "make them aware before ORM comparisons; check DateTimeField vs DateField clean paths."
)

INSTANCE = {
    "instance_id": "django__django-12345",
    "problem_statement": "DateTimeField mishandles naive datetimes when USE_TZ is on.",
    "patch": GOLD_PATCH,
    "test_patch": "",
}


class FakeStoreClient:
    def __init__(self):
        self.stored: list = []
        self._next = 100

    def store_memory(self, content, mem_type, *, importance=5, tags=None):
        self._next += 1
        self.stored.append({"content": content, "type": mem_type, "importance": importance, "tags": tags})
        return {"id": self._next}


def stub_reflector(*_args, **_kwargs):
    return [
        {"content": VERBATIM, "type": "insight", "importance": 6, "tags": ["django"]},
        {"content": GENERAL, "type": "insight", "importance": 6, "tags": ["django", "timezone"]},
    ]


class ReflectAndStoreTests(unittest.TestCase):
    def test_blocks_verbatim_stores_general(self):
        client = FakeStoreClient()
        report = reflect_and_store(client, INSTANCE, "…transcript…", GOLD_PATCH, "ok",
                                   reflector=stub_reflector)
        self.assertEqual(report.candidates, 2)
        self.assertEqual(report.stored, 1)
        self.assertEqual(report.blocked, 1)
        self.assertEqual(len(client.stored), 1)
        self.assertIn("guard naive datetimes", client.stored[0]["content"])
        self.assertEqual(report.blocked_details[0]["source"], "patch")
        self.assertEqual(report.stored_ids, [101])

    def test_dry_run_stores_nothing_but_counts(self):
        client = FakeStoreClient()
        report = reflect_and_store(client, INSTANCE, "t", GOLD_PATCH, "ok",
                                   reflector=stub_reflector, dry_run=True)
        self.assertEqual(report.stored, 1)
        self.assertEqual(report.blocked, 1)
        self.assertEqual(client.stored, [])  # nothing actually written

    def test_reflector_failure_is_contained(self):
        def boom(*_a, **_k):
            raise RuntimeError("model down")
        client = FakeStoreClient()
        report = reflect_and_store(client, INSTANCE, "t", GOLD_PATCH, "ok", reflector=boom)
        self.assertIsNotNone(report.error)
        self.assertEqual(report.stored, 0)
        self.assertEqual(client.stored, [])

    def test_empty_reflection(self):
        client = FakeStoreClient()
        report = reflect_and_store(client, INSTANCE, "t", GOLD_PATCH, "ok",
                                   reflector=lambda *a, **k: [])
        self.assertEqual(report.candidates, 0)
        self.assertEqual(report.stored, 0)


class ParseCandidatesTests(unittest.TestCase):
    def test_plain_json_array(self):
        cands = parse_candidates('[{"content":"x","type":"insight","importance":7,"tags":["a"]}]')
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["importance"], 7)

    def test_fenced_with_preamble(self):
        text = 'Here you go:\n```json\n[{"content":"y","type":"convention"}]\n```\ndone'
        cands = parse_candidates(text)
        self.assertEqual(cands[0]["type"], "convention")
        self.assertEqual(cands[0]["importance"], 6)  # default

    def test_bad_type_normalized_and_importance_clamped(self):
        cands = parse_candidates('[{"content":"z","type":"session_summary","importance":99}]')
        self.assertEqual(cands[0]["type"], "insight")   # not in allowed write set -> insight
        self.assertEqual(cands[0]["importance"], 10)     # clamped

    def test_malformed_returns_empty(self):
        self.assertEqual(parse_candidates("not json at all"), [])
        self.assertEqual(parse_candidates(""), [])

    def test_drops_empty_content(self):
        self.assertEqual(parse_candidates('[{"content":"   ","type":"insight"}]'), [])


class PromptTests(unittest.TestCase):
    def test_prompt_shape_and_guardrail(self):
        msgs = build_reflection_prompt("issue text", "transcript", "the diff", "ok", n=3)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("TRANSFERABLE", msgs[0]["content"])
        self.assertIn("NEVER include the specific diff", msgs[0]["content"])
        self.assertEqual(msgs[1]["role"], "user")
        self.assertIn("issue text", msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
