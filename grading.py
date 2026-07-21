#!/usr/bin/env python3
"""HIVE-343 — grading discipline: stock harness, apply-failures in their own bucket.

Grading itself stays on the **stock** `swebench` harness (`grade_swebench.sh`, untouched
and identical for every arm). This module is a pure POST-PROCESSOR over swebench's own
output: it reads the run report + per-instance `report.json` and sorts every instance
into an explicit bucket so a *patch-apply failure* — a harness artefact where a
correct-but-differently-shaped patch fails to apply — is never silently counted as
`unresolved`. That lesson is from SWE-CL: if apply-failures land harder on the memory
arm they read as a memory regression when they are a grading artefact. Separating the
bucket keeps that from ever happening.

Buckets: ``resolved``, ``unresolved`` (applied, tests ran, not resolved),
``patch_apply_failed`` (patch existed but did not apply), ``empty_patch`` (agent
produced no patch — a genuine non-attempt), ``error`` (harness error).

The resolve rate the CL metrics consume uses ``gradeable = resolved + unresolved`` only
— apply-failures/errors are excluded from the denominator, not folded into unresolved.

Pure stdlib; the classifier is unit-tested with synthetic inputs.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

RESOLVED = "resolved"
UNRESOLVED = "unresolved"
APPLY_FAILED = "patch_apply_failed"
EMPTY_PATCH = "empty_patch"
ERROR = "error"
BUCKETS = [RESOLVED, UNRESOLVED, APPLY_FAILED, EMPTY_PATCH, ERROR]


@dataclass
class InstanceVerdict:
    instance_id: str
    bucket: str
    resolved: bool = False
    patch_applied: bool | None = None


def classify_instance(*, patch_is_empty: bool, is_error: bool,
                      patch_applied: bool | None, is_resolved: bool) -> str:
    """Decide an instance's bucket from normalized flags. Precedence matters: no patch
    at all first, then harness error, then apply-failure (can't be resolved if it never
    applied), then resolved, else unresolved."""
    if patch_is_empty:
        return EMPTY_PATCH
    if is_error:
        return ERROR
    if patch_applied is False:
        return APPLY_FAILED
    if is_resolved:
        return RESOLVED
    return UNRESOLVED


@dataclass
class GradingSummary:
    counts: dict = field(default_factory=dict)
    gradeable: int = 0          # resolved + unresolved
    submitted: int = 0          # all buckets
    resolve_rate: float = 0.0             # resolved / gradeable (HIVE-343 denominator)
    resolve_rate_submitted: float = 0.0   # resolved / submitted (transparency)
    verdicts: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "counts": self.counts, "gradeable": self.gradeable, "submitted": self.submitted,
            "resolve_rate": self.resolve_rate, "resolve_rate_submitted": self.resolve_rate_submitted,
            "verdicts": [{"instance_id": v.instance_id, "bucket": v.bucket} for v in self.verdicts],
        }


def summarize(verdicts: list[InstanceVerdict]) -> GradingSummary:
    counts = Counter(v.bucket for v in verdicts)
    resolved = counts.get(RESOLVED, 0)
    gradeable = resolved + counts.get(UNRESOLVED, 0)
    submitted = len(verdicts)
    return GradingSummary(
        counts={b: counts.get(b, 0) for b in BUCKETS},
        gradeable=gradeable,
        submitted=submitted,
        resolve_rate=(resolved / gradeable) if gradeable else 0.0,
        resolve_rate_submitted=(resolved / submitted) if submitted else 0.0,
        verdicts=verdicts,
    )


# --------------------------------------------------------------------------- #
# Parsing the stock swebench output (I/O; buckets via classify_instance)
# --------------------------------------------------------------------------- #

def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def classify_from_reports(instance_ids: list[str], top_report: dict,
                          per_instance: dict) -> list[InstanceVerdict]:
    """Bucket each instance from swebench's top-level report + per-instance reports.

    `top_report` is the stock run report (`resolved_ids`, `error_ids`, `empty_patch_ids`,
    ...). `per_instance` maps instance_id -> that instance's stock `report.json` payload
    (with `patch_successfully_applied` / `patch_is_None` / `resolved`). Either source may
    be partial; flags are combined defensively."""
    resolved_ids = set(top_report.get("resolved_ids", []))
    error_ids = set(top_report.get("error_ids", []))
    empty_ids = set(top_report.get("empty_patch_ids", []))
    verdicts = []
    for iid in instance_ids:
        pi = per_instance.get(iid, {})
        # swebench per-instance report nests under the instance id sometimes
        if iid in pi and isinstance(pi[iid], dict):
            pi = pi[iid]
        patch_is_none = bool(pi.get("patch_is_None")) or (iid in empty_ids)
        applied = pi.get("patch_successfully_applied")
        applied = None if applied is None else bool(applied)
        is_resolved = bool(pi.get("resolved")) or (iid in resolved_ids)
        is_error = iid in error_ids
        bucket = classify_instance(patch_is_empty=patch_is_none, is_error=is_error,
                                   patch_applied=applied, is_resolved=is_resolved)
        verdicts.append(InstanceVerdict(iid, bucket, resolved=is_resolved, patch_applied=applied))
    return verdicts


def grade_report(report_path: str | Path, logs_dir: str | Path | None = None,
                 instance_ids: list[str] | None = None) -> GradingSummary:
    """Read a stock swebench run report (and per-instance reports under `logs_dir` if
    given) and produce the bucketed summary. `logs_dir` is
    ``logs/run_evaluation/<run_id>/<model>/`` (each instance has a ``report.json``)."""
    top = _load_json(Path(report_path))
    ids = instance_ids or sorted(
        set(top.get("submitted_ids") or top.get("completed_ids") or [])
        | set(top.get("resolved_ids", [])) | set(top.get("unresolved_ids", []))
        | set(top.get("error_ids", [])) | set(top.get("empty_patch_ids", []))
    )
    per_instance: dict = {}
    if logs_dir:
        for iid in ids:
            rp = Path(logs_dir) / iid / "report.json"
            if rp.exists():
                per_instance[iid] = _load_json(rp)
    return summarize(classify_from_reports(ids, top, per_instance))
