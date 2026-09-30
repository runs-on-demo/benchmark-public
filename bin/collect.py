#!/usr/bin/env python3
"""Merge one workflow run into results/<suite>/<date>-<run>-<attempt>.json.

Timings come from the GitHub Jobs API rather than from inside the job, so a
runner can't misreport its own speed:

- queueSeconds  = job started_at - job created_at
- phases        = every step whose name starts with "phase: "
- workloadSeconds = sum of phase durations (setup/teardown steps excluded)

Host facts (CPU model, disks, EC2 metadata) and hardware metrics come from the
artifacts each job uploads: they are self-reported by the job. Runners in the
plan that never produced a job are reported as "unavailable" so a missing
provider is visible, not silently absent.

Status of a job that started:
- success: every phase succeeded (a harness step failing afterwards doesn't
  change that).
- failure: a phase failed, was killed (OOM: the step reports "cancelled" inside
  a failed job), timed out, or never finished because the runner was lost
  ("lost communication", "shutdown signal"). `reason` says which, from the
  job's annotations, and `logTail` keeps the last lines of the failing step.
- cancelled: only when the run was cancelled (the job's conclusion is
  "cancelled", no phase was left unfinished, and the annotations, which could
  be read, don't say the runner was lost or the job timed out). If they
  couldn't be read, the job is a failure with reason "unknown (annotations
  unavailable)" rather than dropped.

RunsOn results get `cost` (RunsOn's own per-job estimate, read from its
control plane by bin/runs-on-costs.py, passed with --costs) when there is one;
a failed job on an interrupted spot instance gets reason "spot interruption".

Files listed in results/exclusions.json stay out of results/index.json.

Usage: bin/collect.py --plan artifacts/plan/plan.json --artifacts artifacts --out run.json [--costs costs.json]
       bin/collect.py --reindex            # rebuild results/index.json only
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PHASE_PREFIX = "phase: "


def api(path):
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def job_log(repo, job_id):
    """The job's plain-text log. The API answers with a redirect to a signed
    URL, which is fetched without the token."""
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repo}/actions/jobs/{job_id}/logs"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        try:
            resp = urllib.request.build_opener(_NoRedirect).open(req, timeout=30)
            return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if err.code not in (301, 302, 303, 307, 308) or not err.headers.get("Location"):
                return None
            with urllib.request.urlopen(err.headers["Location"], timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Likely secrets in a log line. The first group, when a pattern keeps one, is
# the key or scheme, left in place so the line still reads.
SECRETS = [
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), False),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), False),
    (re.compile(r"(?i)\b(bearer\s+|token\s+|basic\s+)[A-Za-z0-9._~+/=-]{16,}"), True),
    (re.compile(r"(?i)([a-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key|credential)[a-z0-9_-]*\s*[=:]\s*)\S+"), True),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+(?=@)"), True),
    (re.compile(r"(?i)([?&](?:sig|signature|token|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s]+"), True),
    # Infrastructure names a provider prints (RunsOn's pre.sh and every step's
    # env block): bucket names, stack names, instance ids.
    (re.compile(r"\b(RUNS_ON_(?:S3_[A-Z0-9_]+|INSTANCE_ID|RUNNER_NAME|STACK_NAME)\s*[=:]\s*)\S+"), True),
    (re.compile(r"[a-z0-9-]*s3bucket[a-z0-9-]*", re.I), False),
    (re.compile(r"\b(i-)[0-9a-f]{8,17}\b"), True),
]
# Log groups that hold the step header (script, shell, env block) or a
# provider's environment dump, not output. Their title line is kept, the rest
# dropped. Groups the workload opens itself (turbo, cargo) are output and stay.
HEADER_GROUP = re.compile(
    r"^##\[group\](?:Run |.*(?:environment variables|Runner details|Job identity|Timings|GITHUB_TOKEN Permissions))",
    re.I,
)
ENV_LINE = re.compile(r"^\s+[A-Za-z_][A-Za-z0-9_]*: ")
TAIL_LINES = 40


def redact(line):
    for pattern, keep in SECRETS:
        line = pattern.sub((lambda m: m.group(1) + "***") if keep else (lambda m: "***"), line)
    return line


def log_tail(text, step, lines=TAIL_LINES):
    """The last lines the failing step wrote, located by the step's start and
    end times (the job log isn't split by step). Timestamps and colour codes
    are stripped, likely secrets redacted, long lines cut."""
    if not text:
        return None
    start = (step or {}).get("started_at")
    end = (step or {}).get("completed_at")
    start = start[:19] if start else None
    end = end[:19] if end else None
    picked = []
    in_header = in_env = False
    # The log starts with a byte-order mark, which would hide the first
    # line's timestamp.
    for raw in text.lstrip("\ufeff").splitlines():
        m = LOG_TS.match(raw)
        ts = m.group(1) if m else None
        if ts and start and ts < start:
            continue
        if ts and end and ts > end:
            # Step times have 1 s resolution: keep the step's last second.
            break
        body = ANSI.sub("", raw[m.end():] if m else raw).rstrip()
        if in_header:
            in_header = not body.startswith("##[endgroup]")
            continue
        if HEADER_GROUP.match(body):
            in_header = True
            picked.append(body.replace("##[group]", "", 1))
            continue
        if in_env:
            if ENV_LINE.match(body):
                continue
            in_env = False
        if body == "env:":
            # An env block printed outside a group: its KEY: value lines follow.
            in_env = True
            continue
        picked.append(body)
    errors = [i for i, l in enumerate(picked) if "##[error]" in l]
    if errors:
        # Lines after the step's error belong to the next step (same second).
        picked = picked[: errors[-1] + 1]
    picked = [l for l in picked if l.strip()]
    if not picked and (start or end):
        # Nothing in the step's window: fall back to the end of the job log
        # (headers still dropped).
        return log_tail(text, None, lines)
    tail = [redact(l)[:300] for l in picked[-lines:]]
    return tail or None


def parse_ts(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds_between(a, b):
    a, b = parse_ts(a), parse_ts(b)
    if not a or not b:
        return None
    return max(0, round((b - a).total_seconds()))


def run_jobs(repo, run_id, attempt):
    jobs, page = [], 1
    while True:
        data = api(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        jobs.extend(data.get("jobs", []))
        if len(data.get("jobs", [])) < 100:
            return jobs
        page += 1


# Runner and toolchain chatter that says nothing about the result.
NOISE = re.compile(
    r"Node\.js \d+ (is|actions are) deprecated|punycode|set-output|save-state|^Process completed with exit code",
    re.I,
)
# Per-request identifiers that make identical failures look different.
IDS = re.compile(r"\s*(RequestId|Time):\S+")


def annotations(repo, job_id):
    """Errors and warnings the job raised (::error / ::warning, failed steps),
    so a failure can be read as "cache hit, then blob does not exist" instead
    of just "failed at cache restore"."""
    items = None
    for attempt in range(3):
        try:
            items = api(f"/repos/{repo}/check-runs/{job_id}/annotations?per_page=50")
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    if items is None:
        # Not the same as "no annotations": classify() won't call a job
        # cancelled on missing evidence.
        return None
    out = {"errors": [], "warnings": []}
    for a in items:
        msg = IDS.sub("", " ".join((a.get("message") or "").split()))[:300]
        if not msg or NOISE.search(msg):
            continue
        level = a.get("annotation_level")
        if level == "failure" and msg not in out["errors"]:
            out["errors"].append(msg)
        elif level == "warning" and msg not in out["warnings"]:
            out["warnings"].append(msg)
    # {} when the job raised none, None when they couldn't be read.
    return {k: v[:5] for k, v in out.items() if v}


# Annotation messages that say what ended a job.
RUNNER_LOST = re.compile(
    r"lost communication with the server|received a shutdown signal|hosted runner encountered an error",
    re.I,
)
TIMED_OUT = re.compile(r"exceeded the maximum execution time", re.I)
UNFINISHED = ("in_progress", "queued", "pending", "waiting", None)


def classify(conclusion, phases, notes):
    """(status, reason) for a job that started. See the module docstring.
    `notes` is None when the annotations couldn't be read, {} when there were
    none."""
    errors = " | ".join((notes or {}).get("errors", []))
    lost = RUNNER_LOST.search(errors)
    timed_out = TIMED_OUT.search(errors)
    unfinished = any(p["status"] in UNFINISHED for p in phases)
    if phases and all(p["status"] == "success" for p in phases):
        return "success", None
    if lost:
        return "failure", "runner lost"
    if timed_out:
        return "failure", "timed out"
    if unfinished:
        # A phase was still running when the job ended: the runner went away.
        # Cancelling a run marks the running step "cancelled", never leaves it
        # in progress, so this holds whatever the job's conclusion says.
        return "failure", "runner lost"
    if conclusion == "cancelled":
        if notes is None:
            # A timeout or a lost runner also ends as "cancelled"; without the
            # annotations there is no telling them apart from a cancelled run.
            return "failure", "unknown (annotations unavailable)"
        # Only cancelling the run ends a job this way without an annotation
        # saying otherwise. A cancelled run measured nothing.
        return "cancelled", "run cancelled before the workload finished"
    if conclusion == "success":
        return "success", None
    if any(p["status"] == "cancelled" for p in phases):
        # A step killed inside a failed job (out of memory, most often).
        return "failure", "step killed"
    return (conclusion or "failure"), None


def job_key(name):
    # Jobs inside a called workflow are named "<caller> / <name>".
    return name.rsplit(" / ", 1)[-1].strip()


def load_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return None


def job_started(job):
    """A runner picked the job up (a job cancelled or skipped before any
    runner took it never started)."""
    return bool(job and job.get("started_at")) and not (
        job.get("conclusion") in ("cancelled", "skipped") and not job.get("runner_name")
    )


def summarize_job(planned, job, artifacts_dir, repo=None, fetch_logs=True):
    art = artifacts_dir / f"bench-{planned['key']}"
    host = load_json(art / "host.json")
    metrics = load_json(art / "metrics.json")
    entry = {
        "key": planned["key"],
        "runnerId": planned["id"],
        "provider": planned["provider"],
        "arch": planned["arch"],
        "iteration": planned["iteration"],
    }
    if not job_started(job):
        entry["status"] = "unavailable"
        entry["reason"] = "no runner picked up the job" if job else "job missing from run"
        return entry

    phases, failed_phase, failed_step = [], None, None
    steps = job.get("steps", [])
    for step in steps:
        name = step.get("name", "")
        is_phase = name.startswith(PHASE_PREFIX)
        if is_phase and step.get("conclusion") == "skipped" and any(
            s.get("name") == name and s.get("conclusion") != "skipped" for s in steps
        ):
            # Alternative implementations of the same phase (one per cache
            # action): only the one that ran counts.
            continue
        state = step.get("conclusion") or step.get("status")
        if is_phase:
            phases.append(
                {
                    "name": name[len(PHASE_PREFIX):],
                    "seconds": seconds_between(step.get("started_at"), step.get("completed_at")),
                    "status": state,
                }
            )
        # The first step that failed, was killed (OOM, timeout: "cancelled"),
        # or was still running when the runner went away.
        bad = state == "failure" or state == "in_progress" or (
            is_phase and state in ("cancelled", "timed_out") + UNFINISHED
        )
        if bad and failed_step is None:
            failed_phase = name[len(PHASE_PREFIX):] if is_phase else name
            failed_step = step

    conclusion = job.get("conclusion")
    notes = annotations(repo, job["id"]) if repo and job.get("id") else None
    status, reason = classify(conclusion, phases, notes)
    common = {
        "createdAt": job.get("created_at"),
        "startedAt": job.get("started_at"),
        "phases": phases,
        "runnerName": job.get("runner_name"),
        "runnerGroup": job.get("runner_group_name"),
        "jobUrl": job.get("html_url"),
    }
    if status == "cancelled":
        entry.update({"status": "cancelled", "reason": reason, **common})
        if notes:
            entry["annotations"] = notes
        return entry
    ok = status == "success"
    entry.update(
        {
            "status": status,
            "failedAt": None if ok else failed_phase,
            "queueSeconds": seconds_between(job.get("created_at"), job.get("started_at")),
            "jobSeconds": seconds_between(job.get("started_at"), job.get("completed_at")),
            # The workload decides the outcome: a harness step (disk report,
            # upload) failing after every phase passed doesn't make the
            # benchmark a failure.
            "workloadSeconds": sum(p["seconds"] or 0 for p in phases if p["status"] == "success") if ok else None,
            **common,
            "host": host,
        }
    )
    if reason:
        entry["reason"] = reason
    if metrics:
        entry["metrics"] = metrics
    if notes:
        entry["annotations"] = notes
    if not ok and repo and job.get("id") and fetch_logs:
        tail = log_tail(job_log(repo, job["id"]), failed_step)
        if tail:
            entry["logTail"] = tail
    return entry


COST_SOURCE = "runs-on-control-plane"


def apply_cost(entry, summary):
    """Merge RunsOn's own estimate for this job (bin/runs-on-costs.py) into a
    RunsOn result: `cost` in USD, the instance's lifecycle and whether it was
    interrupted. A failed job on an interrupted spot instance gets reason
    "spot interruption". Without a summary the entry is left as it is (the
    site falls back to its own estimate)."""
    if not summary or entry.get("provider") != "RunsOn" or entry.get("status") == "unavailable":
        return entry
    if summary.get("usd") is None:
        return entry
    entry["cost"] = {
        "usd": summary["usd"],
        "ec2Usd": summary.get("ec2Usd"),
        "ebsUsd": summary.get("ebsUsd"),
        "lifecycle": summary.get("lifecycle"),
        "interrupted": bool(summary.get("interrupted")),
        "source": COST_SOURCE,
    }
    if entry["cost"]["interrupted"] and entry.get("status") == "failure":
        entry["reason"] = "spot interruption"
    return entry


def load_exclusions(results_dir):
    data = load_json(results_dir / "exclusions.json") or {}
    return {e["path"] for e in data.get("exclusions", []) if e.get("path")}


def rebuild_index(results_dir):
    files = []
    excluded = load_exclusions(results_dir)
    for path in sorted(results_dir.glob("*/*.json")):
        if str(path.relative_to(results_dir)) in excluded:
            continue
        data = load_json(path)
        if not data or "suite" not in data:
            continue
        jobs = data.get("results", [])
        files.append(
            {
                "path": str(path.relative_to(results_dir)),
                "suite": data["suite"],
                "date": data["run"]["date"],
                "runId": data["run"]["id"],
                "attempt": data["run"]["attempt"],
                "jobs": len(jobs),
                "succeeded": sum(1 for j in jobs if j.get("status") == "success"),
                # Which runners the file measured: a consumer keeping "the newest
                # runs" can keep them per runner, since a run (a burst shard, a
                # re-run of a few runners) often covers only some of them.
                "runners": sorted({j["runnerId"] for j in jobs if j.get("runnerId")}),
            }
        )
        selection = data.get("selection")
        if selection:
            # A run that left out some of the suite's default runners.
            files[-1]["subset"] = bool(selection.get("subset"))
            if selection.get("shard"):
                files[-1]["shard"] = f"{selection['shard']}/{selection.get('shards')}"
            origin = selection.get("origin") or {}
            if origin.get("event"):
                # How the run (or the burst chain it belongs to) was started:
                # schedule, push (trigger file) or workflow_dispatch.
                files[-1]["origin"] = origin["event"]
    files.sort(key=lambda f: (f["date"], f["runId"]), reverse=True)
    index = {"schemaVersion": 1, "files": files}
    (results_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")


def fmt(sec):
    if sec is None:
        return "—"
    return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan")
    parser.add_argument("--artifacts")
    parser.add_argument("--out", help="write the run file here instead of results/")
    parser.add_argument("--results", default=str(ROOT / "results"))
    parser.add_argument("--reindex", action="store_true")
    parser.add_argument("--costs", help="RunsOn job costs from bin/runs-on-costs.py (optional; ignored when missing)")
    args = parser.parse_args()

    results_dir = pathlib.Path(args.results)
    if args.reindex:
        rebuild_index(results_dir)
        return 0

    plan = json.loads(pathlib.Path(args.plan).read_text())
    suite = plan["suite"]
    workloads = json.loads((ROOT / "config" / "workloads.json").read_text())
    workload = workloads.get(suite)

    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    run = api(f"/repos/{repo}/actions/runs/{run_id}")
    jobs = {job_key(j["name"]): j for j in run_jobs(repo, run_id, attempt)}

    artifacts_dir = pathlib.Path(args.artifacts)
    costs = (load_json(args.costs) or {}) if args.costs else {}
    results = []
    for p in plan["jobs"]:
        job = jobs.get(p["key"])
        entry = summarize_job(p, job, artifacts_dir, repo)
        if job and job.get("id"):
            apply_cost(entry, costs.get(str(job["id"])))
        results.append(entry)

    started = parse_ts(run.get("run_started_at") or run.get("created_at"))
    date = started.date().isoformat()
    doc = {
        "schemaVersion": 1,
        "suite": suite,
        "workload": workload,
        "run": {
            "id": int(run_id),
            "attempt": int(attempt),
            "date": date,
            "startedAt": run.get("run_started_at"),
            "url": run.get("html_url"),
            "sha": run.get("head_sha"),
            "event": run.get("event"),
            "repository": repo,
        },
        # How the runners were chosen, and whether that was all of the suite's
        # default runners (subset: false) or only some.
        "selection": plan.get("selection"),
        "runners": plan["runners"],
        "results": results,
    }

    relative = pathlib.Path(suite) / f"{date}-{run_id}-{attempt}.json"
    out = pathlib.Path(args.out) if args.out else results_dir / relative
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"path={relative}\n")
    if not args.out:
        rebuild_index(results_dir)

    lines = [f"## {suite}: {sum(1 for r in results if r['status'] == 'success')}/{len(results)} succeeded", ""]
    lines.append("| Runner | Status | Queue | Workload | CPU | Failed at |")
    lines.append("|---|---|---:|---:|---|---|")
    for r in sorted(results, key=lambda r: (r.get("workloadSeconds") is None, r.get("workloadSeconds") or 0)):
        cpu = ((r.get("host") or {}).get("cpu") or {}).get("model") or "—"
        lines.append(
            f"| {r['key']} | {r['status']} | {fmt(r.get('queueSeconds'))} | "
            f"{fmt(r.get('workloadSeconds'))} | {cpu} | {r.get('failedAt') or ''} |"
        )
    summary = "\n".join(lines) + "\n"
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
