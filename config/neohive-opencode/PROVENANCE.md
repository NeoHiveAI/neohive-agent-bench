# Arm-B NeoHive opencode setup — provenance & faithfulness

This directory is the **faithful end-user NeoHive setup for opencode**, committed in
full so the treatment arm is transparent and critiquable (no hidden retrieval
engineering). Arm B mounts these into the container's `/root/.config/opencode/`; Arm
A gets none of them.

## What it is and where it came from

The official NeoHive Claude Code plugin (`NeoHiveAI/NeoHiveClaude`,
`plugins/neohive/`) was ported to opencode by the team; that port lives on this
machine at `~/.config/opencode/`. These files are a pinned copy of that port:

| File | Source | Role |
|---|---|---|
| `instructions/neohive.md` | `~/.config/opencode/instructions/neohive.md` | The usage rules auto-loaded into every session (call `memory_context` first; prefer `memory_recall` over glob/grep). opencode adaptation of `NeoHiveClaude/plugins/neohive/rules/neohive.md`. |
| `plugin/neohive.ts` | `~/.config/opencode/plugin/neohive.ts` | `tool.execute.before` hook nudging the model from glob/grep/list toward `memory_recall`. opencode port of the `pretool-tree-walker.sh` hook. |
| `agents/explore-neohive.md` | `~/.config/opencode/agents/explore-neohive.md` | Subagent that forces semantic recall first (vs the built-in explore agent). |
| `skill/*` | `~/.config/opencode/skill/neohive/*` | The NeoHive skills (getting-started, load-context, capture-session-learnings, generate-agents-md, migrate-memory, design-codebase-docs, enable-smart-prompts). |
| `plugin/neohive-smart-prompts.ts` | **generated** by the `enable-smart-prompts` skill template, adapted here | `chat.message` hook: rewrites each prompt → `memory_recall` → filters → injects context. The opt-in "smart-recall" layer. |

Plugin SDK at copy time: `@opencode-ai/plugin@1.15.10` (type-only import, erased at
runtime). opencode binary pinned at 1.17.10 (see `fetch_opencode.sh`).

## "Every feature enabled"

Per the benchmark decision, Arm B enables the **maximal** end-user configuration:
- usage rules (always-on instructions),
- the glob/grep→recall nudge (`neohive.ts`, default soft mode),
- the `explore-neohive` subagent,
- **smart-recall auto-context** (`neohive-smart-prompts.ts`) — the `enable-smart-prompts`
  opt-in, which rewrites + injects `memory_recall` results on every qualifying prompt.

Both plugins are registered in `config/opencode-arm-b.json`'s `plugin` array.

## Adaptations for this benchmark (vs the stock artifacts)

Only `neohive-smart-prompts.ts` was modified, and only for our hosted target:
- **Auth:** `callMemoryRecall` sends Cloudflare Access service-token headers
  (`CF-Access-Client-Id` / `CF-Access-Client-Secret`) + an explicit `User-Agent`
  (the CF WAF blocks default UAs — Error 1010). Bearer `NEOHIVE_TOKEN` still honored.
- **Rewriter model:** an OpenRouter model (the container has `OPENROUTER_API_KEY`),
  overridable via `NEOHIVE_SMART_MODEL`.
- **Rewrite/filter transport:** the stock template runs the rewrite + filter steps as
  a nested `opencode run` (a second/third full opencode agent per prompt). Under x86
  emulation that stacks multiple ~170MB processes and OOM-kills the solve (exit 137),
  so here they're a **direct OpenRouter chat completion** instead — same prompts, same
  model, same injected output, a fraction of the memory.

The rules, the nudge plugin, the subagent, and the skills are **verbatim** copies.
