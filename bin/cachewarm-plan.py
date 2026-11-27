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
import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from plan import merge_label, unique_runs_on  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--suite", default="cachewarm", help="suite name recorded in plan.json")
    ap.add_argument("--label-suites", default="cache", help="suiteLabel entries merged into RunsOn labels")
    ap.add_argument("--trigger", default="", help="trigger file; its `variants` list adds pairs such as runson-m8a-x64+sccache")
    args = ap.parse_args()
    path = pathlib.Path(args.plan)
    plan = json.loads(path.read_text())
    variants = {}
    if args.trigger and os.environ.get("EVENT", "push") == "push":
        try:
            for v in json.loads(pathlib.Path(args.trigger).read_text()).get("variants") or []:
                base, _, name = v.partition("+")
                variants.setdefault(base, []).append(name)
        except FileNotFoundError:
            pass
    catalog = {r["id"]: r for r in json.loads((ROOT / "config" / "runners.json").read_text())["runners"]}
    run_id = os.environ.get("GITHUB_RUN_ID", "0")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    runners, jobs, pairs = [], [], []
    expanded = []
    for job in plan["jobs"]:
        expanded.append((job, ""))
        for name in variants.get(job["id"], []):
            expanded.append((job, name))
    for job, variant in expanded:
        base = catalog[job["id"]]
        actions = base.get("cacheActions") or ["actions/cache"]
        action = next((a for a in actions if a != "actions/cache"), "actions/cache")
        label = job["label"]
        extras = [x for x in ((base.get("suiteLabel") or {}).get(n) for n in args.label_suites.split(",")) if x]
        vid = f"{job['id']}+{variant}" if variant else job["id"]
        row = {}
        for stage in ("cold", "warm"):
            rid = f"{vid}.{stage}"
            lab = label
            if isinstance(lab, str) and lab.startswith("runs-on="):
                # unique runs-on= per job, cold and warm included
                lab = unique_runs_on(f"runs-on={run_id},{lab.split(',', 1)[1]}", f"{rid}-{job['iteration']}")
                for extra in extras:
                    lab = merge_label(lab, extra)
            key = f"{rid}--{job['iteration']}"
            jobs.append({**job, "key": key, "id": rid, "label": lab, "cacheAction": action, "stage": stage, "variant": variant})
            row[stage] = {"key": key, "label": json.dumps(lab)}
        pairs.append(
            {
                "key": f"{vid}--{job['iteration']}",
                "variant": variant,
                "provider": job["provider"],
                "coldKey": row["cold"]["key"],
                "warmKey": row["warm"]["key"],
                "coldLabel": row["cold"]["label"],
                "warmLabel": row["warm"]["label"],
                "cacheAction": action,
                "cacheKey": f"{args.suite}-{vid.replace('+', '-')}-{job['iteration']}-{run_id}-{attempt}",
            }
        )

    for r in plan["runners"]:
        for variant in [""] + variants.get(r["id"], []):
            vid = f"{r['id']}+{variant}" if variant else r["id"]
            for stage in ("cold", "warm"):
                runners.append({**r, "id": f"{vid}.{stage}", "baseId": r["id"], "stage": stage, "variant": variant})

    plan.update(suite=args.suite, runners=runners, jobs=jobs)
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
