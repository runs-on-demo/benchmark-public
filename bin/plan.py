#!/usr/bin/env python3
"""Expand config/runners.json into a GitHub Actions matrix.

Usage: bin/plan.py --suite rust [--runners default|all|id,id] [--arch all|x64|arm64]
                   [--iterations 1] [--trigger triggers/rust.json] [--private true|false]
                   [--event schedule|push|workflow_dispatch] [--origin event:source:run]

Where the selection comes from:
- `--event schedule`: always the suite's default runners and iterations. Trigger
  files and inputs are ignored, so a scheduled run can't inherit a hand-edited
  subset.
- `--event push`: the trigger file is a one-shot request. A file at rest (default
  runners, all arches, default iterations) runs nothing, so resetting a trigger
  file after use never starts a suite.
- otherwise (dispatch): the inputs, and the suite's defaults for any left
  empty. Trigger files are read on push only, so "dispatch with empty inputs"
  always means the whole suite, whatever a trigger file still says.

`--origin` is for runs another run started (burst shards 2..n): it carries
the event and source of the run that started the chain, recorded as
`selection.origin`, so a scheduled chain doesn't read as n manual dispatches.

Iterations are capped (MAX_ITERATIONS). Private repositories get `labelPrivate`
where a runner defines one (GitHub's free `ubuntu-24.04` runner is 4 vCPU only
for public repositories).

Writes `run=true|false`, `matrix=<json>`, `wave1`..`wave<MAX_WAVES>`, `shards`
and the resolved `runners`, `arch`, `iterations` to $GITHUB_OUTPUT (or the
matrix to stdout) and the full plan to plan.json, which the publish job uses to
report runners that never started. plan.json's `selection` records whether the
run covered the suite's default runners (`subset: false`) or only some of them.

Cache jobs that store in this repository's own GitHub cache storage
(actions/cache on a provider that doesn't reroute it) go to waves of
WAVE_SIZE jobs (`wave1`, `wave2`, ...; plan.json marks them `shared` and their
`wave`), every other job to `matrix`; an output is empty ("") when it has no
job. That storage is capped per repository (10 GB by default) and GitHub
evicts entries past the cap, even one another job is still restoring, so
cache.yml runs one wave (8 GiB) at a time, each after the previous one ended,
and the other jobs all at once. Waves, not a max-parallel matrix: GitHub
announces every job of a matrix when the run starts, max-parallel or not, and
providers that start a runner on that announcement (CodeBuild, StarSling,
Namespace) lose it to their idle timeout before a held-back job gets its turn,
then never start another. A job of a later wave is announced when its wave
starts.
"""
import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Providers whose runners send actions/cache to their own storage, with no
# workflow change or with the label and step the catalog already gives
# (RunsOn: extras=s3-cache and runs-on/action), per their docs and checked
# against this repository's GitHub cache list during a cache run (2026-10-01:
# no entry of theirs appeared; GitHub, Namespace, StarSling, CodeBuild and
# every Warpbuild runner's did). Warpbuild's own action (WarpBuilds/cache)
# stores in Warpbuild's storage, so only its actions/cache jobs share.
REROUTES_ACTIONS_CACHE = {"RunsOn", "Blacksmith", "Ubicloud", "Avrea"}


# Shared cache jobs per wave (4 GiB each: 8 GiB in flight), and the waves
# cache.yml defines (bench-w1 .. bench-w<MAX_WAVES>).
WAVE_SIZE = 2
MAX_WAVES = 20


def github_cache_storage(runner) -> bool:
    """The job's cache entry lands in this repository's GitHub cache storage."""
    return runner.get("cacheAction") == "actions/cache" and runner["provider"] not in REROUTES_ACTIONS_CACHE

# Jobs per runner a suite runs when nothing asks otherwise. The burst queues 15
# jobs per runner at once: that is the measurement, not a repeat count.
DEFAULT_ITERATIONS = {"burst": 15}
# A trigger file or a dispatch can't ask for more than this.
MAX_ITERATIONS = {"burst": 15}
MAX_ITERATIONS_DEFAULT = 5
ARCHES = ("all", "x64", "arm64")


def in_suite(runner, suite):
    """The suite's default runners. Smoke exists to check labels, so by default
    it reaches every runner in the catalog."""
    return suite == "smoke" or suite in runner.get("suites", [])


def default_iterations(suite):
    return DEFAULT_ITERATIONS.get(suite, 1)


def max_iterations(suite):
    return MAX_ITERATIONS.get(suite, MAX_ITERATIONS_DEFAULT)


def trigger_at_rest(suite, trigger):
    """True when a trigger file asks for nothing beyond the suite's defaults."""
    return (
        str(trigger.get("runners", "default")).strip() in ("", "default")
        and str(trigger.get("arch", "all")).strip() in ("", "all")
        and int(trigger.get("iterations") or default_iterations(suite)) == default_iterations(suite)
    )


def write_output(**values):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def merge_label(label: str, extra: str) -> str:
    """Append comma-separated settings to a RunsOn label. `extras` values are
    combined with "+" (extras=tmpfs + extras=s3-cache -> extras=tmpfs+s3-cache)
    instead of repeating the key."""
    parts = label.split(",")
    for setting in extra.split(","):
        key, _, value = setting.partition("=")
        if key == "extras":
            for i, part in enumerate(parts):
                if part.startswith("extras="):
                    existing = part[len("extras="):].split("+")
                    parts[i] = "extras=" + "+".join(existing + [v for v in value.split("+") if v not in existing])
                    break
            else:
                parts.append(setting)
        else:
            parts.append(setting)
    return ",".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True)
    parser.add_argument("--runners", default="")
    parser.add_argument("--arch", default="")
    parser.add_argument("--iterations", default="")
    parser.add_argument("--trigger", default="")
    parser.add_argument("--private", default="false")
    parser.add_argument("--shard", default="", help="burst only: 1-based shard, one runner per provider")
    parser.add_argument("--event", default="", help="GitHub event that started the run")
    parser.add_argument("--origin", default="", help="event:source:run id of the run that started this chain")
    parser.add_argument("--out", default="plan.json")
    args = parser.parse_args()

    trigger = {}
    if args.event == "schedule":
        # Scheduled runs measure the suite as configured, nothing else.
        args.runners = args.arch = args.iterations = ""
        source = "schedule"
    elif args.event == "push":
        # A push of a trigger file: the file is the request, nothing else.
        if args.trigger and pathlib.Path(args.trigger).exists():
            trigger = json.loads(pathlib.Path(args.trigger).read_text())
        if trigger_at_rest(args.suite, trigger):
            print(f"{args.trigger} is at rest (default runners): nothing to run on push", file=sys.stderr)
            write_output(run="false", shards=0)
            return 0
        args.runners = args.arch = args.iterations = ""
        source = "trigger"
    else:
        # A dispatch: its inputs, and the suite's defaults for empty ones.
        source = "inputs" if (args.runners or args.arch or args.iterations) else "default"
    args.runners = args.runners or str(trigger.get("runners", "default"))
    args.arch = args.arch or str(trigger.get("arch", "all"))
    args.iterations = int(args.iterations or trigger.get("iterations") or default_iterations(args.suite))
    private = args.private.lower() == "true"
    if args.arch not in ARCHES:
        print(f"arch must be one of {', '.join(ARCHES)}, not {args.arch!r}", file=sys.stderr)
        return 1
    if not 1 <= args.iterations <= max_iterations(args.suite):
        print(f"iterations must be 1 to {max_iterations(args.suite)} for {args.suite}, not {args.iterations}", file=sys.stderr)
        return 1

    config = json.loads((ROOT / "config" / "runners.json").read_text())
    runners = config["runners"]

    wanted = args.runners.strip()
    if wanted in ("", "default"):
        selected = [r for r in runners if in_suite(r, args.suite)]
    elif wanted == "all":
        selected = list(runners)
    else:
        ids = {i.strip() for i in wanted.split(",") if i.strip()}
        unknown = ids - {r["id"] for r in runners}
        if unknown:
            print(f"unknown runner ids: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        selected = [r for r in runners if r["id"] in ids]

    if args.arch != "all":
        selected = [r for r in selected if r["arch"] == args.arch]
    if private:
        # labelPrivate: null means the runner has no equivalent label outside
        # a public repository, so it would only sit in the queue.
        skipped = [r["id"] for r in selected if "labelPrivate" in r and r["labelPrivate"] is None]
        if skipped:
            print(f"skipping in private repositories: {', '.join(skipped)}", file=sys.stderr)
        selected = [r for r in selected if r["id"] not in skipped]

    # Subset runs are flagged: a run that left out some of the suite's default
    # runners (a hand-picked list, one arch) is not a full pass of the suite.
    defaults = [r["id"] for r in runners if in_suite(r, args.suite)]
    if private:
        defaults = [i for i in defaults if not any(r["id"] == i and r.get("labelPrivate", "") is None for r in runners)]
    chosen = {r["id"] for r in selected}
    missing = [i for i in defaults if i not in chosen]
    run_id = os.environ.get("GITHUB_RUN_ID", "0")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    # The run that started this one (burst chain), or this run itself.
    o_event, _, rest = args.origin.partition(":")
    o_source, _, o_run = rest.partition(":")
    origin = {
        "event": o_event or args.event or None,
        "source": o_source or source,
        "runId": int(o_run) if o_run.isdigit() else (int(run_id) if run_id.isdigit() and run_id != "0" else None),
    }
    selection = {
        "event": args.event or None,
        "source": source,
        "origin": origin,
        "runners": args.runners,
        "arch": args.arch,
        "iterations": args.iterations,
        "defaultRunners": len(defaults),
        "subset": bool(missing),
        "missingDefaultRunners": missing,
    }

    shards = 0
    if args.shard:
        # One runner per provider per shard, in catalog order. Some providers
        # cap concurrency account-wide (Namespace, Avrea: x64 and arm64 share
        # one limit), so two runners of one provider in the same shard would
        # queue behind each other and measure that shared cap, not a burst on
        # one runner. A shard is 15 jobs per provider at most, well under
        # GitHub's 256-job matrix cap.
        groups: dict = {}
        for r in selected:
            groups.setdefault(r["provider"], []).append(r)
        shards = max((len(g) for g in groups.values()), default=0)
        k = int(args.shard)
        selected = [g[k - 1] for g in groups.values() if len(g) >= k]
        selection.update({"shard": k, "shards": shards})
        print(f"shard {k} of {shards}: {len(selected)} runners", file=sys.stderr)

    if args.suite == "cache":
        # Each cache action is its own row: "blacksmith-x64.useblacksmith-cache".
        # baseId keeps the link to the runner catalog.
        selected = [
            {**r, "id": f"{r['id']}.{a.lower().replace('/', '-')}", "baseId": r["id"], "cacheAction": a}
            for r in selected
            for a in (r.get("cacheActions") or ["actions/cache"])
        ]

    include = []
    for runner in selected:
        label = runner.get("labelPrivate", runner["label"]) if private else runner["label"]
        extra = (runner.get("suiteLabel") or {}).get(args.suite)
        if extra and isinstance(label, str):
            label = merge_label(label, extra)
            runner["label"] = merge_label(runner["label"], extra)

        def fill(value):
            return value.replace("{run_id}", run_id).replace("{run_attempt}", run_attempt)

        # A label is one string, or a list of labels a runner must all carry
        # (e.g. Namespace's "namespace-features:linux-on-apple-silicon=true").
        label = [fill(x) for x in label] if isinstance(label, list) else fill(label)
        for iteration in range(1, max(1, args.iterations) + 1):
            include.append(
                {
                    "key": f"{runner['id']}--{iteration}",
                    "id": runner["id"],
                    "provider": runner["provider"],
                    "arch": runner["arch"],
                    "label": label,
                    "iteration": iteration,
                    "cacheAction": runner.get("cacheAction", ""),
                    **({"shared": True} if github_cache_storage(runner) else {}),
                }
            )

    write_output(
        shards=shards,
        runners=args.runners,
        arch=args.arch,
        iterations=args.iterations,
        origin=f"{origin['event'] or ''}:{origin['source']}:{origin['runId'] or ''}",
    )
    if not include:
        print("no runners selected", file=sys.stderr)
        return 1

    # Shared cache jobs, in waves (see the module docstring).
    shared = [j for j in include if j.get("shared")]
    waves = [shared[i:i + WAVE_SIZE] for i in range(0, len(shared), WAVE_SIZE)]
    if len(waves) > MAX_WAVES:
        print(f"{len(shared)} shared cache jobs need {len(waves)} waves; cache.yml defines {MAX_WAVES}", file=sys.stderr)
        return 1
    for k, wave in enumerate(waves, 1):
        for j in wave:
            j["wave"] = k

    if private:
        for runner in selected:
            if "labelPrivate" in runner:
                runner["label"] = runner.pop("labelPrivate")
                runner.update(runner.pop("private", {}))
    plan = {
        "suite": args.suite,
        "iterations": args.iterations,
        "selection": selection,
        "runners": [
            {k: v for k, v in r.items() if k not in ("suites", "labelPrivate", "private", "suiteLabel", "cacheActions")}
            for r in selected
        ],
        "jobs": include,
    }
    pathlib.Path(args.out).write_text(json.dumps(plan, indent=2) + "\n")

    def lane(jobs):
        return json.dumps({"include": jobs}, separators=(",", ":")) if jobs else ""

    matrix = lane([j for j in include if not j.get("shared")])
    if os.environ.get("GITHUB_OUTPUT"):
        write_output(
            run="true",
            matrix=matrix,
            **{f"wave{k}": lane(waves[k - 1] if k <= len(waves) else []) for k in range(1, MAX_WAVES + 1)},
        )
    else:
        print(matrix)
    subset = f", subset: {len(missing)} of {len(defaults)} default runners left out" if missing else ""
    print(f"{len(include)} jobs across {len(selected)} runners{subset}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
