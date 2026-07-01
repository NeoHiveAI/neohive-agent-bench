// neohive-smart-prompts.ts — smart-recall auto-context for opencode (Arm B).
//
// This is the "smart-recall" feature from neohive:enable-smart-prompts, rewritten in
// the VERIFIED-LOADABLE style. opencode's in-container plugin loader fires a plain
// no-import plugin (confirmed empirically: a marker plugin's init + tool.execute.before
// both fired inside the SWE-bench image), but the upstream typed plugin
// (`import type { Plugin } ... satisfies Plugin`) stayed silent there. So this file
// deliberately uses NO imports and NO `satisfies` — just the async-default-export shape.
//
// Behavior (faithful to enable-smart-prompts' intent): on each user message, call
// NeoHive memory_recall (scoped to the instance's hive via NEOHIVE_HIVE, through
// Cloudflare Access) and inject the results as a context block on the message — the
// auto-context "forcing function". The optional model-driven query rewrite/filter from
// the stock template is omitted here for reliability (it needs extra model calls and
// was the part most prone to silent failure); the user's prompt is used as the query.
//
// Env: NEOHIVE_MCP_URL (the project MCP endpoint), NEOHIVE_HIVE (scope), CF service
// token (NEOHIVE_CF_ACCESS_CLIENT_ID/SECRET). NEOHIVE_SMART_DISABLED=1 disables it.
// Emits [neohive-smart] lines to stderr so each run proves whether it fired/injected.

export default (async () => {
  try { process.stderr.write("[neohive-smart] plugin init\n") } catch {}
  return {
    "chat.message": async (_input, output) => {
      try {
        if (process.env.NEOHIVE_SMART_DISABLED === "1") return
        const url = process.env.NEOHIVE_MCP_URL
        if (!url) { process.stderr.write("[neohive-smart] no NEOHIVE_MCP_URL\n"); return }

        const userText = (output.parts || [])
          .filter((p) => p && p.type === "text")
          .map((p) => p.text)
          .join("\n")
          .slice(0, 2000)
        if (userText.length < 10 || userText.startsWith("/")) return

        const args = { query: userText, limit: 6 }
        if (process.env.NEOHIVE_HIVE) args.hive = process.env.NEOHIVE_HIVE
        const headers = {
          "Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
          "User-Agent": "neohive-smart-prompts/0.1",
        }
        if (process.env.NEOHIVE_CF_ACCESS_CLIENT_ID && process.env.NEOHIVE_CF_ACCESS_CLIENT_SECRET) {
          headers["CF-Access-Client-Id"] = process.env.NEOHIVE_CF_ACCESS_CLIENT_ID
          headers["CF-Access-Client-Secret"] = process.env.NEOHIVE_CF_ACCESS_CLIENT_SECRET
        }
        if (process.env.NEOHIVE_TOKEN) headers["Authorization"] = `Bearer ${process.env.NEOHIVE_TOKEN}`

        const resp = await fetch(url, {
          method: "POST",
          headers,
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call",
                                 params: { name: "memory_recall", arguments: args } }),
          signal: AbortSignal.timeout(12000),
        })
        const raw = await resp.text()
        let payload = raw
        for (const line of raw.split("\n")) { if (line.startsWith("data: ")) { payload = line.slice(6); break } }
        const data = JSON.parse(payload)
        const texts = (data && data.result && data.result.content ? data.result.content : [])
          .filter((c) => c && c.type === "text").map((c) => c.text)
        const combined = texts.join("\n")
        if (!combined || combined.indexOf("No relevant memories found") !== -1) {
          process.stderr.write("[neohive-smart] recall returned nothing\n"); return
        }
        process.stderr.write(`[neohive-smart] injected ${combined.length} chars from memory_recall\n`)
        output.parts.unshift({
          type: "text",
          text: `NeoHive smart context (auto-recall over the indexed repo):\n${combined.slice(0, 4000)}`,
        })
      } catch (e) {
        try { process.stderr.write("[neohive-smart] err " + e + "\n") } catch {}
      }
    },
  }
})
