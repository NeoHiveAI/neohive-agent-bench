#!/usr/bin/env python3
"""
HIVE-265/268 — Run opencode on SWE-bench instances, per arm, INSIDE the container.

Arm A = opencode + OpenRouter model, no NeoHive. Arm B = identical + NeoHive wired
as an MCP server (config/opencode-arm-b.json). The agent uses NeoHive's MCP tools
natively — the faithful end-user experience. Both arms run opencode inside the
per-instance SWE-bench image so the agent has the repo's deps and can run tests.

Per instance:
  - (Arm B) isolate + index: delete any existing hives in the project, then
    `index_instance.py <id>` to index repo@base_commit into a fresh hive.
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


# ---- Arm-B NeoHive hive lifecycle (temporal isolation: one hive in the project at a time) ----

def project_hives() -> list[dict]:
    res = idx._neohive_req("GET", "/api/hives")
    return res.get("items", res if isinstance(res, list) else [])


def write_routing(cfgdir: Path, repo: str, base_commit: str, hive_id: str) -> None:
    """Generate the NeoHive HIVE-routing table (the generate-agents-md "topology
    block" idea) and register it as an opencode instruction. Multiple repos coexist
    in one project (real NeoHive usage); this tells the agent which hive holds
    /testbed's repo and to scope recall to it — routing + the contamination guard in
    one. No per-run hive teardown."""
    rows = []
    for h in project_hives():
        if h.get("type") != "repo":
            continue
        nm = h.get("name", "")
        mark = "  ← THIS repo (/testbed)" if h.get("id") == hive_id else ""
        rows.append(f"| `{h.get('id')}` | {nm} |{mark} |")
    table = "\n".join(rows) if rows else "| (none) | | |"
    routing = f"""# NeoHive hive routing (this project)

This NeoHive project indexes one repository per hive, each pinned at a fixed commit.
The repository in your working directory (`/testbed`) is **{repo} @ {base_commit}**,
indexed in hive **`{hive_id}`**.

When you call the NeoHive `memory_recall` / `memory_context` MCP tools, ALWAYS pass
`hive: "{hive_id}"` so results come only from this repository's index (other hives in
this project hold unrelated repos / commits).

| hive id | name (instance) | |
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


NEOHIVE_TOOLS = ["neohive_memory_recall", "neohive_memory_context", "neohive_memory_store",
                 "neohive_list_hives", "neohive_memory_stats"]


def parse_usage(events_text: str) -> dict:
    """Best-effort NeoHive-usage counts from opencode --format json events + logs.
    Counts are raw occurrences of each tool name (approximate — a call emits several
    events) plus the smart-prompts injection marker; the point is to PROVE usage."""
    import re
    usage = {t: len(re.findall(re.escape(f'"{t}"'), events_text)) for t in NEOHIVE_TOOLS}
    usage["explore_neohive_dispatch"] = len(re.findall("explore-neohive", events_text))
    usage["smart_context_injections"] = events_text.count("NeoHive smart context")
    usage["used_neohive"] = any(v for k, v in usage.items() if k != "used_neohive")
    return usage


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
    """Assemble the container's /root/.config/opencode/ contents for this arm.

    Arm A: just opencode.json (permissions, no NeoHive). Arm B: opencode.json
    (permissions + MCP + instructions + plugin refs) plus the full faithful NeoHive
    setup (instructions/, plugin/ x2, agents/, skill/) from config/neohive-opencode/.
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


def run_instance(instance_id: str, arm: str, model: str, ocbin: str, out_dir: Path,
                 timeout: int, project: str | None) -> None:
    inst = idx.load_instance(instance_id)
    image = image_for(instance_id)
    inst_dir = out_dir / instance_id
    inst_dir.mkdir(parents=True, exist_ok=True)
    hive_id = None

    cfgdir = build_config_dir(arm, project)
    if arm == "b":
        hive_id = index_instance(instance_id)  # idempotent; hives persist (multi-repo project)
        write_routing(cfgdir, inst["repo"], inst["base_commit"], hive_id)

    print(f"[run] arm={arm} {instance_id} model={model} image={image}" + (f" hive={hive_id}" if hive_id else ""))
    cid = docker("run", "-d", "--rm", "-v", f"{ocbin}:/opt/oc/opencode:ro",
                 image, "sleep", str(timeout + 600)).stdout.strip()
    try:
        docker("exec", cid, "mkdir", "-p", "/root/.config/opencode")
        docker("cp", f"{cfgdir}/.", f"{cid}:/root/.config/opencode/")
        # smart-prompts shells out to `opencode` for the rewriter; put it on PATH.
        docker("exec", cid, "ln", "-sf", "/opt/oc/opencode", "/usr/local/bin/opencode", check=False)

        env_flags = ["-e", "OPENROUTER_API_KEY"]
        if arm == "b":
            env_flags += ["-e", "NEOHIVE_CF_ACCESS_CLIENT_ID", "-e", "NEOHIVE_CF_ACCESS_CLIENT_SECRET",
                          "-e", f"NEOHIVE_HIVE={hive_id}"]  # scope recall + smart-prompts to this hive
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
        print(f"[run] {instance_id} arm={arm} status={status} patch_len={len(diff)}{usage_str}")
    finally:
        docker("rm", "-f", cid, check=False)
    # No hive teardown: hives persist for reuse (idempotent index) + the multi-repo project.


def update_preds(path: Path, instance_id: str, model: str, patch: str) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    data[instance_id] = {"model_name_or_path": model, "instance_id": instance_id, "model_patch": patch}
    path.write_text(json.dumps(data, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="Run opencode per SWE-bench instance, per arm (in-container).")
    ap.add_argument("instance_ids", nargs="+")
    ap.add_argument("--arm", choices=["a", "b"], required=True)
    ap.add_argument("--model", required=True, help="e.g. openrouter/z-ai/glm-4.6")
    ap.add_argument("--timeout", type=int, default=1200, help="per-instance opencode wall-clock budget (s)")
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("set OPENROUTER_API_KEY")
    project = os.environ.get("NEOHIVE_PROJECT")
    ocbin = opencode_bin()
    slug = args.model.replace("/", "_").replace(":", "_").replace(".", "_")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output) if args.output else HERE / "results" / f"arm{args.arm}-{slug}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run] output -> {out_dir}")

    for iid in args.instance_ids:
        try:
            run_instance(iid, args.arm, args.model, ocbin, out_dir, args.timeout, project)
        except Exception as e:  # noqa: BLE001 — keep going across instances
            print(f"[run] ERROR on {iid}: {e}", file=sys.stderr)

    print(f"[run] done. predictions -> {out_dir / 'preds.json'}")
    print(f"[run] grade: ./grade_swebench.sh {out_dir / 'preds.json'} arm{args.arm}-{slug}-{stamp} 2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
