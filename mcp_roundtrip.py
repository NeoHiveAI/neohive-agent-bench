#!/usr/bin/env python3
"""
HIVE-263 — Minimal NeoHive MCP round-trip probe (stdlib only).

Runs INSIDE a container (or anywhere) and does a real MCP handshake against an
external NeoHive gateway, then a memory_store -> memory_recall round-trip,
printing latency for each call. This is the in-container ("Pattern 2") case —
the harder reachability scenario; the host-side agent case (Pattern 1) is
strictly easier. See REACHABILITY_FINDINGS.md.

Env:
    NEOHIVE_MCP_URL   e.g. http://host.docker.internal:3577/hiveminds/<id>/mcp
    NEOHIVE_PAT       a NeoHive personal access token (pat_...)

Exit 0 on a successful round-trip; non-zero otherwise.

NOTE: NeoHive's MCP transport (streamable-HTTP) and auth header are validated on
first run against the live gateway. If auth 401s, try x-api-key instead of
Authorization: Bearer (toggle AUTH_HEADER below).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

URL = os.environ.get("NEOHIVE_MCP_URL")
PAT = os.environ.get("NEOHIVE_PAT")
AUTH_HEADER = "Authorization"          # or "x-api-key"
AUTH_VALUE = f"Bearer {PAT}"           # or just PAT, if using x-api-key
PROTOCOL = "2025-06-18"


def post(body, session_id=None):
    """POST one JSON-RPC message; return (parsed_json_or_None, headers)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        AUTH_HEADER: AUTH_VALUE,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = r.headers.get("Content-Type", "")
        sid = r.headers.get("Mcp-Session-Id")
        raw = r.read().decode()
    if "text/event-stream" in ctype:
        # parse SSE: collect the last JSON payload from `data:` lines
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


def call_tool(name, arguments, rpc_id, session_id):
    t0 = time.perf_counter()
    resp, _ = post({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}, session_id)
    dt = (time.perf_counter() - t0) * 1000
    if resp and resp.get("error"):
        raise RuntimeError(f"{name} error: {resp['error']}")
    return resp, dt


def main():
    if not URL or not PAT:
        print("ERROR: set NEOHIVE_MCP_URL and NEOHIVE_PAT", file=sys.stderr)
        return 2
    print(f"[probe] target: {URL}")
    marker = f"reachability-probe-{int(time.time())}"

    # 1) initialize
    t0 = time.perf_counter()
    init, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "neohive-reachability-probe", "version": "0.1"}}})
    print(f"[probe] initialize OK  ({(time.perf_counter()-t0)*1000:.0f} ms)  session={sid}")
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

    # 2) memory_store
    _, store_ms = call_tool("memory_store",
                            {"content": f"NeoHive reachability probe {marker}. Transferable note: probes verify the MCP path.",
                             "type": "insight", "importance": 1, "tags": ["reachability-probe", marker]},
                            2, sid)
    print(f"[probe] memory_store OK  ({store_ms:.0f} ms)")

    # 3) memory_recall
    recall, recall_ms = call_tool("memory_recall", {"query": f"reachability probe {marker}", "limit": 3}, 3, sid)
    body = json.dumps(recall.get("result", recall)) if recall else ""
    hit = marker in body
    print(f"[probe] memory_recall OK ({recall_ms:.0f} ms)  round-trip marker found: {hit}")

    if not hit:
        print("[probe] WARN: stored marker not found in recall (indexing lag or transport mismatch?)", file=sys.stderr)
        return 1
    print(f"[probe] SUCCESS  store={store_ms:.0f}ms recall={recall_ms:.0f}ms")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"[probe] HTTP {e.code}: {e.reason} — check auth (try x-api-key) / URL path", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 — probe: surface any failure plainly
        print(f"[probe] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
