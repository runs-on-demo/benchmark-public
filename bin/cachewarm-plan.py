#!/usr/bin/env python3
"""Turn a rust-suite plan into a cold/warm pair plan (experiment, branch only).

bin/plan.py --suite rust picks the runners (trigger file, dispatch inputs).
This rewrites its plan.json so every runner gives two rows:

  <id>.cold  cold build: the cache key is new (run id + attempt + runner), so the
             restore must miss; the build then saves its cache under that key.
  <id>.warm  warm rebuild on a fresh runner, right after its cold job: restores
             exactly what the cold job saved (and nothing else), then rebuilds.

Writes `matrix` (one entry per runner, carrying both keys) to GITHUB_OUTPUT.
RunsOn rows get the cache suite's label settings (extras=s3-cache), and a
runner with its own cache action (WarpBuilds/cache) uses it, as in cache.yml.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from plan import merge_label, unique_runs_on  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    path = pathlib.Path(sys.argv[1])
    plan = json.loads(path.read_text())
    catalog = {r["id"]: r for r in json.loads((ROOT / "config" / "runners.json").read_text())["runners"]}
    run_id = os.environ.get("GITHUB_RUN_ID", "0")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    runners, jobs, pairs = [], [], []
    for job in plan["jobs"]:
        base = catalog[job["id"]]
        actions = base.get("cacheActions") or ["actions/cache"]
        action = next((a for a in actions if a != "actions/cache"), "actions/cache")
        label = job["label"]
        extra = (base.get("suiteLabel") or {}).get("cache")
        row = {}
        for stage in ("cold", "warm"):
            rid = f"{job['id']}.{stage}"
            lab = label
            if isinstance(lab, str) and lab.startswith("runs-on="):
                # unique runs-on= per job, cold and warm included
                lab = unique_runs_on(f"runs-on={run_id},{lab.split(',', 1)[1]}", f"{rid}-{job['iteration']}")
                if extra:
                    lab = merge_label(lab, extra)
            key = f"{rid}--{job['iteration']}"
            jobs.append({**job, "key": key, "id": rid, "label": lab, "cacheAction": action, "stage": stage})
            row[stage] = {"key": key, "label": json.dumps(lab)}
        pairs.append(
            {
                "key": f"{job['id']}--{job['iteration']}",
                "provider": job["provider"],
                "coldKey": row["cold"]["key"],
                "warmKey": row["warm"]["key"],
                "coldLabel": row["cold"]["label"],
                "warmLabel": row["warm"]["label"],
                "cacheAction": action,
                "cacheKey": f"cachewarm-{job['id']}-{job['iteration']}-{run_id}-{attempt}",
            }
        )

    for r in plan["runners"]:
        for stage in ("cold", "warm"):
            runners.append({**r, "id": f"{r['id']}.{stage}", "baseId": r["id"], "stage": stage})

    plan.update(suite="cachewarm", runners=runners, jobs=jobs)
    path.write_text(json.dumps(plan, indent=2) + "\n")
    matrix = json.dumps({"include": pairs}, separators=(",", ":"))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"matrix={matrix}\n")
    else:
        print(json.dumps(pairs, indent=2))
    print(f"{len(pairs)} cold/warm pairs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
