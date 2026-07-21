/**
 * NeoHive opencode plugin — global, all sessions.
 *
 * Adapted from the NeoHiveAi/NeoHiveClaude Claude Code plugin to use
 * opencode's plugin hooks instead of Claude Code's shell-based hook scripts.
 *
 * What this plugin does:
 *
 * 1. tool.execute.before — nudges the model toward NeoHive's memory_recall
 *    when it reaches for `glob` or `grep` (and now also `list`) inside a
 *    project that has a NeoHive MCP server registered. By default a soft
 *    nudge is emitted via the bus event channel; set NEOHIVE_PRETOOL_STRICT=1
 *    to convert the nudge into a thrown error that forces the model to retry.
 *
 * Skipped vs the Claude plugin (and the reasons):
 *
 * - SessionStart hook → not needed. opencode auto-loads
 *   ~/.config/opencode/instructions/neohive.md via the global config's
 *   `instructions:` field; the file is in context from turn 0 of every
 *   session, persistent across compaction. No script needed.
 *
 * - UserPromptSubmit auto-context hook → opted out by default. The
 *   instructions tell the model to call memory_context manually, which
 *   keeps token cost bounded and avoids a network call on every prompt.
 *   To install a smarter, model-rewritten version, run the
 *   `enable-smart-prompts` skill, which generates and registers a separate
 *   plugin file at ~/.config/opencode/plugin/neohive-smart-prompts.ts.
 *
 * Environment overrides (all default off):
 *
 *   NEOHIVE_PRETOOL_DISABLED=1   — skip the glob/grep nudge entirely
 *   NEOHIVE_PRETOOL_STRICT=1     — throw on glob/grep in indexed projects
 *                                  (model must retry with memory_recall)
 *   NEOHIVE_MCP_HINTS=0          — suppress the MCP-side hint emitted on
 *                                  every memory_recall response (handled
 *                                  server-side; documented here for parity)
 */

// NOTE: type-only `import type { Plugin }` removed — opencode's in-container plugin
// loader silently drops plugins that carry it (empirically; a no-import plugin loads
// fine). This plugin is stderr-only by opencode's design (it cannot inject context into
// the model), so it is effectively inert during a solve regardless; kept for parity.
import { existsSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { homedir } from "node:os"

const TOOLS_TO_NUDGE = new Set(["glob", "grep", "list"])

const NUDGE_TEXT =
  "This directory is indexed by NeoHive. Prefer the NeoHive `memory_recall` MCP " +
  "tool (semantic search over embedded source) before falling back to glob/grep/list " +
  "— it returns the most relevant files in a single call, uses far less context " +
  "than a tree walk, and covers the same content. For multi-file exploration, " +
  "dispatch the bundled `explore-neohive` subagent instead of `explore`. " +
  "Use glob/grep when you need exact-string matching (e.g. a unique symbol or " +
  "import path) or for files created in this session that the index doesn't yet cover."

/**
 * Walk up from `start` looking for an `.mcp.json` (or `opencode.json` /
 * `opencode.jsonc`) that registers a NeoHive MCP server. Returns the
 * matching server's URL/command string, or undefined if none is found
 * before hitting the filesystem root.
 *
 * The match is lenient: any MCP server whose key contains "neohive" or
 * "hivemind" (case-insensitive) counts. opencode and Claude Code use
 * slightly different MCP config shapes; this scans both:
 *
 *   opencode style:   { "mcp": { "name": { "url": "..." } } }
 *   claude style:     { "mcpServers": { "name": { "url": "..." } } }
 */
function findNeoHiveServerHint(start: string): string | undefined {
  let dir = start
  while (dir && dir !== "/" && dir !== dirname(dir)) {
    for (const filename of ["opencode.json", "opencode.jsonc", ".mcp.json"]) {
      const p = join(dir, filename)
      if (!existsSync(p)) continue
      const hint = scanConfigFile(p)
      if (hint) return hint
    }
    dir = dirname(dir)
  }

  // Fall back to the user's global opencode + Claude configs.
  const home = homedir()
  for (const p of [
    join(home, ".config/opencode/opencode.json"),
    join(home, ".config/opencode/opencode.jsonc"),
    join(home, ".claude.json"),
  ]) {
    if (!existsSync(p)) continue
    const hint = scanConfigFile(p)
    if (hint) return hint
  }
  return undefined
}

function scanConfigFile(path: string): string | undefined {
  let raw: string
  try {
    raw = readFileSync(path, "utf8")
  } catch {
    return undefined
  }
  // Tolerate JSONC: strip `// line comments` and `/* block comments */`
  // before parsing. This is the same trick opencode uses internally.
  const stripped = raw
    .replace(/\/\/.*$/gm, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
  let data: unknown
  try {
    data = JSON.parse(stripped)
  } catch {
    return undefined
  }
  if (!data || typeof data !== "object") return undefined
  const obj = data as Record<string, unknown>
  const servers = (obj.mcp ?? obj.mcpServers ?? {}) as Record<string, unknown>
  if (!servers || typeof servers !== "object") return undefined
  for (const [name, server] of Object.entries(servers)) {
    if (!/neohive|hivemind/i.test(name)) continue
    if (!server || typeof server !== "object") continue
    const s = server as Record<string, unknown>
    if (typeof s.url === "string" && s.url) return s.url
    if (Array.isArray(s.command) && s.command.length > 0) return s.command.join(" ")
    // bare match: registered but no url/command — still counts as "neohive present"
    return name
  }
  return undefined
}

export default (async ({ directory }) => {
  return {
    "tool.execute.before": async (input, _output) => {
      // Compounding mode (HIVE-335): the ONLY write path is the host-side,
      // filter-gated reflect-and-store step. Block the agent's own direct
      // memory_store so an unfiltered verbatim answer can't reach the hive. Off
      // unless the harness sets NEOHIVE_AGENT_WRITE_DISABLED=1, so single-pass is
      // unaffected.
      if (process.env.NEOHIVE_AGENT_WRITE_DISABLED === "1" &&
          typeof input.tool === "string" && /(^|[_.])memory_store$/.test(input.tool)) {
        throw new Error(
          "[neohive] Direct memory_store is disabled in compounding mode; learnings are " +
          "written by the host-side, filter-gated reflect-and-store step (HIVE-335/336).")
      }
      if (process.env.NEOHIVE_PRETOOL_DISABLED === "1") return
      if (!TOOLS_TO_NUDGE.has(input.tool)) return

      const hint = findNeoHiveServerHint(directory)
      if (!hint) return

      const message = `[neohive] ${NUDGE_TEXT} (matched MCP hint: ${hint})`

      if (process.env.NEOHIVE_PRETOOL_STRICT === "1") {
        // Strict mode: throw so the model is forced to back out and
        // re-plan. opencode surfaces the thrown error to the model as
        // the tool result, which the model will read and route around.
        throw new Error(message)
      }

      // Default mode: soft nudge. opencode's tool.execute.before hook
      // exposes no documented way to inject additional context into the
      // tool result, so we route the reminder to stderr (visible in the
      // opencode log) where the user can see that the plugin fired
      // without polluting tool output. The model itself is steered by
      // the system-prompt ruleset loaded from
      // ~/.config/opencode/instructions/neohive.md, which already tells
      // it to prefer memory_recall over filesystem traversal.
      try {
        process.stderr.write(`${message}\n`)
      } catch {
        // Best-effort; never block the tool call.
      }
    },
  }
})
