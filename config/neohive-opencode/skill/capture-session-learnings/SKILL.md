---
name: capture-session-learnings
description: Use at the end of any session, or when the user says "save what I learned", "capture this session", "extract learnings", "revise vector memory", or "/revise-vector-memory" (legacy alias). Scans the conversation transcript, extracts corrections, conventions, decisions, and gotchas worth persisting, deduplicates against NeoHive, stores up to 5 new memories, and audits the global AGENTS.md / instructions for memory-usage gaps.
---

You capture valuable knowledge from the current session into NeoHive semantic memory so future sessions inherit it. This skill is the end-of-session counterpart to `load-context`.

## Your Job

Analyze the conversation transcript of this session. Extract learnings worth persisting across sessions and store them in the vector memory system via the NeoHive `memory_*` MCP tools (typically prefixed `neohive-<server>_`). Also check whether the memory usage instructions in `~/.config/opencode/instructions/neohive.md` need improvement.

## Phase 1 — Extract Candidate Learnings

Scan the full session transcript for these categories of knowledge:

1. **User corrections**: The user said "no, that's wrong" or corrected your approach → `error_pattern` (importance 7-8)
2. **New conventions**: The user established a rule like "always use X" or "never do Y" → `convention` or `directive` (importance 8-9)
3. **Architectural decisions**: A design choice was made with rationale → `decision` (importance 6-8)
4. **Non-obvious discoveries**: A gotcha, workaround, or surprising behavior was found → `insight` (importance 6-7)
5. **Bug patterns**: A tricky bug was debugged and solved → `error_pattern` (importance 7)
6. **Idiomatic patterns**: A preferred way of doing something was established → `idiom` (importance 6-7)
7. **Syntax/API learnings**: New syntax or API usage was clarified → `syntax_rule` or `stdlib_reference` (importance 7-9)

**Be selective.** Only extract knowledge that would be valuable in a *future* session on a *different* day. Skip:
- Session-specific file paths or temporary state
- Trivial facts anyone would know
- Information that's already in official documentation and easy to look up
- Debugging steps that led nowhere

For each candidate, formulate a clear, self-contained description. It should make sense to someone (or an LLM) reading it *without* the context of this session.

## Phase 2 — Deduplicate Against Existing Memory

For **each** candidate learning:

1. Call `memory_recall` with a semantic query that describes the learning (use `memory_recall` here, not `memory_context` — deduplication needs a narrow single-type search against the specific learning, not a broad context load)
2. Examine the results:
   - **Strong match found** (the memory system already knows this) → **Skip**. The system is working correctly.
   - **Weak/partial match** (related but not the same insight) → **Store** with a reference to the related memory. This adds a new "angle" that may fire on different queries.
   - **No match** (the memory system had no idea) → **Store**. This is a genuine knowledge gap.

This is the critical step. The deduplication heuristic is: *if the memory system already had this insight, you should have found it during the session. If you didn't find it, either the insight is new, or the existing memory's embedding doesn't cover this query angle — either way, storing a new entry is correct.*

## Phase 3 — Store New Memories

For each learning that passed deduplication, call `memory_store` with:
- `content`: Clear, self-contained description of the learning
- `type`: The appropriate type from Phase 1
- `tags`: 3-6 relevant tags for filtering
- `importance`: As suggested in Phase 1

(In opencode, `memory_store` automatically routes to the project's Knowledge hive — pass an explicit `hive` parameter only if you have a specific reason to write elsewhere.)

## Phase 4 — Self-Audit Memory Usage Instructions

Check whether this session revealed a gap in how you use the memory system:

- Did you forget to call `memory_context` at session start?
- Did you fail to recall before working on an unfamiliar topic?
- Did you use the wrong memory type or importance level?
- Did you store something it shouldn't have, or fail to store something it should have?
- Is there a usage pattern that should be documented but isn't?

If you identify a gap, read `~/.config/opencode/instructions/neohive.md` and check the relevant section. If the instructions don't already cover this gap, update that section with a specific, actionable instruction. Use `edit` — do not rewrite the entire file.

## Output

After completing all phases, output a brief summary:

```
Memory revision complete:
- Candidates found: N
- Already known (skipped): N
- New memories stored: N (list IDs)
- neohive.md updated: yes/no (what changed)
```

## Important Rules

- **Do not store more than 5 memories per session.** If you find more than 5 candidates, prioritize by importance and novelty.
- **Do not store memories about the memory system itself** (that goes in `~/.config/opencode/instructions/neohive.md` via Phase 4).
- **Be conservative.** When in doubt, don't store. A clean memory system with high-signal entries outperforms a noisy one.
- **Each memory must be self-contained.** Someone reading it in 6 months with no context should understand it.
- **Prefer specificity over generality.** "Use `Promise.allSettled` instead of `Promise.all` when partial failures are acceptable in our batch processor" is better than "Handle promise errors properly".
