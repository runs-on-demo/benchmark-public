#!/usr/bin/env python3
"""Merge the cache restore burst records (one JSON per restore job) into one
file, with per-runner stats: the single restore, and the burst's median, p90,
max, the spread of its start times and its aggregate throughput."""
import json
import pathlib
import statistics
import sys


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def main() -> int:
    raw, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    recs = [json.loads(p.read_text()) for p in sorted(raw.glob("*.json"))] if raw.exists() else []
    summary = {}
    for rid in sorted({r["runner"] for r in recs}):
        rs = [r for r in recs if r["runner"] == rid]
        single = next((r for r in rs if r["stage"] == "single"), None)
        burst = [r for r in rs if r["stage"] == "burst" and r["ok"]]
        failed = [r for r in rs if r["stage"] == "burst" and not r["ok"]]
        secs = [r["seconds"] for r in burst]
        s = {
            "single": single["seconds"] if single and single["ok"] else None,
            "burstOk": len(burst),
            "burstFailed": len(failed),
        }
        if burst:
            starts = [r["start"] for r in burst]
            ends = [r["end"] for r in burst]
            gib = sum(r["bytes"] for r in burst) / 2**30
            s.update(
                median=round(statistics.median(secs), 2),
                p90=pct(secs, 0.9),
                max=round(max(secs), 2),
                min=round(min(secs), 2),
                startSpread=round(max(starts) - min(starts), 2),
                late=sum(1 for r in burst if r["ready"] > r["release"]),
                wallSeconds=round(max(ends) - min(starts), 2),
                aggregateMiBps=round(gib * 1024 / (max(ends) - min(starts)), 1),
                perJobMiBps=round(statistics.median(r["bytes"] / 2**20 / r["seconds"] for r in burst), 1),
            )
            if s["single"]:
                s["medianVsSingle"] = round(s["median"] / s["single"], 2)
        summary[rid] = s
        print(rid, json.dumps(s))
    out.write_text(json.dumps({"summary": summary, "records": recs}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
