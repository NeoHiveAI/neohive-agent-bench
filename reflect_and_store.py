#!/usr/bin/env python3
"""HIVE-335 — reflect-and-store write path (host-side, filter-gated).

After each solved task the agent's experience must be written back to the persistent
experience pool, so later rounds retrieve accumulated learnings (current Arm B is
read-only; without this the hive never grows and the experiment measures nothing).

Why host-side rather than letting the agent call `memory_store` itself: the write
MUST pass the solution-copy filter (HIVE-336), and the filter cannot run inside the
NeoHive server (an unchanged dependency). So the controlled path is: reflect on the
finished task -> gate every candidate through the filter -> write survivors via the
same MCP transport the smart-prompts hook already uses. The agent's own unfiltered
`memory_store` is disabled in compounding mode (see run_opencode wiring).

The reflector (transcript -> candidate learnings) is injectable so the gating logic is
unit-testable without any model call; the default reflector asks the fixed helper model
(GLM-4.6, same as the smart-prompts rewriter, so it is not a per-arm confound) for a
few *transferable* lessons — explicitly not the diff. The filter is the backstop that
blocks any verbatim solution/test span that slips through.

Stdlib only (the default reflector uses urllib against OpenRouter).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from solution_copy_filter import DEFAULT_NGRAM_THRESHOLD, check_memory

DEFAULT_REFLECT_MODEL = os.environ.get("NEOHIVE_REFLECT_MODEL", "z-ai/glm-4.6")
_ALLOWED_TYPES = {
    "insight", "convention", "decision", "error_pattern", "idiom",
    "directive", "semantic_rule", "syntax_rule", "example_pattern", "stdlib_reference",
}


@dataclass
class ReflectionReport:
    instance_id: str
    candidates: int = 0
    stored: int = 0
    blocked: int = 0
    stored_ids: list = field(default_factory=list)
    blocked_details: list = field(default_factory=list)  # [{content_head, reason, source, span_len}]
    stored_summaries: list = field(default_factory=list)  # [{type, content_head}]
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "instance_id": self.instance_id, "candidates": self.candidates,
            "stored": self.stored, "blocked": self.blocked, "stored_ids": self.stored_ids,
            "blocked_details": self.blocked_details, "stored_summaries": self.stored_summaries,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Prompt + candidate parsing (pure, testable)
# --------------------------------------------------------------------------- #

REFLECT_SYSTEM = (
    "You extract durable, TRANSFERABLE engineering lessons from a coding session so a "
    "future agent solving a DIFFERENT issue in the same repository benefits. Output must "
    "generalize. NEVER include the specific diff, patch text, or test code — describe the "
    "approach, the failure mode, the relevant module/API, and how to reason about it. "
    "Return ONLY a JSON array of objects {\"content\": str, \"type\": str, \"importance\": int, "
    "\"tags\": [str]}. type is one of: insight, convention, decision, error_pattern, idiom. "
    "importance 1-10. Return at most {n} items; fewer if little was learned; [] if nothing."
)


def build_reflection_prompt(problem_statement: str, transcript_excerpt: str,
                            produced_patch: str, status: str, *, n: int = 3) -> list[dict]:
    """Chat messages for the reflector. The produced patch is shown for grounding, but
    the system prompt forbids copying it; the filter enforces that regardless."""
    user = (
        f"Repository issue (problem statement):\n{problem_statement[:4000]}\n\n"
        f"Run status: {status}\n\n"
        f"Abbreviated agent transcript:\n{transcript_excerpt[:6000]}\n\n"
        f"The change the agent produced (for context only — DO NOT reproduce it):\n"
        f"{produced_patch[:3000]}\n\n"
        f"Extract up to {n} transferable lessons as the specified JSON array."
    )
    return [
        {"role": "system", "content": REFLECT_SYSTEM.replace("{n}", str(n))},
        {"role": "user", "content": user},
    ]


def parse_candidates(text: str) -> list[dict]:
    """Parse the model's JSON array of candidates, tolerating code fences / preamble.
    Normalizes type + importance; drops malformed items."""
    if not text:
        return []
    s = text.strip()
    # pull the first JSON array out of the response
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        raw = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        mtype = str(item.get("type", "insight")).strip().lower()
        if mtype not in _ALLOWED_TYPES:
            mtype = "insight"
        try:
            importance = int(item.get("importance", 6))
        except (TypeError, ValueError):
            importance = 6
        importance = max(1, min(10, importance))
        tags = [str(t) for t in item.get("tags", []) if str(t).strip()][:6]
        out.append({"content": content, "type": mtype, "importance": importance, "tags": tags})
    return out


# --------------------------------------------------------------------------- #
# Default reflector (OpenRouter / GLM-4.6) — networked
# --------------------------------------------------------------------------- #

def openrouter_reflector(model: str = DEFAULT_REFLECT_MODEL, *, n: int = 3, timeout: int = 60):
    """Return a reflector callable(problem_statement, transcript, patch, status)->[cand].
    Uses OPENROUTER_API_KEY; raises RuntimeError if the key is missing when called."""
    def _reflect(problem_statement: str, transcript: str, patch: str, status: str) -> list[dict]:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set — cannot run the default reflector")
        messages = build_reflection_prompt(problem_statement, transcript, patch, status, n=n)
        body = json.dumps({"model": model, "messages": messages, "temperature": 0.2}).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "User-Agent": "neohive-agent-bench/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return parse_candidates(text)
    return _reflect


# --------------------------------------------------------------------------- #
# The gated write path
# --------------------------------------------------------------------------- #

def reflect_and_store(
    client,
    instance: dict,
    transcript: str,
    produced_patch: str,
    status: str,
    *,
    reflector=None,
    threshold: int = DEFAULT_NGRAM_THRESHOLD,
    dry_run: bool = False,
) -> ReflectionReport:
    """Reflect on a finished task and store the surviving learnings.

    `instance` is the SWE-bench row (needs instance_id, problem_statement, and the gold
    `patch`/`test_patch` used ONLY by the filter). `client` is a NeoHiveClient (or a stub
    exposing store_memory). `reflector(problem_statement, transcript, patch, status)`
    returns candidate dicts; defaults to the OpenRouter GLM-4.6 reflector."""
    iid = instance.get("instance_id", "?")
    report = ReflectionReport(instance_id=iid)
    reflector = reflector or openrouter_reflector()
    gold_patch = instance.get("patch", "") or ""
    gold_test = instance.get("test_patch", "") or ""

    try:
        candidates = reflector(instance.get("problem_statement", ""), transcript, produced_patch, status)
    except Exception as e:  # noqa: BLE001 — reflection must never abort the run
        report.error = f"reflector failed: {e}"
        return report

    report.candidates = len(candidates)
    for cand in candidates:
        content = cand["content"]
        verdict = check_memory(content, gold_patch, gold_test, threshold=threshold)
        if verdict.blocked:
            report.blocked += 1
            report.blocked_details.append({
                "content_head": content[:120], "reason": verdict.reason,
                "source": verdict.source, "span_len": verdict.span_len,
            })
            continue
        if not dry_run:
            try:
                res = client.store_memory(content, cand["type"], importance=cand["importance"],
                                          tags=cand.get("tags"))
                mid = _extract_stored_id(res)
                if mid is not None:
                    report.stored_ids.append(mid)
            except Exception as e:  # noqa: BLE001
                report.error = f"store failed (partial): {e}"
                break
        report.stored += 1
        report.stored_summaries.append({"type": cand["type"], "content_head": content[:120]})
    return report


def _extract_stored_id(store_result: dict):
    """Best-effort pull of the new memory id from an MCP memory_store result."""
    if not isinstance(store_result, dict):
        return None
    if "id" in store_result:
        return store_result["id"]
    # MCP returns {content:[{type:text, text:"...id: N..."}]}; scan for an id.
    from neohive_rest import NeoHiveClient
    text = NeoHiveClient.mcp_text(store_result)
    import re
    m = re.search(r"\bid[:=]?\s*(\d+)", text)
    return int(m.group(1)) if m else None
