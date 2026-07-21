#!/usr/bin/env python3
"""HIVE-341 — loader + guard for the pinned model set and seeds.

The one invariant this enforces in code: the smart-prompts rewriter AND the
reflect-and-store model are BOTH pinned to GLM-4.6 across every arm, so the helper LLM
is never a per-arm confound — the only thing that varies between arms is the agent
model. Reads models.json; stdlib only.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXED_HELPER = "z-ai/glm-4.6"


class ModelConfigError(ValueError):
    pass


def load(path: str | Path | None = None) -> dict:
    cfg = json.loads(Path(path or (HERE / "models.json")).read_text())
    _validate(cfg)
    return cfg


def agent_model_slugs(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load()
    return [m["openrouter"] for m in cfg["agent_models"]]


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
    c = load()
    print("agent models:", agent_model_slugs(c))
    print("fixed helper (rewriter+reflect):", c["smart_prompts_rewriter"]["openrouter"])
    print("seeds:", c["seeds"], "replications:", c.get("replications_per_condition"))
