#!/usr/bin/env python3
"""Check every triggers/<suite>.json against config/runners.json.

A trigger file must parse, name only runner ids that exist, use a known arch,
and stay within the iteration cap. Run by .github/workflows/check.yml on every
push that touches triggers, config or the harness scripts.
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("plan", ROOT / "bin" / "plan.py")
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)

KEYS = {"runners", "arch", "iterations", "note"}


def main() -> int:
    ids = {r["id"] for r in json.loads((ROOT / "config" / "runners.json").read_text())["runners"]}
    workflows = {p.stem for p in (ROOT / ".github" / "workflows").glob("*.yml")}
    errors = []
    for path in sorted((ROOT / "triggers").glob("*.json")):
        suite = path.stem
        where = path.relative_to(ROOT)
        try:
            trigger = json.loads(path.read_text())
        except Exception as exc:
            errors.append(f"{where}: invalid JSON ({exc})")
            continue
        if suite not in workflows:
            errors.append(f"{where}: no workflow .github/workflows/{suite}.yml")
        extra = set(trigger) - KEYS
        if extra:
            errors.append(f"{where}: unknown keys {sorted(extra)}")
        runners = str(trigger.get("runners", "default")).strip()
        if runners not in ("", "default", "all"):
            unknown = sorted({i.strip() for i in runners.split(",") if i.strip()} - ids)
            if unknown:
                errors.append(f"{where}: unknown runner ids {', '.join(unknown)}")
        if str(trigger.get("arch", "all")) not in plan.ARCHES:
            errors.append(f"{where}: arch must be one of {', '.join(plan.ARCHES)}")
        try:
            iterations = int(trigger.get("iterations") or plan.default_iterations(suite))
        except (TypeError, ValueError):
            errors.append(f"{where}: iterations must be an integer")
        else:
            if not 1 <= iterations <= plan.max_iterations(suite):
                errors.append(f"{where}: iterations must be 1 to {plan.max_iterations(suite)}")
        if plan.trigger_at_rest(suite, trigger):
            print(f"{where}: at rest")
        else:
            # Not an error (this push may be the request), but a file left like
            # this runs its subset again on the next unrelated edit.
            print(f"::warning file={where}::{where} requests a run on push; reset it to its at-rest content once the run has started")
    for e in errors:
        print(f"::error::{e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
