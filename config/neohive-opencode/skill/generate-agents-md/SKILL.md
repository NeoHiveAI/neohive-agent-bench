---
name: generate-agents-md
description: Use when the user says "generate my NeoHive AGENTS.md", "regenerate the topology", "re-survey my hives", or after adding/removing/renaming hives. Surveys connected hives (list_hives + memory_stats + sampled memory_recall probes) and writes a project-specific NeoHive topology block into `./AGENTS.md`. Marker-bounded write that's safe to re-run. Also invoked as a sub-step of the `getting-started` skill.
---

# Generate Project-Specific NeoHive AGENTS.md Block

You are surveying the user's connected NeoHive hives and writing a **project-specific** topology block into `./AGENTS.md`. This block tells opencode — at session start, before `memory_context` is even called — which hives exist, what each one holds, and where new writes should land. Without this block, the generic rules in `~/.config/opencode/instructions/neohive.md` are running blind.

**Three non-negotiable rules:**

1. **Read-only until the user confirms the diff.** Cartography, synthesis, and the proposed table are all preview-only. Do NOT write `./AGENTS.md` until the user explicitly approves the diff.
2. **Marker-bounded writes only.** Generated content always lives between `<!-- BEGIN neohive-managed v=1 -->` and `<!-- END neohive-managed v=1 -->`. Never modify content outside the markers.
3. **Evidence-grounded synthesis.** Every column of the topology table must be traceable to `list_hives`, `memory_stats`, or sampled `memory_recall` results. When inference is uncertain, mark the cell `(verify)` rather than guess confidently.

## Three internal stages

The skill runs as three stages. Stages B and C each have one user gate; Stage A is silent unless it errors.

- **Stage A — Cartography** — read-only data gathering against the NeoHive MCP.
- **Stage B — Synthesis** — produce a draft topology block; user reviews the table.
- **Stage C — Write** — show diff, user confirms, write to `./AGENTS.md`.

## Stage A — Cartography (silent, no user input)

```
A.1  list_hives                         → name, UUID, type, description per hive
A.2  memory_stats                       → type-distribution per hive (one call, all hives)
A.3  for each hive:
       probes = derive_probes(hive.name, hive.description)
       memory_recall(hive=<uuid>, queries=probes, limit=10)
       → 5–10 sample memories per hive
```

### Probe derivation

For each hive, generate 2–3 probe query strings:

1. Tokenize `name` and `description`. Drop stopwords and MCP boilerplate ("stores", "hive", "default", "main").
2. Combine remaining content words with intent suffixes: `"<token> convention"`, `"<token> directive"`, `"<token> example pattern"`.
3. If `description` is missing or generic, fall back to: `"convention"`, `"directive"`, `"insight"`, `"example pattern"`.
4. Cap probes at 3 per hive.

### Stage A failure handling

| Failure | Response |
|---|---|
| `list_hives` returns empty / errors | Abort. Tell the user: "Cannot generate topology — no hives reachable. Confirm Phase 1 of `getting-started` passed before re-running." Do NOT write anything. |
| `memory_stats` unavailable | Continue. Mark every hive's `Write to it?` cell `(verify)`. Surface a one-line warning at the Stage B review gate. |
| `memory_recall` returns nothing for a hive | Re-attempt with the generic fallback probes. If still empty, set `What it holds` to the hive's `description` verbatim and append `(no sampled memories)`. |

## Stage B — Synthesis

Produce one row per hive with these columns:

| Column | Source | Inference rule |
|---|---|---|
| Hive (UUID) | `list_hives` | verbatim |
| Name | `list_hives` | verbatim |
| Type | `list_hives` | verbatim |
| Embedding model | `list_hives.description` + heuristic | code-tuned (e.g. `jinaai/jina-embeddings-v2-base-code`) if hive is type `repo` or description mentions "code"/"indexed". Prose-tuned (e.g. `nomic-ai/nomic-embed-text-v1.5`) if type is `knowledge`/`markdown` or description mentions "prose"/"curated". When uncertain → `(verify)`. |
| What it holds | sampled memories from A.3 + type-distribution from A.2 | 1–2 sentences grounded in real content. Cite the type composition (e.g., "Mostly `convention` and `insight` entries — curated knowledge"). |
| Write to it? | type-distribution from A.2 | Heavy `example_pattern` / `syntax_rule` / `stdlib_reference` → "**NO** — auto-managed; manual writes risk being overwritten by indexing." Mixed `convention` / `directive` / `insight` → "**YES** — default write target." Small + `directive`-heavy → "**RARELY** — only for durable, language-level conventions. Ask the user before writing here." |

### Default write target

Pick the hive with the largest write-safe (`YES`) memory count. Tie-break alphabetically by name. If no hive is write-safe, set the default to `<none — ask before any write>` and emit a warning row.

### Stage B review gate

Render the proposed table to the terminal as a markdown table (so the user can read it). Then ask via the `question` tool:

- **Header:** "Topology"
- **Question:** "Topology table looks right?"
- Options: `Looks good — proceed to write`, `Edit a row`, `Re-sample with different probes`, `Cancel`

`Edit a row` → ask which row + which column → accept free-text override → re-render and re-confirm.
`Re-sample with different probes` → ask user for probe terms → re-run A.3 only → re-synthesize.
`Cancel` → exit cleanly. No file writes.

## Stage C — Write

### Pre-write git safety check

If the target file is in a git worktree:

```bash
if git -C "$(dirname ./AGENTS.md)" rev-parse --is-inside-work-tree 2>/dev/null; then
  if [ -n "$(git status --porcelain -- ./AGENTS.md)" ]; then
    # warn + question to confirm or cancel
  fi
fi
```

Warning text: "`AGENTS.md` has uncommitted changes — recommend committing first so this skill's diff is easy to review separately. Continue?" Do not auto-commit. If `rev-parse` fails (not a git repo), skip the check silently.

### Write-behavior matrix

| Existing `./AGENTS.md` state | Action |
|---|---|
| File absent | Create `./AGENTS.md` containing only the marker block. |
| File present with `<!-- BEGIN neohive-managed v=N -->` markers | Replace content between markers; preserve everything outside. Update `v=N` to current version (`v=1`). |
| File present without markers | Append the marker block to end-of-file. Preserves user-authored content at the top. |

**v1 upgrade behavior:** any existing block, regardless of `v=`, is replaced wholesale. v1 does not implement format-aware migration.

### Diff-and-confirm gate

1. Compute proposed file content in memory.
2. Allocate temp file via `mktemp` in `$TMPDIR` (NOT in the project worktree). Register a cleanup trap so the temp file is removed on any exit path including SIGINT.
3. Run `diff -u AGENTS.md "$tmp"` (treat absent `AGENTS.md` as `/dev/null`); print the unified diff to terminal.
4. Ask via the `question` tool:
   - **Header:** "Write block"
   - **Question:** "Write this block to ./AGENTS.md?"
   - Options: `Write`, `Edit block first`, `Cancel`
5. `Write` → atomic `mv "$tmp" ./AGENTS.md`. `Edit block first` → spawn `$EDITOR` on `$tmp` → re-show diff → re-confirm. `Cancel` → cleanup trap fires, exit.

## Generated content template

Always wrap in markers. Use these substitution variables; fill them from Stage B output.

```markdown
<!-- BEGIN neohive-managed v=1 -->
<!-- Generated by the neohive:generate-agents-md skill on {{DATE}}. Re-run to refresh. -->

## NeoHive Cognitive Memory — Project Topology

Generic NeoHive tool-usage rules are loaded from `~/.config/opencode/instructions/neohive.md`.
This block adds the **project-specific** topology and routing that determine
WHICH hive serves which query, and where new writes should land.

### Hive Topology

| Hive (UUID) | Name | Type | Embedding model | What it holds | Write to it? |
|---|---|---|---|---|---|
{{ROWS}}

**Why query phrasing matters here.** {{QUERY_PHRASING_GUIDANCE}}

**Interpreting `[hive: <uuid>]` in recall results.** {{HIVE_PROVENANCE_GUIDE}}

### Session Start — Non-Negotiable (Project-Specific)

1. `memory_context` is your FIRST action — see `~/.config/opencode/instructions/neohive.md`.
2. Confirm the topology above has not drifted: call `list_hives` once per session.
   If a hive is added / removed / renamed, re-run the `generate-agents-md` skill.
3. Follow up with a targeted `memory_recall` for this project's domain. Suggested seeds:
{{DOMAIN_RECALL_SEEDS}}

### What Goes Where: Cognitive Memory vs AGENTS.md

| Store in **Cognitive Memory** | Store in **AGENTS.md** |
|---|---|
{{ROUTING_TABLE}}

### Hive routing for writes

Writes default to **{{DEFAULT_WRITE_HIVE}}** — {{DEFAULT_WRITE_RATIONALE}}.
**Do not pass an explicit `hive` parameter to `memory_store` unless you have a
specific reason.** When you do, write one sentence in the memory body explaining why.

{{ADDITIONAL_WRITE_HIVES_DISAMBIGUATION}}

<!-- END neohive-managed v=1 -->
```

### Substitution variables

| Variable | Format |
|---|---|
| `{{DATE}}` | `YYYY-MM-DD` |
| `{{ROWS}}` | One markdown table row per hive (Stage B output). |
| `{{QUERY_PHRASING_GUIDANCE}}` | One paragraph. Mention code-token queries iff any code-tuned hive is present; mention affirmative-statement queries iff any prose-tuned hive is present; recommend both styles via `queries` parameter when both. |
| `{{HIVE_PROVENANCE_GUIDE}}` | One paragraph. Per-hive 1-liner mapping name → typical content profile. |
| `{{DOMAIN_RECALL_SEEDS}}` | 3–5 example query strings as a markdown bullet list, scoped to the project's domain (synthesized from sampled content). |
| `{{ROUTING_TABLE}}` | 2-column markdown table; structurally same as the reference; row contents reference user's actual hive names. **N=1 case:** still emit both columns. |
| `{{DEFAULT_WRITE_HIVE}}` | Hive name in backticks. |
| `{{DEFAULT_WRITE_RATIONALE}}` | One sentence; cite why this hive was chosen. |
| `{{ADDITIONAL_WRITE_HIVES_DISAMBIGUATION}}` | Bulleted disambiguation rules; only emit when ≥2 hives are write-safe. Empty otherwise. |

## End-of-run summary

Print a single line:

```
generate-agents-md: 3 hives mapped, default write target: Knowledge,
written to ./AGENTS.md (block lines 42-87, +35 lines vs previous version).
```

When replacing an existing block, additionally report **only changed rows**:

```
  Topology changes:
    + added hive: StarlangLearnings (markdown, prose-tuned, RARELY write)
    ~ updated hive: patterns ("What it holds" updated; sampled count grew 12 → 38)
```

If nothing changed: `Topology changes: none — block already up to date.`

## Common mistakes

| Mistake | Fix |
|---|---|
| Writing the file before Stage C diff confirmation | Cartography and synthesis are read-only. The first write is after the user picks `Write` at the diff gate. |
| Modifying content outside the marker block | Markers are a hard boundary. Anything outside `<!-- BEGIN ... -->` / `<!-- END ... -->` is user-owned. |
| Confidently guessing embedding model from hive name | When uncertain, mark `(verify)`. |
| Inferring write-policy from hive description alone | `memory_stats` is the load-bearing signal. If it's unavailable, mark `(verify)` everywhere. |
| Putting the temp file in the project worktree | Use `mktemp` in `$TMPDIR`. Otherwise the temp file shows up in `git status` mid-run. |

## Important rules

- **NEVER write `./AGENTS.md` before the diff-and-confirm gate completes.**
- **NEVER touch content outside the marker block.** Idempotent re-runs depend on this.
- **NEVER skip the synthesis review gate.** The table is the highest-leverage artifact this skill produces; if it's wrong, everything downstream is wrong.
- **NEVER auto-commit.** The user owns commit timing.
- **If Stage A errors, abort and surface plainly.** Do not write a partial / placeholder block.
