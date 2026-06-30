---
name: enable-smart-prompts
description: Use when the user says "make NeoHive smarter", "rewrite my prompts before searching memory", "enable smart recall", or "give me a smarter hook". Generates a tailored chat.message plugin hook that uses a small model to rewrite the user's prompt into a good `memory_recall` query, decides when lookup is worthwhile, calls NeoHive, and injects only the most relevant results. Augments the default `tool.execute.before` glob/grep nudge that the bundled neohive plugin already installs.
---

# Enable Smart-Recall Prompts

You help the user install a customized `chat.message` plugin hook (the opencode equivalent of UserPromptSubmit) that intercepts their prompt, uses a small model to formulate a good NeoHive query, calls `memory_recall`, and injects relevant results back into the model's context.

This is a **dynamic setup** — every user has a different hive layout, shell, API key location, and tolerance for latency. You walk them through each choice with a strong recommended default, then write the script.

The output is a new file `~/.config/opencode/plugin/neohive-smart-prompts.ts` that augments the bundled `~/.config/opencode/plugin/neohive.ts`. The two can coexist; opencode loads every `.ts` file under `plugin/` that's referenced in `opencode.json`'s `plugin:` array.

## Phase 0 — Check prerequisites

Before asking anything, verify:

```bash
command -v opencode >/dev/null && echo "opencode-cli: OK" || echo "opencode-cli: MISSING"
command -v curl     >/dev/null && echo "curl: OK"          || echo "curl: MISSING"
command -v python3  >/dev/null && echo "python3: OK"       || echo "python3: MISSING"
[ -n "${ANTHROPIC_API_KEY:-}" ] && echo "ANTHROPIC_API_KEY: set" || echo "ANTHROPIC_API_KEY: not set (required for the headless rewriter)"
```

If `opencode-cli` or `python3` is missing, stop and tell the user to install them. If the API key is missing, tell them to export it first and point at https://console.anthropic.com/.

## Phase 1 — Gather configuration

Ask these in sequence, one per `question` tool call (never combine):

### 1. Which hive to target

Call `list_hives`. Ask:

- **Header:** "Target hive"
- **Question:** "Which hive should the hook search on every prompt?"
- Options: populate from `list_hives`, first option `(Recommended) All hives (cross-hive RRF)` — this calls `memory_recall` without a `hive` param.

### 2. Which model drives the query rewriter

- **Header:** "Rewriter model"
- **Question:** "Which model should rewrite your prompt into a NeoHive query and filter results?"
- Options:
  - `anthropic/claude-haiku-4-5 (Recommended) — fast + cheap`
  - `anthropic/claude-sonnet-4-6 — more accurate, slower, ~10x cost`
  - `anthropic/claude-opus-4-7 — overkill, only for very noisy hives`

### 3. Trigger policy

- **Header:** "When to run"
- **Question:** "When should the hook fire?"
- Options:
  - `(Recommended) Every prompt longer than 10 chars — skips short clarifications`
  - `Only when prompt contains a keyword I pick`
  - `Every prompt — no filtering`
  - `Manual only — I'll trigger it via an env flag`

If "keyword": ask for the keyword(s) via a follow-up `question` with a custom-answer option.

### 4. Disable-flag name

- **Header:** "Disable flag"
- **Question:** "What env var should disable the hook when set to 1?"
- Options:
  - `(Recommended) NEOHIVE_SMART_DISABLED`
  - `Custom — I'll type it`

## Phase 2 — Preview the generated plugin

Build a TypeScript plugin module from the template below, substituting the chosen values. Show the final script to the user in a fenced code block. Summarize changes at the top:

```
Generated plugin with:
  • Hive:          <hive-or-all>
  • Model:         <model>
  • Trigger:       <policy>
  • Disable flag:  <env-var>
  • Install path:  ~/.config/opencode/plugin/neohive-smart-prompts.ts
```

Ask one last `question`:

- **Header:** "Install"
- **Question:** "Install this plugin now?"
- Options:
  - `(Recommended) Yes, write it and register it in ~/.config/opencode/opencode.jsonc`
  - `Yes, write it — I'll register it myself`
  - `No — I want to tweak the script first`

If "tweak": ask what they want to change, regenerate, re-preview.
If "no" at any point: stop with "Nothing written."

## Phase 3 — Write the plugin

Create parent directories if needed. Write the file to `~/.config/opencode/plugin/neohive-smart-prompts.ts`. Show:

```
Wrote ~/.config/opencode/plugin/neohive-smart-prompts.ts (N bytes).
```

## Phase 4 — Register in opencode.jsonc (if user opted in)

Edit `~/.config/opencode/opencode.jsonc` (or `opencode.json`) to add `"./plugin/neohive-smart-prompts.ts"` to the top-level `plugin` array. Preserve existing entries.

Use `python3 -c` with `json` to do the edit — never hand-edit JSON via sed. Pattern:

```bash
python3 <<'PY'
import json, pathlib, os
p = pathlib.Path(os.path.expanduser("~/.config/opencode/opencode.jsonc"))
if not p.exists():
    p = pathlib.Path(os.path.expanduser("~/.config/opencode/opencode.json"))
text = p.read_text()
# strip // and /* */ comments before json.loads
import re
stripped = re.sub(r"//.*", "", text)
stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
data = json.loads(stripped) if stripped.strip() else {}
data.setdefault("plugin", [])
entry = "./plugin/neohive-smart-prompts.ts"
if entry not in data["plugin"]:
    data["plugin"].append(entry)
p.write_text(json.dumps(data, indent=2))
print(f"Registered in {p}")
PY
```

## Phase 5 — Verification

Tell the user:

> Restart opencode for the plugin to take effect (opencode does not hot-reload plugins).
>
> Test it: start a new session and ask about something you know is in your hive. You should see a block starting with "NeoHive smart context:" injected before the model responds.
>
> Disable temporarily: `export <DISABLE_FLAG>=1` in your shell.
> Disable permanently: remove the entry from `~/.config/opencode/opencode.jsonc`'s `plugin` array, or delete the script.

## Plugin template

Substitute `__HIVE__`, `__MODEL__`, `__TRIGGER__`, `__KEYWORDS__`, `__DISABLE_FLAG__` before writing.

```typescript
// ~/.config/opencode/plugin/neohive-smart-prompts.ts
// Generated by the neohive:enable-smart-prompts skill.
//
// Adds a smart-context layer on top of NeoHive memory_recall. On every
// user message that passes the trigger policy, this plugin:
//   1. Asks a small model to rewrite the prompt into a good memory_recall query
//   2. Discovers the NeoHive MCP URL from opencode.json / .mcp.json
//   3. Calls memory_recall via HTTP JSON-RPC
//   4. Asks the small model to filter to the 1-3 most relevant results
//   5. Injects the filtered block as an extra text part on the user message

import type { Plugin } from "@opencode-ai/plugin"

const HIVE = "__HIVE__"               // empty string = cross-hive
const MODEL = "__MODEL__"
const TRIGGER = "__TRIGGER__"         // always | min_len | keyword | manual
const KEYWORDS = "__KEYWORDS__"       // pipe-delimited
const DISABLE_FLAG = "__DISABLE_FLAG__"

export default (async ({ $, directory }) => {
  return {
    "chat.message": async (_input, output) => {
      try {
        if (process.env[DISABLE_FLAG] === "1") return

        // Extract user message from the parts array.
        const userText = output.parts
          .filter((p: any) => p.type === "text")
          .map((p: any) => p.text)
          .join("\n")
          .slice(0, 2000)

        // Trigger policy
        if (TRIGGER === "min_len") {
          if (userText.length < 10) return
          if (userText.startsWith("/")) return
        } else if (TRIGGER === "keyword") {
          const re = new RegExp(`(${KEYWORDS})`, "i")
          if (!re.test(userText)) return
        } else if (TRIGGER === "manual") {
          if (process.env.NEOHIVE_SMART_RUN !== "1") return
        }

        if (!process.env.ANTHROPIC_API_KEY) return

        // Step 1: rewrite prompt into query (opencode CLI headless)
        const rewritePrompt = `You are a query rewriter for a semantic memory system. Given a user prompt, produce a single-line search query that would retrieve relevant stored knowledge. Use affirmative domain terms, not questions. Output ONLY the query, no preamble. Prompt: ${userText}`
        const query = (await $`opencode -p ${rewritePrompt} --model ${MODEL}`.text()).trim().slice(0, 400)
        if (!query) return

        // Step 2: discover MCP URL
        const mcpUrl = await discoverNeoHiveUrl(directory)
        if (!mcpUrl) return

        // Step 3: call memory_recall
        const memoriesRaw = await callMemoryRecall(mcpUrl, query, HIVE || undefined)
        if (!memoriesRaw) return

        // Step 4: filter via small model
        const filterPrompt = `You are filtering memory_recall results for relevance to the user's actual prompt.\n\nUser prompt: ${userText}\nRewritten query: ${query}\n\nMemory results:\n${memoriesRaw}\n\nPick the 1-3 items most relevant to the user's prompt. If none are genuinely relevant, output exactly "IRRELEVANT". Otherwise output a compact markdown block.\n\nOutput only the block, no preamble.`
        const filtered = (await $`opencode -p ${filterPrompt} --model ${MODEL}`.text()).trim().slice(0, 3000)
        if (!filtered || filtered === "IRRELEVANT") return

        // Step 5: inject as extra text part
        output.parts.unshift({
          type: "text",
          text: `NeoHive smart context (rewriter: ${MODEL}, hive: ${HIVE || "all"}):\n${filtered}`,
        } as any)
      } catch {
        // Never block the user's prompt on plugin failure.
      }
    },
  }
}) satisfies Plugin

async function discoverNeoHiveUrl(cwd: string): Promise<string | undefined> {
  const fs = await import("node:fs/promises")
  const path = await import("node:path")
  const candidates = [
    path.join(cwd, ".mcp.json"),
    path.join(cwd, "opencode.json"),
    path.join(cwd, "opencode.jsonc"),
    path.join(process.env.HOME || "", ".config/opencode/opencode.json"),
    path.join(process.env.HOME || "", ".config/opencode/opencode.jsonc"),
  ]
  for (const p of candidates) {
    try {
      const raw = await fs.readFile(p, "utf8")
      const stripped = raw.replace(/\/\/.*/g, "").replace(/\/\*[\s\S]*?\*\//g, "")
      const data = JSON.parse(stripped)
      const mcp = data.mcp || data.mcpServers || {}
      for (const [name, server] of Object.entries<any>(mcp)) {
        if (/neohive|hivemind/i.test(name) && server && typeof server.url === "string") {
          return server.url
        }
      }
    } catch {}
  }
  return undefined
}

async function callMemoryRecall(url: string, query: string, hive?: string): Promise<string | undefined> {
  const args: Record<string, unknown> = { query, limit: 8 }
  if (hive) args.hive = hive
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
  }
  if (process.env.NEOHIVE_TOKEN) headers["Authorization"] = `Bearer ${process.env.NEOHIVE_TOKEN}`
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "memory_recall", arguments: args } }),
      signal: AbortSignal.timeout(8000),
    })
    const raw = await resp.text()
    let payload = raw
    for (const line of raw.split("\n")) {
      if (line.startsWith("data: ")) { payload = line.slice(6); break }
    }
    const data = JSON.parse(payload)
    const contents = data?.result?.content ?? []
    const texts = contents.filter((c: any) => c?.type === "text").map((c: any) => c.text)
    const combined = texts.join("\n")
    if (combined && !combined.includes("No relevant memories found")) return combined.slice(0, 6000)
  } catch {}
  return undefined
}
```

## Important rules

- **Never overwrite an existing `~/.config/opencode/plugin/neohive-smart-prompts.ts` without confirmation.** If the file exists, show its contents and ask whether to replace.
- **Never put the API key in the generated script.** The script reads `$ANTHROPIC_API_KEY` at runtime.
- **Never hardcode the hive UUID in the script.** It discovers the MCP URL the same way the bundled plugin does (via `.mcp.json` / `opencode.json`).
- **Always wrap async hook bodies in try/catch.** A broken plugin must never block the user's prompt.
- **Always set an `AbortSignal.timeout` on every `fetch`.** A slow hook blocks every prompt.
