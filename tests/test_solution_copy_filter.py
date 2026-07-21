"""HIVE-336 — tests for the solution-copy write filter.

The filter must BLOCK a memory that reproduces a long verbatim span of the gold
`patch` / `test_patch`, and KEEP genuine transferable learnings (even when they
mention the same symbols). No semantic/theme dedup — verbatim spans only.

Runs under both `python3 -m pytest tests/` and `python3 -m unittest` (stdlib only,
no venv required).
"""
from __future__ import annotations

import unittest

from solution_copy_filter import (
    DEFAULT_NGRAM_THRESHOLD,
    check_memory,
    extract_diff_code,
    longest_shared_span,
    tokenize,
)

# A django-flavoured gold patch: DateTimeField grows a naive-datetime guard.
GOLD_PATCH = """diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 1234567..89abcde 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1520,6 +1520,11 @@ class DateTimeField(DateField):
             value = datetime.datetime(value.year, value.month, value.day)
             if settings.USE_TZ:
                 default_timezone = timezone.get_default_timezone()
+                if settings.USE_TZ and timezone.is_naive(value):
+                    warnings.warn(
+                        "DateTimeField received a naive datetime while time zone "
+                        "support is active.", RuntimeWarning)
+                    value = timezone.make_aware(value, default_timezone)
                 value = value.astimezone(timezone.utc)
             return value
"""

GOLD_TEST_PATCH = """diff --git a/tests/model_fields/test_datetimefield.py b/tests/model_fields/test_datetimefield.py
--- a/tests/model_fields/test_datetimefield.py
+++ b/tests/model_fields/test_datetimefield.py
@@ -40,6 +40,13 @@ class DateTimeFieldTests(TestCase):
+    def test_naive_datetime_with_use_tz(self):
+        field = DateTimeField()
+        naive = datetime.datetime(2020, 1, 1, 12, 0, 0)
+        with self.assertWarns(RuntimeWarning):
+            result = field.clean(naive, None)
+        self.assertTrue(timezone.is_aware(result))
"""

# The agent tries to store the fix verbatim (copy-paste of the added hunk).
VERBATIM_PATCH_MEMORY = (
    "The fix for this issue is: if settings.USE_TZ and timezone.is_naive(value): "
    'warnings.warn("DateTimeField received a naive datetime while time zone '
    'support is active.", RuntimeWarning) value = timezone.make_aware(value, '
    "default_timezone)"
)

# The agent copies the hidden test verbatim.
VERBATIM_TEST_MEMORY = (
    "def test_naive_datetime_with_use_tz(self): field = DateTimeField() "
    "naive = datetime.datetime(2020, 1, 1, 12, 0, 0) "
    "with self.assertWarns(RuntimeWarning): result = field.clean(naive, None) "
    "self.assertTrue(timezone.is_aware(result))"
)

# A genuine transferable learning: describes the shape of the fix, names the same
# symbols, but copies no long contiguous span of patch code.
GENERAL_NOTE_MEMORY = (
    "In django's DateTimeField, when USE_TZ is enabled you must guard against "
    "naive datetimes reaching the ORM layer: detect naivety early and make the "
    "value timezone-aware before any astimezone conversion, otherwise comparisons "
    "silently misbehave. Compare how DateField and DateTimeField differ in their "
    "clean/pre_save paths when reasoning about timezone regressions."
)

# A short reference to the relevant symbols — legitimate, must be kept.
SHORT_SYMBOL_MEMORY = (
    "The relevant helpers here are timezone.make_aware and timezone.is_naive; the "
    "bug lives in the DateTimeField value-coercion path."
)


class TokenizeTests(unittest.TestCase):
    def test_splits_identifiers_and_symbols(self):
        toks = tokenize("timezone.make_aware(value)")
        self.assertEqual(toks, ["timezone", ".", "make_aware", "(", "value", ")"])

    def test_whitespace_insensitive(self):
        self.assertEqual(
            tokenize("a . b ( c )"),
            tokenize("a.b(c)"),
        )


class ExtractDiffCodeTests(unittest.TestCase):
    def test_keeps_changed_lines_drops_headers_and_context(self):
        code = extract_diff_code(GOLD_PATCH)
        # changed content is present
        self.assertIn("timezone.is_naive(value)", code)
        self.assertIn("make_aware(value, default_timezone)", code)
        # diff syntax is gone
        self.assertNotIn("diff --git", code)
        self.assertNotIn("@@", code)
        self.assertNotIn("+++", code)
        # a context line (no +/-) is dropped
        self.assertNotIn("value.astimezone(timezone.utc)", code)

    def test_empty_diff(self):
        self.assertEqual(extract_diff_code(""), "")


class LongestSharedSpanTests(unittest.TestCase):
    def test_reports_contiguous_run(self):
        cand = tokenize("prefix a b c d e suffix")
        ref = tokenize("x y a b c d e z")
        span_len, start = longest_shared_span(cand, ref)
        self.assertEqual(span_len, 5)  # a b c d e
        self.assertEqual(cand[start], "a")

    def test_no_overlap(self):
        span_len, _ = longest_shared_span(tokenize("alpha beta"), tokenize("gamma delta"))
        self.assertEqual(span_len, 0)


class CheckMemoryTests(unittest.TestCase):
    # ---- spike check (b): blocks verbatim, keeps general ----

    def test_blocks_verbatim_patch_copy(self):
        res = check_memory(VERBATIM_PATCH_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH)
        self.assertTrue(res.blocked)
        self.assertEqual(res.source, "patch")
        self.assertGreaterEqual(res.span_len, DEFAULT_NGRAM_THRESHOLD)

    def test_blocks_verbatim_test_copy(self):
        res = check_memory(VERBATIM_TEST_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH)
        self.assertTrue(res.blocked)
        self.assertEqual(res.source, "test_patch")

    def test_keeps_general_note(self):
        res = check_memory(GENERAL_NOTE_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH)
        self.assertFalse(res.blocked)
        self.assertLess(res.span_len, DEFAULT_NGRAM_THRESHOLD)

    def test_keeps_short_symbol_reference(self):
        res = check_memory(SHORT_SYMBOL_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH)
        self.assertFalse(res.blocked)

    # ---- edge cases ----

    def test_empty_patch_never_blocks(self):
        res = check_memory(VERBATIM_PATCH_MEMORY, patch="", test_patch="")
        self.assertFalse(res.blocked)

    def test_candidate_shorter_than_threshold_never_blocks(self):
        res = check_memory("small note", patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH)
        self.assertFalse(res.blocked)

    def test_threshold_is_tunable(self):
        # Knob works in both directions on the SAME verbatim memory: raise the
        # threshold above the copied span and it is allowed; lower it and it blocks.
        loose = check_memory(VERBATIM_PATCH_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH, threshold=10_000)
        self.assertFalse(loose.blocked)
        strict = check_memory(VERBATIM_PATCH_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH, threshold=1)
        self.assertTrue(strict.blocked)

    def test_matched_excerpt_populated_on_block(self):
        res = check_memory(VERBATIM_PATCH_MEMORY, patch=GOLD_PATCH, test_patch=GOLD_TEST_PATCH)
        self.assertTrue(res.matched_excerpt)
        # the excerpt is the verbatim copied span (a fragment of the gold hunk)
        self.assertIn("USE_TZ", res.matched_excerpt)


if __name__ == "__main__":
    unittest.main()
