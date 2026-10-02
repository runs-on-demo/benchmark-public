#!/usr/bin/env python3
"""RunsOn's own cost estimate for each RunsOn job of this run.

RunsOn's control plane logs one `job_summary` event per completed job
(estimated EC2 + EBS cost from what actually ran: instance type, spot or
on-demand, the AZ's spot price over the job's window, EBS volumes, AWS's
60-second minimum). This reads those events for the run's RunsOn jobs with
CloudWatch Logs Insights and writes

    { "<job id>": { "usd", "ec2Usd", "ebsUsd", "lifecycle", "interrupted",
                    "instanceType", "durationSeconds", "billedSeconds",
                    "ec2HourlyUsd", "ebsHourlyUsd" } }

(the last three only when the control plane logs them: the billed instance
time and the rates its estimate used)

which bin/collect.py --costs merges into the results. Only jobs planned on a
RunsOn runner that started are looked up. A summary lands shortly after its
job ends, so the lookup retries the missing ones for up to --wait seconds.

It never fails the publish: on any error it warns, writes what it has, and
exits 0. AWS credentials come from the environment (the publish job assumes
a read-only role through GitHub OIDC: infra/runs-on-cost-reader.yml).

Usage: bin/runs-on-costs.py --plan plan.json --log-group /aws/ecs/<stack>/runs-on-worker --out costs.json
       bin/runs-on-costs.py --job-ids 1,2,3 --log-group ... --out costs.json   # no GitHub lookup
"""
import argparse
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("collect", ROOT / "bin" / "collect.py")
collect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect)

PROVIDER = "RunsOn"
BATCH = 100  # job ids per query; keeps the query string short
FIELDS = (
    "job_id",
    "estimated_cost_usd",
    "estimated_ec2_cost_usd",
    "estimated_ebs_cost_usd",
    "instance_lifecycle",
    "interrupted",
    "instance_type",
    "job_duration_seconds",
    # The instance time and rates the estimate used, when the control plane logs them.
    "billed_seconds",
    "ec2_hourly_usd",
    "ebs_hourly_usd",
)


def runs_on_jobs(plan, jobs):
    """{job id: created_at} for the planned RunsOn jobs that started. `jobs`
    is the run's jobs keyed like collect.py keys them."""
    out = {}
    for planned in plan.get("jobs", []):
        if planned.get("provider") != PROVIDER:
            continue
        job = jobs.get(planned["key"])
        if collect.job_started(job) and job.get("id"):
            out[int(job["id"])] = job.get("created_at")
    return out


def build_queries(job_ids, batch=BATCH):
    ids = sorted({int(i) for i in job_ids})
    for n in range(0, len(ids), batch):
        chunk = ", ".join(str(i) for i in ids[n : n + batch])
        yield (
            f"fields @timestamp, {', '.join(FIELDS)}"
            f' | filter metric_type = "job_summary" and job_id in [{chunk}]'
            " | sort @timestamp desc | limit 10000"
        )


def _num(value, digits=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if digits is not None else v


def _true(value):
    return str(value).strip().lower() in ("true", "1")


def parse_results(response):
    """get-query-results JSON -> {job id (str): summary}. Rows are newest
    first; the newest summary of a job wins."""
    out = {}
    for row in response.get("results", []):
        f = {c.get("field"): c.get("value") for c in row}
        jid = f.get("job_id")
        if jid is None:
            continue
        try:
            jid = str(int(float(jid)))
        except ValueError:
            continue
        if jid in out:
            continue
        usd = _num(f.get("estimated_cost_usd"), 6)
        if usd is None:
            continue
        duration = _num(f.get("job_duration_seconds"))
        out[jid] = {
            "usd": usd,
            "ec2Usd": _num(f.get("estimated_ec2_cost_usd"), 6),
            "ebsUsd": _num(f.get("estimated_ebs_cost_usd"), 6),
            "lifecycle": f.get("instance_lifecycle") or None,
            "interrupted": _true(f.get("interrupted")),
            "instanceType": f.get("instance_type") or None,
            "durationSeconds": round(duration) if duration is not None else None,
            "billedSeconds": _num(f.get("billed_seconds"), 1),
            "ec2HourlyUsd": _num(f.get("ec2_hourly_usd"), 6),
            "ebsHourlyUsd": _num(f.get("ebs_hourly_usd"), 6),
        }
    return out


def aws(*args, region=None):
    cmd = ["aws", *args, "--output", "json"]
    if region:
        cmd += ["--region", region]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"aws {args[0]} {args[1]}: {res.stderr.strip()[:300]}")
    return json.loads(res.stdout or "{}")


def insights_query(log_group, query, start, end, region=None, poll=2.0, timeout=120):
    """Run one Logs Insights query and return get-query-results' JSON."""
    qid = aws(
        "logs", "start-query",
        "--log-group-name", log_group,
        "--start-time", str(int(start)),
        "--end-time", str(int(end)),
        "--query-string", query,
        region=region,
    )["queryId"]
    deadline = time.monotonic() + timeout
    while True:
        res = aws("logs", "get-query-results", "--query-id", qid, region=region)
        status = res.get("status")
        if status == "Complete":
            return res
        if status in ("Failed", "Cancelled", "Timeout", "Unknown"):
            raise RuntimeError(f"query {status.lower()}")
        if time.monotonic() > deadline:
            try:
                aws("logs", "stop-query", "--query-id", qid, region=region)
            except Exception:
                pass
            raise RuntimeError("query did not complete in time")
        time.sleep(poll)


def lookup(job_ids, start, wait, interval, query_fn, now=time.time, sleep=time.sleep):
    """Summaries for `job_ids`, retrying the missing ones until `wait`
    seconds have passed. `query_fn(query, start, end)` returns
    get-query-results JSON."""
    found = {}
    deadline = now() + wait
    while True:
        missing = [i for i in job_ids if str(i) not in found]
        for query in build_queries(missing):
            try:
                found.update(parse_results(query_fn(query, start, now() + 300)))
            except Exception as err:
                # Access denied, a missing log group: retrying won't help.
                print(f"::warning::RunsOn cost query failed: {err}", file=sys.stderr)
                return found, [i for i in job_ids if str(i) not in found]
        missing = [i for i in job_ids if str(i) not in found]
        if not missing or now() + interval > deadline:
            return found, missing
        print(f"{len(missing)} RunsOn job summaries not in yet, retrying in {interval}s", file=sys.stderr)
        sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan")
    parser.add_argument("--job-ids", default="", help="comma-separated job ids (skips the GitHub lookup)")
    parser.add_argument("--log-group", default=os.environ.get("RUNS_ON_LOG_GROUP", ""))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    parser.add_argument("--wait", type=int, default=360, help="seconds to keep retrying missing summaries")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    found, missing = {}, []
    try:
        if not args.log_group:
            raise RuntimeError("no log group (RUNS_ON_LOG_GROUP)")
        if args.job_ids:
            created = {int(i): None for i in args.job_ids.split(",") if i.strip()}
        else:
            plan = json.loads(pathlib.Path(args.plan).read_text())
            repo = os.environ["GITHUB_REPOSITORY"]
            run_id = os.environ["GITHUB_RUN_ID"]
            attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            jobs = {collect.job_key(j["name"]): j for j in collect.run_jobs(repo, run_id, attempt)}
            created = runs_on_jobs(plan, jobs)
        if created:
            stamps = [collect.parse_ts(c).timestamp() for c in created.values() if c]
            # Summaries are logged when a job ends: from the first job's
            # creation on (a day back when unknown).
            start = (min(stamps) if stamps else time.time() - 86400) - 900
            ids = sorted(created)
            found, missing = lookup(
                ids, start, args.wait, args.interval,
                lambda q, s, e: insights_query(args.log_group, q, s, e, region=args.region),
            )
    except Exception as err:
        print(f"::warning::RunsOn job costs unavailable: {err}", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
    total = sum(v["usd"] for v in found.values())
    print(f"RunsOn job costs: {len(found)} found, {len(missing)} missing, ${total:.4f} in all -> {out}")
    if missing:
        print(f"::warning::no RunsOn job summary for {len(missing)} job(s): {', '.join(map(str, missing))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
