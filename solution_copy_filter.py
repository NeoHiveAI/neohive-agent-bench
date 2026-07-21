#!/usr/bin/env python3
"""HIVE-336 — solution-copy write filter.

Gate for the reflect-and-store write path (HIVE-335): block a memory only when it
reproduces a *long verbatim span* of the gold `patch` or `test_patch`, while letting
genuine transferable learnings through untouched. This is the write-side complement
to the read-side base_commit contamination guard (HIVE-267/269): the read guard keeps
the answer out of the *indexed code*; this keeps the answer out of the *agent's
written memories*, so a later round can't retrieve a copy-pasted solution.

Design (deliberately NOT semantic dedup — see the decision memory, id 390):
  - We detect only VERBATIM overlap. A paraphrase, a renamed-variable version, or a
    conceptual note about the same code is a real learning and MUST persist.
  - The unit of comparison is a token n-gram. Tokens are ``\\w+`` runs and single
    non-space symbols (``re`` pattern ``\\w+|[^\\w\\s]``), so whitespace/reformatting
    cannot evade the filter and each punctuation char counts toward span length.
  - Only the diff's *changed* lines (``+``/``-`` content) are treated as "the answer";
    diff headers/hunks are stripped and unchanged context lines are ignored (they are
    pre-existing repo code, which is legitimately indexed and legitimately discussable).
  - A memory is blocked iff the longest contiguous token span it shares with the
    changed code of `patch` or `test_patch` is >= ``threshold`` tokens.

Stdlib only — importable and testable without the benchmark venv.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A "long" verbatim run, measured in `\w+|[^\w\s]` tokens. Because symbols tokenize
# individually, ~25 tokens is roughly two to three lines of dense code — long enough
# that a shared identifier chain (e.g. `timezone.make_aware(value)`, 6 tokens) never
# trips it, but a copied hunk always does. Tunable per call.
DEFAULT_NGRAM_THRESHOLD = 25

# Bound the reference size so a pathological multi-MB diff can't make the O(m*n) span
# search blow up. SWE-bench gold patches are a few KB; this is a safety valve only.
_MAX_REFERENCE_TOKENS = 40000

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")

# Unified-diff lines that are structure, not code payload.
_DIFF_HEADER_PREFIXES = (
    "diff --git ", "index ", "--- ", "+++ ", "@@ ", "new file mode ",
    "deleted file mode ", "old mode ", "new mode ", "similarity index ",
    "rename from ", "rename to ", "copy from ", "copy to ", "Binary files ",
    "GIT binary patch",
)


def tokenize(text: str) -> list[str]:
    """Whitespace-insensitive code tokenizer: identifier/number runs plus single
    symbol characters. ``"a.b(c)"`` and ``"a . b ( c )"`` tokenize identically."""
    return _TOKEN_RE.findall(text or "")


def extract_diff_code(diff_text: str) -> str:
    """Return the concatenated *changed* code from a unified diff — the added and
    removed content lines with their leading ``+``/``-`` marker stripped — dropping
    all diff syntax and unchanged context lines. This is the text a "verbatim copy of
    the answer" would reproduce."""
    out: list[str] = []
    for line in (diff_text or "").splitlines():
        if any(line.startswith(p) for p in _DIFF_HEADER_PREFIXES):
            continue
        if line[:1] in ("+", "-"):
            out.append(line[1:])
        # unchanged context (leading space) and everything else: skip
    return "\n".join(out)


def longest_shared_span(candidate: list[str], reference: list[str]) -> tuple[int, int]:
    """Longest contiguous run of `candidate` tokens that appears, in order, as a
    sub-sequence of `reference`. Returns ``(length, start_index_in_candidate)``.

    Classic longest-common-substring DP over token lists with a single rolling row
    (O(len(candidate)) space). Reference is capped at ``_MAX_REFERENCE_TOKENS``."""
    if not candidate or not reference:
        return (0, 0)
    ref = reference[:_MAX_REFERENCE_TOKENS]
    m = len(candidate)
    # index reference tokens -> positions, so we only walk matching columns
    ref_positions: dict[str, list[int]] = {}
    for j, tok in enumerate(ref):
        ref_positions.setdefault(tok, []).append(j)

    prev = [0] * (len(ref) + 1)  # prev[j+1] = LCS-suffix ending at candidate[i-1], ref[j]
    best_len = 0
    best_end_i = 0
    for i in range(m):
        cur = [0] * (len(ref) + 1)
        for j in ref_positions.get(candidate[i], ()):
            run = prev[j] + 1
            cur[j + 1] = run
            if run > best_len:
                best_len = run
                best_end_i = i
        prev = cur
    start = best_end_i - best_len + 1
    return (best_len, start)


@dataclass
class FilterResult:
    """Verdict for one candidate memory. `blocked` is the gate decision; the rest is
    telemetry so a run can log *why* something was blocked (never silently)."""
    blocked: bool
    reason: str
    source: str | None          # "patch" | "test_patch" | None
    span_len: int               # longest verbatim shared-token span found
    threshold: int
    matched_excerpt: str        # the offending span, detokenized + truncated (logging)


def _excerpt(candidate_tokens: list[str], start: int, length: int, limit: int = 200) -> str:
    span = candidate_tokens[start:start + length]
    text = " ".join(span)
    return text[:limit] + ("…" if len(text) > limit else "")


def check_memory(
    content: str,
    patch: str = "",
    test_patch: str = "",
    *,
    threshold: int = DEFAULT_NGRAM_THRESHOLD,
) -> FilterResult:
    """Decide whether `content` may be stored. Blocks iff it shares a contiguous
    verbatim token span of length >= `threshold` with the changed code of `patch` or
    `test_patch`."""
    cand = tokenize(content)
    best = (0, 0, None)  # (span_len, start, source)
    for source, diff in (("patch", patch), ("test_patch", test_patch)):
        ref = tokenize(extract_diff_code(diff))
        span_len, start = longest_shared_span(cand, ref)
        if span_len > best[0]:
            best = (span_len, start, source)

    span_len, start, source = best
    blocked = span_len >= threshold
    if blocked:
        reason = (f"verbatim {source} copy: {span_len} contiguous tokens "
                  f">= threshold {threshold}")
        excerpt = _excerpt(cand, start, span_len)
    else:
        reason = f"clear: longest verbatim span {span_len} < threshold {threshold}"
        excerpt = ""
        source = None
    return FilterResult(
        blocked=blocked, reason=reason, source=source,
        span_len=span_len, threshold=threshold, matched_excerpt=excerpt,
    )


if __name__ == "__main__":  # tiny CLI: echo a verdict for ad-hoc checks
    import json
    import sys

    data = json.load(sys.stdin)
    r = check_memory(
        data.get("content", ""),
        data.get("patch", ""),
        data.get("test_patch", ""),
        threshold=int(data.get("threshold", DEFAULT_NGRAM_THRESHOLD)),
    )
    json.dump(r.__dict__, sys.stdout, indent=2)
    sys.stdout.write("\n")
