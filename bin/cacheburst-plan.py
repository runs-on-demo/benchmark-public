#!/usr/bin/env python3
"""Plan the cache restore burst experiment (branch only).

Reads triggers/cacheburst.json (runner ids, burst size, cache size) and writes
`matrix` to GITHUB_OUTPUT: one entry per runner, with the labels of its seed
job, its single (baseline) restore and its burst of restores. RunsOn labels
get their own `runs-on=` value per job (bin/plan.py unique_runs_on) and the
cache suite's settings (extras=s3-cache).
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from plan import merge_label, unique_runs_on  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    trig = json.loads((ROOT / "triggers" / "cacheburst.json").read_text())
    ids = [s.strip() for s in os.environ.get("RUNNERS", "").split(",") if s.strip()] or [s.strip() for s in trig["runners"].split(",")]
    burst = int(os.environ.get("BURST") or trig.get("burst", 10))
    size = int(os.environ.get("SIZE_MIB") or trig.get("sizeMiB", 1536))
    if not 1 <= burst <= 20:
        raise SystemExit("burst must be 1 to 20")
    catalog = {r["id"]: r for r in json.loads((ROOT / "config" / "runners.json").read_text())["runners"]}
    run_id = os.environ.get("GITHUB_RUN_ID", "0")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    rows = []
    for rid in ids:
        r = catalog[rid]
        actions = r.get("cacheActions") or ["actions/cache"]
        action = next((a for a in actions if a != "actions/cache"), "actions/cache")
        label = r["label"]
        extra = (r.get("suiteLabel") or {}).get("cache")

        def lab(job: str):
            if isinstance(label, str) and label.startswith("runs-on="):
                out = unique_runs_on(f"runs-on={run_id},{label.split(',', 1)[1]}", f"{rid}-{job}")
                return merge_label(out, extra) if extra else out
            return label

        rows.append(
            {
                "id": rid,
                "provider": r["provider"],
                "cacheAction": action,
                "cacheKey": f"cacheburst-{rid}-{run_id}-{attempt}",
                "seedLabel": json.dumps(lab("seed")),
                "singleLabel": json.dumps(lab("single")),
                "burstLabels": json.dumps([lab(f"b{i}") for i in range(burst)]),
                "slots": json.dumps(list(range(burst))),
                "sizeMiB": size,
            }
        )
    matrix = json.dumps({"include": rows}, separators=(",", ":"))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"matrix={matrix}\n")
    else:
        print(json.dumps(rows, indent=2))
    print(f"{len(rows)} runners, burst {burst}, {size} MiB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
