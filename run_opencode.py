#!/usr/bin/env python3
"""
HIVE-265/268 — Run opencode on SWE-bench instances, per arm, INSIDE the container.

Arm A = opencode + OpenRouter model, no NeoHive. Arm B = identical + NeoHive wired
as an MCP server (config/opencode-arm-b.json). The agent uses NeoHive's MCP tools
natively — the faithful end-user experience. Both arms run opencode inside the
per-instance SWE-bench image so the agent has the repo's deps and can run tests.

Per instance:
  - (Arm B) resolve the hive: `index_instance.py <id>` indexes/reuses ONE hive per
    repo at that repo's oldest pilot base_commit (leakage-safe; ancestor of all its
    instances). Idempotent — first instance of a repo indexes it, the rest reuse it.
  - docker run the image with the pinned opencode binary mounted (-v ...:ro).
  - docker cp the arm's opencode.json to /root/.config/opencode/ (OUTSIDE /testbed,
    so it never lands in the diff).
  - `opencode run -m <model> "<task>"` in /testbed (wall-clock budget).
  - `git -C /testbed diff` -> model_patch -> preds.json.
  - tear down the container; (Arm B) delete the hive.

Then grade with grade_swebench.sh.

Env:  OPENROUTER_API_KEY (required); NEOHIVE_PROJECT + NEOHIVE_CF_ACCESS_CLIENT_ID/
      SECRET (Arm B). CF creds also used by index_instance.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import index_instance as idx  # reuse the NeoHive API client + dataset loader
from neohive_rest import NeoHiveClient  # HIVE-335 host-side write path
from reflect_and_store import reflect_and_store

HERE = Path(__file__).resolve().parent
TASK_TEMPLATE = """<issue>
{problem_statement}
</issue>

You are working in the repository checked out at /testbed (the current directory).
Resolve the issue described above by editing the repository's SOURCE code.

- Make the minimal change needed and keep it consistent with the codebase.
- Do NOT modify tests or configuration files; the failing tests are hidden.
- You may run the repository's existing tests to reproduce the problem and verify
  your fix.
- When you are confident the fix is complete, stop. Do not commit; leave the edits
  in the working tree.
"""


def docker(*args: str, check: bool = True, timeout: int | None = None, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True,
                          capture_output=capture, timeout=timeout)


def image_for(instance_id: str) -> str:
    return f"docker.io/swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def opencode_bin() -> str:
    out = subprocess.run([str(HERE / "fetch_opencode.sh")], check=True, text=True, capture_output=True)
    return out.stdout.strip().splitlines()[-1]


def node_dir() -> str:
    """Pinned node linux-x64 dir (opencode's plugin loader needs a node runtime; the
    eval images have none). Mounted read-only into each container."""
    out = subprocess.run([str(HERE / "fetch_node.sh")], check=True, text=True, capture_output=True)
    return out.stdout.strip().splitlines()[-1]


# ---- Arm-B NeoHive hive lifecycle (temporal isolation: one hive in the project at a time) ----

def project_hives() -> list[dict]:
    res = idx._neohive_req("GET", "/api/hives")
    return res.get("items", res if isinstance(res, list) else [])


def write_routing(cfgdir: Path, repo: str, base_commit: str, hive_id: str) -> None:
    """Generate the NeoHive HIVE-routing table (the generate-agents-md "topology
    block" idea) and register it as an opencode instruction. Multiple repos coexist
    in one project (real NeoHive usage); this tells the agent which hive holds
    /testbed's repo and to scope recall to it — routing + the contamination guard in
    one. Hives persist and are reused across runs (no per-run teardown).

    Each hive indexes its repo at a commit that is an ancestor of every routed instance's
    base_commit (leakage-safe) — the repo's oldest pilot base for most repos, or the
    instance's own base where a repo's instances diverge. So the index is at or before
    /testbed's checkout of the same repository; semantic recall over its source applies."""
    rows = []
    for h in project_hives():
        if h.get("type") != "repo":
            continue
        nm = h.get("name", "")
        mark = "  ← THIS repo (/testbed)" if h.get("id") == hive_id else ""
        rows.append(f"| `{h.get('id')}` | {nm} |{mark} |")
    table = "\n".join(rows) if rows else "| (none) | | |"
    routing = f"""# NeoHive hive routing (this project)

This NeoHive project indexes one repository per hive. Your working directory
(`/testbed`) is **{repo} @ {base_commit}**. That repository is indexed in hive
**`{hive_id}`** (pinned at a commit of the *same* repo at or before your checkout — same
modules, same APIs, so semantic recall over its source applies directly).

When you call the NeoHive `memory_recall` / `memory_context` MCP tools, ALWAYS pass
`hive: "{hive_id}"` so results come only from this repository's index (other hives in
this project hold unrelated repos).

| hive id | name (repo) | |
|---|---|---|
{table}
"""
    (cfgdir / "instructions").mkdir(exist_ok=True)
    (cfgdir / "instructions" / "neohive-routing.md").write_text(routing)
    ocj = cfgdir / "opencode.json"
    cfg = json.loads(ocj.read_text())
    instr = cfg.setdefault("instructions", [])
    if "instructions/neohive-routing.md" not in instr:
        instr.append("instructions/neohive-routing.md")
    ocj.write_text(json.dumps(cfg, indent=2))


def write_routing_compounding(cfgdir: Path, repo: str, base_commit: str, hive_id: str) -> None:
    """Compounding-mode routing (HIVE-334/335). Unlike single-pass `write_routing`, the
    agent recalls ACROSS this career project's hives — the repo's code index (`hive_id`)
    AND the accumulated experience pool (the project's Knowledge hive that reflect-and-
    store writes to each round) — so learnings from earlier rounds are retrievable. It
    therefore does NOT pin a single `hive:` scope. Because the project holds only this
    one repo's code + its own experience pool, cross-hive recall stays leakage-clean."""
    routing = f"""# NeoHive routing (compounding career project)

Your working directory (`/testbed`) is **{repo} @ {base_commit}**, whose source is
indexed in hive **`{hive_id}`**. This project ALSO holds an accumulated *experience
pool* (the Knowledge hive) written from earlier rounds solving other issues in this
same repository.

When you call `memory_recall` / `memory_context`, do NOT restrict to a single hive —
recall across this project's hives so you get BOTH the repo's source and prior
transferable lessons. Everything here is the same repository at or before your
checkout, so it applies directly.
"""
    (cfgdir / "instructions").mkdir(exist_ok=True)
    (cfgdir / "instructions" / "neohive-routing.md").write_text(routing)
    ocj = cfgdir / "opencode.json"
    cfg = json.loads(ocj.read_text())
    instr = cfg.setdefault("instructions", [])
    if "instructions/neohive-routing.md" not in instr:
        instr.append("instructions/neohive-routing.md")
    ocj.write_text(json.dumps(cfg, indent=2))


NEOHIVE_TOOLS =["neohive_memory_recall", "neohive_memory_context", "neohive_memory_store",
                 "neohive_list_hives", "neohive_memory_stats"]


def parse_usage(events_text: str) -> dict:
    """Accurate NeoHive-usage from opencode --format json events: count tool_use
    parts whose tool is a NeoHive MCP tool (not regex over the raw text, which
    conflates a real call with the tool merely being listed), plus the explore-neohive
    subagent and the smart-prompts injection marker."""
    counts = {t: 0 for t in NEOHIVE_TOOLS}
    explore = 0
    for line in events_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        part = e.get("part") if isinstance(e.get("part"), dict) else {}
        if part.get("type") == "tool":
            tool = part.get("tool", "")
            if tool in counts:
                counts[tool] += 1
            if "explore-neohive" in str(tool) or part.get("agent") == "explore-neohive":
                explore += 1
    counts["explore_neohive_dispatch"] = explore
    # The smart-prompts plugin logs "[neohive-smart] injected N chars" to stderr on each
    # successful auto-context injection (the injected text goes into the model's INPUT,
    # not the output event stream, so stderr is the reliable signal).
    counts["smart_context_injections"] = events_text.count("[neohive-smart] injected")
    counts["used_neohive"] = (any(counts[t] for t in NEOHIVE_TOOLS)
                              or explore > 0 or counts["smart_context_injections"] > 0)
    return counts


def index_instance(instance_id: str) -> str:
    """Run index_instance.py for one instance; return the NEOHIVE_HIVE id."""
    p = subprocess.run([str(HERE / ".venv/bin/python"), str(HERE / "index_instance.py"), instance_id],
                       text=True, capture_output=True)
    sys.stderr.write(p.stdout)
    if p.returncode != 0:
        raise RuntimeError(f"indexing failed for {instance_id}:\n{p.stdout}\n{p.stderr}")
    line = next((l for l in p.stdout.splitlines() if l.startswith("NEOHIVE_HIVE=")), "")
    if not line:
        raise RuntimeError(f"index_instance.py produced no NEOHIVE_HIVE for {instance_id}")
    return line.split("=", 1)[1].strip()


def build_config_dir(arm: str, project: str | None) -> Path:
    """Assemble the opencode config placed PROJECT-level into the container's /testbed.

    Arm A: just opencode.json (permissions, no NeoHive). Arm B: opencode.json
    (permissions + MCP + instructions + plugin refs) plus the full faithful NeoHive
    setup (instructions/, plugin/ x2, agents/, skill/) from config/neohive-opencode/.
    Placed in /testbed because opencode only loads plugins from the project config;
    the files are untracked, so `git diff` (the prediction) excludes them.
    """
    d = Path(tempfile.mkdtemp())
    raw = (HERE / "config" / f"opencode-arm-{arm}.json").read_text()
    if arm == "b":
        if not project:
            raise SystemExit("Arm B needs NEOHIVE_PROJECT")
        raw = raw.replace("__NEOHIVE_PROJECT__", project)
        src = HERE / "config" / "neohive-opencode"
        for sub in ("instructions", "plugin", "agents", "skill"):
            if (src / sub).exists():
                shutil.copytree(src / sub, d / sub)
    (d / "opencode.json").write_text(raw)
    return d


def run_instance(instance_id: str, arm: str, model: str, ocbin: str, nodedir: str, out_dir: Path,
                 timeout: int, project: str | None,
                 compounding: bool = False, twin: bool = False) -> None:
    inst = idx.load_instance(instance_id)
    image = image_for(instance_id)
    inst_dir = out_dir / instance_id
    inst_dir.mkdir(parents=True, exist_ok=True)
    hive_id = None
    # Modes (all arm-b only; both default False => untouched single-pass A/B behaviour):
    #   career_mode = recall across the career project's hives (repo code + experience pool)
    #   writes_on   = run the HIVE-335 reflect-and-store write path after the task (treated)
    #   twin        = same scaffold + cross-hive recall but writes OFF (HIVE-338 control)
    career_mode = arm == "b" and (compounding or twin)
    writes_on = arm == "b" and compounding and not twin

    cfgdir = build_config_dir(arm, project)
    if arm == "b":
        hive_id = index_instance(instance_id)  # idempotent; hives persist (multi-repo project)
        if career_mode:
            write_routing_compounding(cfgdir, inst["repo"], inst["base_commit"], hive_id)
        else:
            write_routing(cfgdir, inst["repo"], inst["base_commit"], hive_id)

    print(f"[run] arm={arm} {instance_id} model={model} image={image}" + (f" hive={hive_id}" if hive_id else ""))
    # Mount opencode + node read-only. Config goes PROJECT-level into /testbed (opencode
    # only loads plugins from the project config; untracked files are excluded from git diff).
    cid = docker("run", "-d", "--rm",
                 "-v", f"{ocbin}:/opt/oc/opencode:ro",
                 "-v", f"{nodedir}:/opt/node:ro",
                 image, "sleep", str(timeout + 600)).stdout.strip()
    try:
        docker("cp", f"{cfgdir}/.", f"{cid}:/testbed/")
        # Symlink into /usr/local/bin (already on PATH) rather than overriding PATH —
        # keeps the image's conda `testbed` env intact for the agent's own commands/tests.
        docker("exec", cid, "ln", "-sf", "/opt/oc/opencode", "/usr/local/bin/opencode", check=False)
        docker("exec", cid, "ln", "-sf", "/opt/node/bin/node", "/usr/local/bin/node", check=False)
        docker("exec", cid, "ln", "-sf", "/opt/node/bin/npm", "/usr/local/bin/npm", check=False)

        env_flags = ["-e", "OPENROUTER_API_KEY"]
        if arm == "b":
            mcp_url = f"{os.environ.get('NEOHIVE_BASE', 'https://neohive.logilica.com').rstrip('/')}/projects/{project}/mcp"
            env_flags += ["-e", "NEOHIVE_CF_ACCESS_CLIENT_ID", "-e", "NEOHIVE_CF_ACCESS_CLIENT_SECRET",
                          "-e", f"NEOHIVE_MCP_URL={mcp_url}"]    # smart-prompts plugin calls this directly
            if career_mode:
                # Cross-hive recall within the career project: DON'T pin NEOHIVE_HIVE, so
                # smart-prompts + the agent see the repo code index AND the experience pool.
                if writes_on:
                    # Only the host-side, filter-gated reflect step may write; block the
                    # agent's own unfiltered memory_store (guarded in plugin/neohive.ts).
                    env_flags += ["-e", "NEOHIVE_AGENT_WRITE_DISABLED=1"]
            else:
                env_flags += ["-e", f"NEOHIVE_HIVE={hive_id}"]  # single-pass: scope recall to this repo hive
        task = TASK_TEMPLATE.format(problem_statement=inst["problem_statement"])
        # --format json => raw event stream (tool calls etc.) for usage telemetry;
        # --print-logs => plugin activity (smart-prompts, grep-nudge) on stderr.
        try:
            r = docker("exec", *env_flags, "-w", "/testbed", cid,
                       "/opt/oc/opencode", "run", "--format", "json", "--print-logs", "-m", model, task,
                       timeout=timeout, check=False)
            events, errlog = r.stdout or "", r.stderr or ""
            status = "ok" if r.returncode == 0 else f"exit{r.returncode}"
        except subprocess.TimeoutExpired as e:
            dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
            events, errlog = dec(e.stdout), dec(e.stderr)
            status = "timeout"
        (inst_dir / "opencode-events.json").write_text(events)
        (inst_dir / "opencode-stderr.log").write_text(errlog)

        diff = docker("exec", "-w", "/testbed", cid, "git", "diff").stdout
        (inst_dir / "patch.diff").write_text(diff)
        update_preds(out_dir / "preds.json", instance_id, model, diff)

        usage_str = ""
        if arm == "b":
            usage = parse_usage(events + "\n" + errlog)
            (inst_dir / "usage.json").write_text(json.dumps(usage, indent=2))
            usage_str = (f" neohive_used={usage['used_neohive']}"
                         f" recall~{usage['neohive_memory_recall']} smartctx={usage['smart_context_injections']}")
        if writes_on:
            usage_str += reflect_after_task(inst, events + "\n" + errlog, diff, status, project, inst_dir)
        print(f"[run] {instance_id} arm={arm} status={status} patch_len={len(diff)}{usage_str}")
    finally:
        docker("rm", "-f", cid, check=False)
    # No hive teardown: hives persist for reuse (idempotent index) + the multi-repo project.


def update_preds(path: Path, instance_id: str, model: str, patch: str) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    data[instance_id] = {"model_name_or_path": model, "instance_id": instance_id, "model_patch": patch}
    path.write_text(json.dumps(data, indent=2))


def reflect_after_task(inst: dict, transcript: str, diff: str, status: str,
                       project: str | None, inst_dir: Path) -> str:
    """HIVE-335: host-side, filter-gated reflect-and-store after a task. Writes the
    surviving transferable learnings to the career project's Knowledge hive on the
    LOCAL NeoHive instance (NEOHIVE_HOST_BASE, default http://127.0.0.1:3577 — the host
    reaches its own instance directly, while the in-container smart-prompts uses
    NEOHIVE_MCP_URL). Returns a short log suffix; never raises (a write-path hiccup must
    not fail the instance)."""
    try:
        host_base = os.environ.get("NEOHIVE_HOST_BASE", "http://127.0.0.1:3577")
        client = NeoHiveClient(base=host_base, project=project,
                               cf_id=os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_ID"),
                               cf_secret=os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_SECRET"))
        rr = reflect_and_store(client, inst, transcript, diff, status)
        (inst_dir / "reflection.json").write_text(json.dumps(rr.as_dict(), indent=2))
        return f" reflect(stored={rr.stored},blocked={rr.blocked}{',err' if rr.error else ''})"
    except Exception as e:  # noqa: BLE001 — writes must never fail the instance
        sys.stderr.write(f"[run] reflect-and-store failed: {e}\n")
        return " reflect(failed)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run opencode per SWE-bench instance, per arm (in-container).")
    ap.add_argument("instance_ids", nargs="+")
    ap.add_argument("--arm", choices=["a", "b"], required=True)
    ap.add_argument("--model", required=True, help="e.g. openrouter/z-ai/glm-4.6")
    ap.add_argument("--timeout", type=int, default=1200, help="per-instance opencode wall-clock budget (s)")
    ap.add_argument("--output", default="")
    ap.add_argument("--compounding", action="store_true",
                    help="HIVE-334/335: persistent-career mode — cross-hive recall within the career "
                         "project + filter-gated reflect-and-store after each task (arm b only).")
    ap.add_argument("--twin", action="store_true",
                    help="HIVE-338: memoryless twin — same arm-b scaffold + cross-hive recall but "
                         "writes OFF, so the experience pool never accumulates (arm b only).")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("set OPENROUTER_API_KEY")
    if (args.compounding or args.twin) and args.arm != "b":
        raise SystemExit("--compounding/--twin require --arm b")
    if args.compounding and args.twin:
        raise SystemExit("--compounding and --twin are mutually exclusive")
    project = os.environ.get("NEOHIVE_PROJECT")
    ocbin = opencode_bin()
    nodedir = node_dir()
    slug = args.model.replace("/", "_").replace(":", "_").replace(".", "_")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output) if args.output else HERE / "results" / f"arm{args.arm}-{slug}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run] output -> {out_dir}")

    for iid in args.instance_ids:
        try:
            run_instance(iid, args.arm, args.model, ocbin, nodedir, out_dir, args.timeout, project,
                         compounding=args.compounding, twin=args.twin)
        except Exception as e:  # noqa: BLE001 — keep going across instances
            print(f"[run] ERROR on {iid}: {e}", file=sys.stderr)

    print(f"[run] done. predictions -> {out_dir / 'preds.json'}")
    print(f"[run] grade: ./grade_swebench.sh {out_dir / 'preds.json'} arm{args.arm}-{slug}-{stamp} 2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
