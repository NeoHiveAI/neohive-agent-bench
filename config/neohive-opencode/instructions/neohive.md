# NeoHive Cognitive Memory

You have access to a persistent semantic memory system via MCP tools whose names usually look like `*_memory_context`, `*_memory_recall`, `*_memory_store`, `*_memory_forget`, `*_list_hives`, and `*_memory_stats` (the exact prefix is set by the user's MCP server name — e.g. `neohive-gist_memory_recall`). The hives connected to this session may contain durable team knowledge **and indexed source code** (typically embedded with a code-tuned model). Treat the hives as a first-class navigation surface, not a side-channel. **Use them actively, not passively.**

## Session Start — ALWAYS Do This First

Call `memory_context` with a description of your current task BEFORE doing any work. This loads relevant directives, conventions, and task-specific knowledge. Describe the task in affirmative form with specific domain terms:

- GOOD: `"implementing authentication middleware for Express gateway"`
- BAD: `"what do we know about auth?"`

If the task involves a specific domain (e.g., starlang rules, dashboard tiles), call `memory_context` again with a domain-specific description to pre-load relevant context.

## Codebase Exploration — Prefer `memory_recall` Over File Traversal

If a hive contains the codebase you're working in (the `list_hives` output names a `repo`-typed hive, or `memory_context` returned indexed code snippets), call `memory_recall` BEFORE doing broad file exploration with `glob`, `grep`, or `read`. The indexed embedding is almost always faster and uses less context than walking the tree:

- Frame the query as what you'd say to a teammate: `"how does the sync engine handle git clone credentials"` not `"find git clone code"`.
- Use `memory_recall` to locate the relevant files, then use `read` for the precise line numbers you need to edit.
- Fall back to `glob`/`grep` only when you need an exact symbol that semantic search misses, or for files outside the index (e.g. brand-new files in your working tree).

This applies for the entire session, not just at start: every time you'd reach for "let me search the codebase for X," try `memory_recall` first. The MCP server itself surfaces a self-reinforcing hint on every `memory_recall` / `memory_context` response (result count, top score, latency); when you see that hint, take it as a cue to keep using semantic recall instead of switching to filesystem tools.

This config also ships an opencode plugin (`plugin/neohive.ts`) that nudges via `tool.execute.before` whenever `glob` or `grep` fire inside a project with a NeoHive MCP server configured in `.mcp.json`. By default the hook lets the tool run and emits an informational reminder via the bus event channel; set `NEOHIVE_PRETOOL_STRICT=1` to make it throw and force the model to retry with `memory_recall` instead, or `NEOHIVE_PRETOOL_DISABLED=1` to opt out entirely. To suppress the in-response hint emitted by `memory_recall` / `memory_context`, set `NEOHIVE_MCP_HINTS=0`.

## Delegating to Subagents — Prefer `explore-neohive` Over Built-In `explore`

This config bundles a subagent called **`explore-neohive`** whose system prompt forces semantic recall first. Whenever you would dispatch the built-in `explore` agent for codebase or knowledge exploration in this project, dispatch `explore-neohive` instead via the `task` tool with `subagent_type: explore-neohive`. It is faster, uses less context, and returns ranked snippets with provenance metadata that filesystem tools cannot produce.

Examples of when to pick `explore-neohive`:
- "Where is X defined?" / "How does Y work?" / "What's the convention for Z?"
- Architecture questions, decision archaeology, locating files by concept rather than by exact symbol.
- Open-ended research where you don't yet know the precise file paths.

Stick with the built-in `explore` only when:
- The project has no NeoHive instance reachable (no `*memory_recall*` MCP tools available), or
- You need an exact-symbol search that semantic recall has already missed in this session.

**Other subagents (build, general, etc.):** The MCP tool list is inherited by subagents you spawn, but **the directives in this rules file are not.** When the work touches an indexed area of the codebase, include in the subagent's prompt:

> "This project has a NeoHive instance with indexed code/knowledge. Before file exploration, call `memory_recall` (or `memory_context` if you're starting fresh) with an affirmative description of what you're looking for. Use glob/grep/read only for precise line numbers or files the index doesn't cover."

## Discovering Hives

Call `list_hives` to see what hives are available. Each hive has a description explaining what it stores (code, knowledge, rules, etc.). Use this to decide which hive to target for writes.

## Reading — memory_recall & memory_context

When no `hive` parameter is specified, reads search across ALL hives using cross-hive RRF fusion — the most relevant results from any hive are returned. You usually want this behavior.

Query formulation matters:
- Write **affirmative statements**, not questions: `"error handling in async batch processing"` not `"How do we handle errors?"`
- Include **specific domain terms** that would appear in stored knowledge: `"sqlite-vec F32_BLOB column type"` not `"vector database column"`
- Use the **types parameter** to narrow results: `types: ["directive", "convention"]` for rules, `types: ["error_pattern", "insight"]` for gotchas
- For important retrievals, pass **multiple queries** via the `queries` parameter — 2-4 different phrasings of the same need

Call `memory_recall` before working on unfamiliar topics or when you need specific knowledge. If results are weak, reformulate and retry with synonyms or broader/narrower scope.

**Key recall checkpoints** (not just session start — recall at each of these moments):
- Before drafting a PR body or commit message → recall PR template conventions
- Before scoping rule work → recall cross-language scope for the rule key
- Before creating branches → recall branch naming conventions
- Before any JIRA transition → recall ticket lifecycle conventions
- Before transitioning a PR's review stage (un-drafting, adding/removing review-stage labels like internal→external/RI review) → recall the team's 2-stage review + label-stickiness conventions (and any "approval overrides" rule), since the convention often diverges from the platform's native review state
- Before asserting a PR is "ready to merge", "complete", or "blocked on X" in a report or summary (e.g. daily PR triage, weekly status, hand-off notes) → recall the team's "what counts as ready" criteria. Platform-level signals like `reviewDecision: APPROVED` can mean "internal stage only" in teams with multi-stage review; a PR with a `ready-for-ri-review`-style label has been PROMOTED to that stage but is NOT necessarily externally approved. Always cross-check the actual reviewer logins against the team's known external-reviewer list (typically retrievable via `memory_recall` with queries like `"<repo> external reviewer convention"`)
- Before submitting a code review you authored (choosing the APPROVE / COMMENT / REQUEST_CHANGES event) → recall the team's review-event convention, since which event counts as a "non-blocking sign-off" can diverge from the generic instinct that "COMMENT = feedback without blocking" (e.g. some internal-review processes require APPROVE, not COMMENT, to advance a PR)
- Before designing a new framework taint source/sink predicate (or any rule whose match shape depends on cross-file type resolution, annotation reading, or call→method-decl resolution) → recall engine-limit memories for the target language with queries like "engine cross-file resolution limit", "type resolution gap", "wrapper unwrapping not modelled". Prior tickets often leave specific resolution gaps documented; finding them BEFORE writing the predicate saves an iteration cycle of committing dead code that doesn't propagate end-to-end.
- Before debugging a CI check failure, OR before manually editing what looks like an auto-generated artifact (CSVs, markers.txt, lockfiles, generated docs, etc.) → recall the file's origin AND any required-companion-file conventions with queries like `"<check-name> CI failure root cause"`, `"<filename> auto-generated regenerated by"`, `"new rule key required files registration"`. Most CI checks that fail "for the same code" across many branches are gated on a missing companion file (e.g., a per-key doc, a registration entry, a manifest update) — finding the convention up front avoids investigating script internals or manually editing a generated artifact that will be overwritten on the next push.
- Before asserting a DSL-syntax or project-convention claim in a code review (e.g. "this empty marker block is invalid", "this predicate name violates convention", "this pattern is the wrong shape") → recall with queries like `"<syntactic element> valid usage convention"`, `"<predicate name> semantics"`. Code reviews are public, and rolling back a wrong syntax claim is awkward; the project's DSL may have idioms that look unusual but are correct (e.g. `EXPORT PRED Foo []` with empty markers is valid when consumed by a taint-template wrapper). `memory_recall` against the indexed code/knowledge is far better evidence than Grep-pattern frequency in the tree.

## Writing — memory_store

The user's project may have a default write hive configured in `AGENTS.md` or via an explicit hive parameter. When in doubt, use `list_hives` to find the right hive — the Knowledge hive is a reasonable default for cross-project insights.

Call `memory_store` when:
- The user corrects you or says "no, we do X instead"
- A new convention or rule is established
- You discover a non-obvious gotcha or insight
- An architectural decision is made with rationale
- A tricky bug is debugged and solved

Write content as a **self-contained statement** that someone with no context could understand in 6 months. Include specific terms that future searches would use to find this knowledge.

**Store at the moment of insight, not only at session end.** When a correction, convention, decision, or non-obvious discovery surfaces mid-session, capture it right then — long sessions lose detail if every store is deferred to a final batch. Before storing, run a quick `memory_recall` to dedupe: if a strong match already exists, skip; if only a partial/related match exists, store anyway with a reference to the related entry (a new query angle is worth it). End-of-session extraction (`capture-session-learnings`) is a safety net for what you missed, not the primary capture mechanism.

Memory types: `directive` (rules/musts), `convention` (practices/preferences), `decision` (trade-offs with rationale), `insight` (gotchas/discoveries), `error_pattern` (bugs/pitfalls), `syntax_rule`, `semantic_rule`, `example_pattern`, `idiom`, `narrative`, `session_summary`, `consolidated`, `stdlib_reference`.

## Forgetting — memory_forget

Call `memory_forget` when knowledge becomes outdated or is superseded by a correction. Always provide a `reason` and `superseded_by` ID if a replacement was stored.

## Bundled Skills

This config installs these skills under `~/.config/opencode/skill/neohive/`. They are invoked by the model when your request matches their description (no slash-command prefix). You can also reference them by name via the `skill` tool:

- `getting-started` — first-run setup (verify MCP, configure auth, generate topology block in `AGENTS.md`, migrate memory, enable helpers). Run once per machine.
- `load-context` — pre-load relevant memory for the current task via `memory_context`. Run at the start of every session.
- `generate-agents-md` — survey connected hives and write a project-specific topology block into `./AGENTS.md`. Re-run when hives are added, removed, or renamed.
- `capture-session-learnings` — end-of-session extraction of corrections, conventions, decisions, and insights into NeoHive.
- `migrate-memory` — scan local `AGENTS.md` / `CLAUDE.md` / `.claude/rules` / `.opencode/instructions` and import project-scoped entries into a hive.
- `design-codebase-docs` — Socratic design of a documentation standard, save to NeoHive, validate with sample pages.
- `enable-smart-prompts` — install a smarter prompt-context hook that rewrites prompts with a small model before querying NeoHive.
