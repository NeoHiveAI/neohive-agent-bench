#!/usr/bin/env python3
"""
HIVE-265 — `neohive-search`: the Arm-B retrieval shim.

mini-swe-agent is bash-only: its single actuator is bash commands executed
*inside* the per-instance SWE-bench container (`docker exec`). It has no MCP
client and no skill system, so NeoHive cannot be a host-side MCP tool (the
HIVE-263 "Pattern 1" story). Instead retrieval is a bash command the agent runs
in the container, which reaches the (public) NeoHive over MCP.

This script IS that command. Given a natural-language query, it does a NeoHive
MCP `memory_recall` against the instance's indexed-repo hive and prints the top
code chunks for the agent to read. stdlib-only (the container's python is the
repo's conda env, which may lack requests/httpx) — transport reused from
mcp_roundtrip.py (HIVE-263).

Usage (inside the container):
    python /opt/neohive/neohive_search.py "<query>" [limit]

Auth/env (forwarded into the container by run_arm_b.sh):
    NEOHIVE_MCP_URL                 e.g. https://neohive.logilica.com/hiveminds/<project>/mcp
    NEOHIVE_CF_ACCESS_CLIENT_ID     Cloudflare Access service-token id     (hosted)
    NEOHIVE_CF_ACCESS_CLIENT_SECRET Cloudflare Access service-token secret (hosted)
    NEOHIVE_PAT                     (optional) a NeoHive PAT, if Auth0 is ever enabled
    NEOHIVE_HIVE                    (optional) the instance's hive id; scopes recall to
                                    that hive. Omit to fan out across all hives.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("NEOHIVE_MCP_URL")
HIVE = os.environ.get("NEOHIVE_HIVE")  # optional hive scoping
PROTOCOL = "2025-06-18"


def auth_headers():
    """Build auth headers from the environment.

    The hosted NeoHive sits behind Cloudflare Access (no Auth0/PAT): a CF service
    token (CF-Access-Client-Id/Secret) gets past CF, after which NeoHive attaches
    owner context itself. A NEOHIVE_PAT is also honored if a future deployment
    enables Auth0.
    """
    h = {}
    cf_id = os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_ID")
    cf_secret = os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        h["CF-Access-Client-Id"] = cf_id
        h["CF-Access-Client-Secret"] = cf_secret
    pat = os.environ.get("NEOHIVE_PAT")
    if pat:
        h["Authorization"] = f"Bearer {pat}"
    return h


def post(body, session_id=None):
    """POST one JSON-RPC message; return (parsed_json_or_None, session_id)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # Cloudflare's WAF blocks the default "Python-urllib/x.y" UA (Error 1010);
        # any explicit UA passes. Required for the shim to work from the container.
        "User-Agent": "neohive-search/0.1",
        **auth_headers(),
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = r.headers.get("Content-Type", "")
        sid = r.headers.get("Mcp-Session-Id")
        raw = r.read().decode()
    if "text/event-stream" in ctype:
        payload = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[len("data:"):].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        payload = json.loads(chunk)
                    except json.JSONDecodeError:
                        pass
        return payload, sid
    return (json.loads(raw) if raw.strip() else None), sid


def extract_text(recall_resp):
    """Pull human-readable text out of an MCP tools/call result.

    MCP returns {"result": {"content": [{"type": "text", "text": "..."}], ...}}.
    For a code hive the text blocks are the ranked chunks (path + snippet). We
    print them verbatim so the agent reads exactly what NeoHive surfaced.
    """
    if not recall_resp:
        return ""
    result = recall_resp.get("result", recall_resp)
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(p for p in parts if p)
        if joined:
            return joined
    if isinstance(result, dict) and result.get("structuredContent"):
        return json.dumps(result["structuredContent"], indent=2)
    return json.dumps(result, indent=2)


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('usage: neohive_search.py "<query>" [limit]', file=sys.stderr)
        return 2
    if not URL:
        print("ERROR: NEOHIVE_MCP_URL must be set.", file=sys.stderr)
        return 2
    if not auth_headers():
        print("ERROR: set NEOHIVE_CF_ACCESS_CLIENT_ID/SECRET (or NEOHIVE_PAT) for auth.", file=sys.stderr)
        return 2

    query = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8

    _, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                              "clientInfo": {"name": "neohive-search", "version": "0.1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

    args = {"query": query, "limit": limit}
    if HIVE:
        args["hive"] = HIVE
    t0 = time.perf_counter()
    resp, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "memory_recall", "arguments": args}}, sid)
    dt = (time.perf_counter() - t0) * 1000
    if resp and resp.get("error"):
        print(f"neohive-search error: {resp['error']}", file=sys.stderr)
        return 1

    text = extract_text(resp)
    print(text if text.strip() else "(no results)")
    print(f"\n[neohive-search: '{query}' -> {limit} results in {dt:.0f} ms"
          + (f", hive={HIVE}]" if HIVE else "]"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"neohive-search HTTP {e.code}: {e.reason} — check NEOHIVE_MCP_URL + "
              "CF-Access service token (NEOHIVE_CF_ACCESS_CLIENT_ID/SECRET)", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 — agent-facing tool: surface failure plainly
        print(f"neohive-search FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
