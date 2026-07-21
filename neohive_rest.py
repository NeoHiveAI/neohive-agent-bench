#!/usr/bin/env python3
"""Thin NeoHive client for the compounding-memory harness (HIVE-334/335).

Targets a **local** NeoHive by default (``http://127.0.0.1:3577``, dev auth, no
Cloudflare) — the compounding experiment runs its own NeoHive on the run host so
the hive data dir is on local disk (byte-exact FS snapshots, see ``hive_snapshot``)
and per-repo projects can be created freely. Cloudflare-Access / PAT headers are
still honoured if set, so the same client also works against the hosted deployment.

Two transports, matching how NeoHive actually exposes things (verified against the
running server + the indexed server code):
  - **REST** ``/api/*`` with an ``X-HiveMind-Id: <projectId>`` header — used for
    listing/creating projects+hives, enumerating a hive's memories, and soft-deleting
    a memory (the REST-fallback snapshot path).
  - **MCP** JSON-RPC ``tools/call`` at ``/projects/<projectId>/mcp`` — used for
    ``memory_store`` (writes auto-route to the project's Knowledge hive; there is no
    REST create route) and ``memory_recall``. This mirrors how the smart-prompts
    opencode plugin already talks to NeoHive.

Network methods are thin and easy to stub: tests subclass and override ``_req`` /
``mcp_call``. Stdlib only — no venv required.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class NeoHiveError(RuntimeError):
    """A NeoHive REST/MCP call failed (non-2xx, transport error, or JSON-RPC error)."""


@dataclass
class NeoHiveClient:
    base: str = "http://127.0.0.1:3577"
    project: str | None = None            # X-HiveMind-Id (REST) + /projects/<id>/mcp (MCP)
    cf_id: str | None = None              # Cloudflare Access service token (hosted only)
    cf_secret: str | None = None
    pat: str | None = None                # Bearer PAT (hosted with Auth0 only)
    user_agent: str = "neohive-agent-bench/0.1"   # any non-default UA (CF WAF blocks urllib's)
    timeout: int = 60

    @classmethod
    def from_env(cls) -> "NeoHiveClient":
        return cls(
            base=os.environ.get("NEOHIVE_BASE", "http://127.0.0.1:3577").rstrip("/"),
            project=os.environ.get("NEOHIVE_PROJECT"),
            cf_id=os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_ID"),
            cf_secret=os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_SECRET"),
            pat=os.environ.get("NEOHIVE_PAT"),
        )

    # ---- low-level transports ----

    def _headers(self, *, with_project: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self.user_agent,
        }
        if with_project:
            if not self.project:
                raise NeoHiveError("NeoHiveClient.project (X-HiveMind-Id) is required for this call")
            h["X-HiveMind-Id"] = self.project
        if self.cf_id and self.cf_secret:
            h["CF-Access-Client-Id"] = self.cf_id
            h["CF-Access-Client-Secret"] = self.cf_secret
        if self.pat:
            h["Authorization"] = f"Bearer {self.pat}"
        return h

    def _req(self, method: str, path: str, body: dict | None = None, *, with_project: bool = True):
        """One REST call. Returns parsed JSON (dict or list) or {} for an empty body."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=self._headers(with_project=with_project), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
            raise NeoHiveError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise NeoHiveError(f"{method} {path} -> transport error: {e.reason}") from e
        return json.loads(raw) if raw.strip() else {}

    def mcp_call(self, name: str, arguments: dict) -> dict:
        """One MCP ``tools/call``. Handles both plain-JSON and SSE (``data: ...``)
        responses and unwraps the JSON-RPC envelope. Raises on a JSON-RPC error."""
        if not self.project:
            raise NeoHiveError("NeoHiveClient.project is required for MCP calls")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        req = urllib.request.Request(
            f"{self.base}/projects/{self.project}/mcp",
            data=json.dumps(payload).encode(),
            headers=self._headers(with_project=False),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
            raise NeoHiveError(f"MCP {name} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise NeoHiveError(f"MCP {name} -> transport error: {e.reason}") from e
        return self._parse_mcp(raw, name)

    @staticmethod
    def _parse_mcp(raw: str, name: str) -> dict:
        payload = raw
        for line in raw.split("\n"):
            if line.startswith("data: "):
                payload = line[6:]
                break
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            raise NeoHiveError(f"MCP {name}: could not parse response: {raw[:200]}") from e
        if isinstance(data, dict) and data.get("error"):
            raise NeoHiveError(f"MCP {name} JSON-RPC error: {data['error']}")
        return data.get("result", data) if isinstance(data, dict) else {}

    @staticmethod
    def mcp_text(result: dict) -> str:
        """Join the text content parts of an MCP tools/call result."""
        parts = result.get("content", []) if isinstance(result, dict) else []
        return "\n".join(c.get("text", "") for c in parts if isinstance(c, dict) and c.get("type") == "text")

    # ---- REST convenience ----

    def list_projects(self) -> list[dict]:
        res = self._req("GET", "/api/projects", with_project=False)
        return res.get("items", res) if isinstance(res, dict) else res

    def create_project(self, name: str, *, description: str = "") -> dict:
        return self._req("POST", "/api/projects", {"name": name, "description": description},
                         with_project=False)

    def delete_project(self, project_id: str, *, force: bool = True) -> dict:
        q = "?force=true" if force else ""
        return self._req("DELETE", f"/api/projects/{project_id}{q}", with_project=False)

    def list_hives(self) -> list[dict]:
        res = self._req("GET", "/api/hives")
        return res.get("items", res) if isinstance(res, dict) else res

    def create_hive(self, name: str, *, hive_type: str = "knowledge",
                    embedding_model: str | None = None, description: str = "") -> dict:
        body: dict = {"name": name, "type": hive_type, "description": description}
        if embedding_model:
            body["embedding_model"] = embedding_model
        return self._req("POST", "/api/hives", body)

    def get_hive(self, name: str) -> dict | None:
        return next((h for h in self.list_hives() if h.get("name") == name), None)

    def get_memories(self, hive_id: str, limit: int = 100000) -> list[dict]:
        """Top-N active memories for a hive (excludes parent chunks; no embeddings).
        Complete content-wise for a knowledge-type hive."""
        res = self._req("GET", f"/api/hives/{hive_id}/memories?limit={limit}")
        return res.get("items", res) if isinstance(res, dict) else res

    def memory_count(self, hive_id: str, limit: int = 100000) -> int:
        return len(self.get_memories(hive_id, limit=limit))

    def delete_memory(self, hive_id: str, memory_id) -> dict:
        return self._req("DELETE", f"/api/hives/{hive_id}/memories/{memory_id}")

    # ---- MCP convenience ----

    def store_memory(self, content: str, mem_type: str, *, importance: int = 5,
                     tags: list[str] | None = None) -> dict:
        """Write a memory via MCP. Auto-routes to the project's Knowledge hive."""
        args: dict = {"content": content, "type": mem_type, "importance": importance}
        if tags:
            args["tags"] = tags
        return self.mcp_call("memory_store", args)

    def recall(self, query: str, *, hive: str | None = None, limit: int = 10) -> dict:
        args: dict = {"query": query, "limit": limit}
        if hive:
            args["hive"] = hive
        return self.mcp_call("memory_recall", args)


if __name__ == "__main__":  # ad-hoc: python3 neohive_rest.py projects | hives | recall "<q>"
    import sys

    c = NeoHiveClient.from_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "projects"
    if cmd == "projects":
        print(json.dumps(c.list_projects(), indent=2))
    elif cmd == "hives":
        print(json.dumps(c.list_hives(), indent=2))
    elif cmd == "recall":
        print(c.mcp_text(c.recall(sys.argv[2])))
    else:
        print(f"unknown cmd {cmd}", file=sys.stderr)
        sys.exit(2)
