#!/usr/bin/env python3
"""HIVE-341 — loader + guard for the pinned model set and seeds.

The one invariant this enforces in code: the smart-prompts rewriter AND the
reflect-and-store model are BOTH pinned to GLM-4.6 across every arm, so the helper LLM
is never a per-arm confound — the only thing that varies between arms is the agent
model. Reads models.json; stdlib only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXED_HELPER = "z-ai/glm-4.6"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class ModelConfigError(ValueError):
    pass


def load(path: str | Path | None = None) -> dict:
    cfg = json.loads(Path(path or (HERE / "models.json")).read_text())
    _validate(cfg)
    return cfg


def agent_model_slugs(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load()
    return [m["openrouter"] for m in cfg["agent_models"]]


def all_pinned_slugs(cfg: dict | None = None) -> list[str]:
    """Every distinct OpenRouter slug the run touches: the agent models plus the fixed
    GLM-4.6 helper (rewriter == reflect, so it collapses to one). Order-preserving dedup
    so the report lists them agent-first. This is the "five slugs" HIVE-341 verifies."""
    cfg = cfg or load()
    slugs = agent_model_slugs(cfg)
    for extra in (cfg.get("smart_prompts_rewriter", {}).get("openrouter"),
                  cfg.get("reflect_model", {}).get("openrouter")):
        if extra and extra not in slugs:
            slugs.append(extra)
    return slugs


def verify_slugs_openrouter(cfg: dict | None = None, api_key: str | None = None,
                            timeout: int = 30) -> dict[str, bool]:
    """Check each pinned slug against OpenRouter's live model list. Returns slug -> bool.

    Uses the catalogue endpoint (a plain GET that consumes NO credits), so it proves the
    slug RESOLVES without touching the account balance — deliberately separate from the
    funding check (memory #393: a valid slug does not prove the account can afford a real
    32k-max_tokens agent run). An API key is optional for the public model list but sent
    when present."""
    import urllib.request

    cfg = cfg or load()
    headers = {"User-Agent": "neohive-agent-bench/0.1"}
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(OPENROUTER_MODELS_URL, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        catalogue = json.loads(r.read().decode())
    available = {m.get("id") for m in catalogue.get("data", [])}
    return {slug: (slug in available) for slug in all_pinned_slugs(cfg)}


def _validate(cfg: dict) -> None:
    if not cfg.get("agent_models"):
        raise ModelConfigError("no agent_models pinned")
    rewriter = cfg.get("smart_prompts_rewriter", {}).get("openrouter")
    reflect = cfg.get("reflect_model", {}).get("openrouter")
    if rewriter != FIXED_HELPER:
        raise ModelConfigError(f"smart-prompts rewriter must be fixed at {FIXED_HELPER}, got {rewriter}")
    if reflect != FIXED_HELPER:
        raise ModelConfigError(f"reflect model must be fixed at {FIXED_HELPER}, got {reflect}")
    if not cfg.get("seeds"):
        raise ModelConfigError("no seeds pinned")


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Pinned model set loader + slug verifier (HIVE-341).")
    ap.add_argument("--verify", action="store_true",
                    help="Check every pinned slug resolves on OpenRouter's live model list "
                         "(GET, no credits spent — does NOT prove the account is funded).")
    args = ap.parse_args()

    c = load()
    print("agent models:", agent_model_slugs(c))
    print("fixed helper (rewriter+reflect):", c["smart_prompts_rewriter"]["openrouter"])
    print("seeds:", c["seeds"], "replications:", c.get("replications_per_condition"))

    if args.verify:
        print(f"\nverifying {len(all_pinned_slugs(c))} pinned slug(s) against {OPENROUTER_MODELS_URL} ...")
        results = verify_slugs_openrouter(c)
        for slug, ok in results.items():
            print(f"  [{'OK ' if ok else 'MISS'}] {slug}")
        missing = [s for s, ok in results.items() if not ok]
        if missing:
            print(f"\n{len(missing)} slug(s) do NOT resolve: {missing}")
            sys.exit(1)
        print(f"\nall {len(results)} slug(s) resolve.")
