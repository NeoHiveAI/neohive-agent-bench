---
name: getting-started
description: Use when the user says "set up NeoHive", "get me started with NeoHive", "first time using NeoHive", "onboard me to NeoHive", or when `list_hives` has never been called in this opencode instance. First-run setup that walks a new user through verifying the MCP server, configuring auth, generating a project-specific topology block in AGENTS.md, migrating existing project memory (CLAUDE.md / AGENTS.md / .claude/rules / .opencode/instructions), and enabling optional helpers. Invoke once per machine after installing the neohive opencode config bundle. Distinct from `load-context`, which runs at the start of every session.
---

# Getting Started with NeoHive

You are onboarding a user who has just installed the NeoHive opencode config bundle (`~/.config/opencode/instructions/neohive.md`, `~/.config/opencode/agents/explore-neohive.md`, the `~/.config/opencode/skill/neohive/*` skills, and `~/.config/opencode/plugin/neohive.ts`). Your job is to get them from zero to a fully working setup — MCP reachable, memory migrated, helpers configured — without ever leaving them staring at a blank screen.

**Golden rule for this skill: never act silently.** Narrate every step. Every decision has a recommended default. Every write is gated by an explicit confirmation.

## Phase 0 — Tell the user what's about to happen

Open with this exact script (do not paraphrase):

> I'll walk you through setting up NeoHive on this machine. This takes 3–5 minutes and covers:
>   1. Confirming your NeoHive server is reachable via MCP
>   2. (Optional) Setting up your auth token
>   3. Generating a project-specific topology block in your `AGENTS.md`
>   4. Migrating existing project knowledge into NeoHive
>   5. (Optional) Turning on the smart-recall hook
>
> You can stop at any point by saying "stop" or answering "skip" to a step.

Wait for acknowledgement (any affirmative reply, or just continue if they say nothing).

## Phase 1 — Register and verify the MCP server

The opencode config ships **without** a pre-configured MCP server, so the first task is registering your NeoHive gateway via opencode's MCP config.

### 1a. Check whether a NeoHive MCP is already registered

Run this to detect any `*neohive*`-keyed MCP server in the project or user opencode config:

```bash
python3 - <<'PY' 2>/dev/null || echo "no neohive MCP found"
import json, os, re
def scan(path):
    try:
        with open(path) as f: data = json.load(f)
    except Exception: return
    mcp = data.get("mcp") or data.get("mcpServers") or {}
    if not isinstance(mcp, dict): return
    for k, v in mcp.items():
        if re.search(r"neohive|hivemind", k, re.I) and isinstance(v, dict):
            url = v.get("url") or (v.get("command") and " ".join(v["command"])) or "<no url/cmd>"
            print(f"{path}: {k} -> {url}")
hits = []
for path in (".mcp.json", "opencode.json", "opencode.jsonc",
             os.path.expanduser("~/.config/opencode/opencode.json"),
             os.path.expanduser("~/.config/opencode/opencode.jsonc"),
             os.path.expanduser("~/.claude.json")):
    scan(path)
PY
```

- **If one is found:** call `list_hives` and interpret per the table below.
- **If none is found:** guide the user to register one (see 1b), then rerun `list_hives`.

### 1b. Registering a server (only if none found)

Tell the user:

> The NeoHive config doesn't bundle a default MCP server — you register yours explicitly in opencode's config. Add a block like this to `~/.config/opencode/opencode.json` (or your project's `opencode.json`):
>
> ```json
> {
>   "mcp": {
>     "neohive": {
>       "type": "remote",
>       "url": "https://your-neohive-host/hiveminds/<hive-id>/mcp",
>       "enabled": true,
>       "headers": { "Authorization": "Bearer ${NEOHIVE_TOKEN}" }
>     }
>   }
> }
> ```
>
> Use any key containing "neohive" — the plugin discovers servers by name match. After saving, restart opencode and rerun the `getting-started` skill.

Pause here until the user confirms they've registered it, or say "skip" to jump to Phase 6.

### 1c. Verify with `list_hives`

Once a server is registered, call `list_hives` and interpret:

| Outcome | What to tell the user |
|---|---|
| Returns hives | "Connected. I can see N hives: X, Y, Z." Proceed to Phase 2. |
| Empty list | "Server is reachable but reports no hives. Confirm with your admin — without at least one hive, NeoHive has nowhere to store memories." Pause for user input. |
| Tool unavailable / error | "I can't reach the NeoHive MCP server." Run the diagnostics below. |

### Diagnostics if unreachable

Run these checks and report results in a compact block:

```bash
# 1. Is NEOHIVE_TOKEN set?
[ -n "${NEOHIVE_TOKEN:-}" ] && echo "token set" || echo "token not set"
# 2. Can we reach the registered server URL?
python3 - <<'PY' 2>/dev/null
import json, os, re, urllib.request, ssl
url = None
for path in (".mcp.json", "opencode.json", "opencode.jsonc",
             os.path.expanduser("~/.config/opencode/opencode.json"),
             os.path.expanduser("~/.config/opencode/opencode.jsonc")):
    try:
        with open(path) as f: data = json.load(f)
    except Exception: continue
    mcp = data.get("mcp") or data.get("mcpServers") or {}
    if not isinstance(mcp, dict): continue
    for k, v in mcp.items():
        if re.search(r"neohive", k, re.I) and isinstance(v, dict) and v.get("url"):
            url = v["url"]; break
    if url: break
if not url:
    print("no neohive URL registered — rerun 1b"); raise SystemExit
try:
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
        print(f"HTTP {r.status} from {url}")
except Exception as e:
    print(f"unreachable: {type(e).__name__}: {e}")
PY
```

Then use the `question` tool to offer: "Fix token now", "I'll fix it later and restart opencode", "Skip MCP setup for now". If they skip, jump to Phase 6 with a warning that memory features won't work.

## Phase 2 — Auth token (only if needed)

If `list_hives` succeeded, skip this phase. Otherwise ask:

- **Header:** "Auth token"
- **Question:** "Does your NeoHive server require a bearer token?"
- Options: `Yes — I have one (Recommended)`, `Yes — I need to get one from my admin`, `No — it's open`, `I'm not sure`

For "Yes — I have one", show:

> Export it before launching opencode:
> ```bash
> export NEOHIVE_TOKEN="your-token-here"
> ```
> Add that line to your shell rc (`~/.bashrc`, `~/.zshrc`, or `~/.config/fish/config.fish`) so it persists. Then restart opencode and rerun the `getting-started` skill.

For the other answers, provide the matching guidance verbatim — don't improvise.

## Phase 3 — Generate project AGENTS.md topology

Now that the MCP is reachable, generate a project-specific topology block in `./AGENTS.md`. This is what makes opencode reliable about *which* hive to query and *where* new writes should land — without it, the rules in `~/.config/opencode/instructions/neohive.md` are running blind.

Ask via the `question` tool:

- **Header:** "Topology block"
- **Question:** "Generate a project topology block in ./AGENTS.md? (Recommended — improves tool-calling accuracy for everyone on this repo.)"
- Options: `Yes (Recommended)`, `Yes, but let me review the table before writing`, `Skip — I'll run generate-agents-md later`

If "Yes" or "Yes, but review": invoke the generator skill via the `skill` tool with name `generate-agents-md`.

The sub-skill handles its own confirmation gates (synthesis review + diff review), so this phase just waits for it to return. When it returns, report: "Topology block written to ./AGENTS.md (N hives mapped)."

If "Skip": tell the user they can run the `generate-agents-md` skill anytime to add the block, and continue.

## Phase 4 — Migrate existing project memory

Ask via the `question` tool:

- **Header:** "Migrate memory"
- **Question:** "Want me to scan this project for existing knowledge (AGENTS.md, CLAUDE.md, .claude/rules, .opencode/instructions) and migrate the project-specific parts into NeoHive?"
- Options: `Yes (Recommended)`, `Yes, but let me review each memory first`, `Skip — nothing worth migrating`, `Skip — I'll do this manually later`

If Yes, invoke the `migrate-memory` skill via the `skill` tool.

Wait for it to complete. Report: "Migration done — N memories stored." Then continue.

If "Yes, but review each": invoke `migrate-memory` with argument `review=each` so it pauses per candidate.

## Phase 5 — Smart-recall hook (optional, power users)

Ask:

- **Header:** "Smart recall"
- **Question:** "The default plugin hook nudges you toward `memory_recall` when you reach for glob/grep. A smarter version (the `enable-smart-prompts` skill) installs a separate prompt-context hook that uses a small model to rewrite the user's prompt before querying NeoHive — usually better results, but costs a few tokens per prompt. Set it up?"
- Options: `Not now (Recommended)`, `Yes, set it up`, `Tell me more first`

If "Yes": invoke the `enable-smart-prompts` skill.
If "Tell me more": explain in 3–4 sentences (what it adds, what it costs, how to disable) then re-ask.

## Phase 6 — Final summary

Print a checklist of what's been set up and what's left. Use ✓ / ○ prefixes:

```
✓ MCP server reachable (N hives: ...)
✓ Auth token configured
✓ Project topology block in ./AGENTS.md (N hives mapped)
✓ N project memories migrated
○ Smart-recall hook (skipped; rerun enable-smart-prompts anytime)
```

Then this exact closing block:

> **You're set. Three things to remember:**
>   1. Start every new session by invoking the `load-context` skill with what you're working on, to pre-load relevant memory.
>   2. End sessions by invoking `capture-session-learnings` so new insights get captured.
>   3. When docs feel stale, try `design-codebase-docs`.
>
> Run the `getting-started` skill again anytime to revisit these steps.

## Important rules

- **Never call `memory_store` directly from this skill.** Delegate to `migrate-memory` or `capture-session-learnings`.
- **Never edit the user's shell rc files yourself.** Show the command, let them paste.
- **If the user says "stop" or "skip" at any phase, stop immediately** and print the Phase 6 summary with what's done so far.
- **If any sub-skill fails, surface the error plainly** and offer to skip that phase rather than retrying silently.
