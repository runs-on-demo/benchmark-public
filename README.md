# GitHub Actions runner benchmarks

The harness behind [runs-on.com/benchmarks](https://runs-on.com/benchmarks/).
Every suite runs the same job on each provider's runners, and every run's raw
results are committed to [`results/`](results/) as JSON.

RunsOn is one of the providers measured here. That is why the harness, the
runner labels and the numbers are all public: anyone can check how a number was
produced, or fork the repository and run it on their own account.

## Suites

| Suite | What it runs | Workflow |
|---|---|---|
| `rust` | Cold build of [aome510/spotify-player](https://github.com/aome510/spotify-player): checkout, apt packages, toolchain, `cargo fmt`, `cargo test`, two `cargo clippy` passes | [rust.yml](.github/workflows/rust.yml) |
| `node` | Cold clone, `bun install` and typecheck of [anomalyco/opencode](https://github.com/anomalyco/opencode), at the commit and Bun version of ComputeSDK's [DAX sandbox benchmark](https://www.computesdk.com/benchmarks/sandboxes/dax). A pinned `node-gyp` is installed on every runner in an untimed step, so native modules build the same way everywhere. Typecheck runs 4 turbo tasks on every runner (`--concurrency=4`) so it fits in 16 GB, which makes totals differ from DAX | [node.yml](.github/workflows/node.yml) |
| `docker` | `docker buildx build` of [PostHog](https://github.com/PostHog/posthog)'s production Dockerfile at a pinned commit, no build cache. The source is fetched shallow (depth 1, no tags) in its own `fetch source` phase, and the build uses that checkout as its context, so `docker build` times the build, not a git fetch | [docker.yml](.github/workflows/docker.yml) |
| `hardware` | sysbench, 7-Zip and PassMark CPU; memory bandwidth; fio in the job workspace; downloads; `docker pull`; pgbench | [hardware.yml](.github/workflows/hardware.yml) |
| `ec2-storage` | The hardware suite on EC2 types that differ by storage: EBS gp3 (default and provisioned), local NVMe at several sizes, tmpfs | [ec2-storage.yml](.github/workflows/ec2-storage.yml) |
| `ec2-cpu` | The hardware suite's CPU groups only (sysbench, 7-Zip, PassMark) on one instance per EC2 type, at its smallest 2 vCPU size (`.large`, `.medium` for burstable `t` types): 50 types, x64 and Graviton. See [EC2 CPU per instance type](#ec2-cpu-per-instance-type) | [ec2-cpu.yml](.github/workflows/ec2-cpu.yml) |
| `cpu-daily` | The hardware suite's PassMark group only, every day, on each provider's 2 vCPU runners (and a few runners whose label continues runs-on.com's CPU history): the single-thread score over time. See [CPU daily](#cpu-daily) | [cpu-daily.yml](.github/workflows/cpu-daily.yml) |
| `cache` | A 4 GB file saved with a cache action, deleted, then restored immediately on the same runner (no pause, so a cache that isn't read-after-write consistent fails): `actions/cache` and the provider's own cache action where it offers one (WarpBuilds/cache). Jobs whose `actions/cache` stores in this repository's GitHub cache run in waves of two, each starting when the previous one ends, so the repository's cache storage cap (10 GB by default) never evicts an entry mid-restore; a restore that loses its entry that way anyway counts as measuring nothing, not as the runner's failure | [cache.yml](.github/workflows/cache.yml) |
| `burst` | 15 identical jobs per runner queued at the same instant, each holding its runner for 60 s: how long jobs wait when a spike arrives (scaling speed plus the plan's concurrency limits). Runs in shards of one runner per provider, one shard after another; each shard starts the next once it has published. Some providers cap concurrency per account across both architectures, so a provider's x64 and arm64 runners never share a shard | [burst.yml](.github/workflows/burst.yml) |
| `smoke` | Picks up a job and records the host, to check which labels this repository can reach (every runner in the catalog by default) | [smoke.yml](.github/workflows/smoke.yml) |

Workloads are pinned to a commit and a toolchain version
([config/workloads.json](config/workloads.json)) so a change in the numbers
comes from the runner, not from the project being built.

## Runners

[config/runners.json](config/runners.json) lists every runner label, the shape
it advertises, and which suites it joins. Runners from 2 to 12 vCPU (x64 and
arm64) join every race suite: `rust`, `node`, `docker`, `hardware`, `cache`
and `burst`. The 16 vCPU EC2 types (`m8id.4xlarge`, `i7i.4xlarge`) run only in
`ec2-storage`, where they show how local NVMe scales; they don't race smaller
runners. The `ec2-<instance type>` runners (`ec2-c6a-large`, `ec2-m9g-large`,
…) run only in `ec2-cpu`: they measure an EC2 type, not a CI runner, and never
appear in a race. Every 2 vCPU runner also joins `cpu-daily`, a PassMark run
each day.

Each job also records what it actually got: CPU model, memory, the filesystem
and device behind the workspace and the Docker root, the EC2 instance type,
AMI and lifecycle (spot or on-demand) when there is one, and the RunsOn stack
version and image on RunsOn runners ([bin/host-info.py](bin/host-info.py)).
These host facts, and the hardware metrics, are reported by the job itself
(artifacts it uploads); only the timings come from GitHub.

## How timings are measured

Durations come from the GitHub Jobs API, not from inside the job, so a runner
can't misreport its own speed:

- **queue**: the job's `started_at` minus its `created_at`
- **phases**: every step named `phase: …`, from its start and completion times
- **workload**: the sum of the phases. Harness steps (host info, uploads,
  disk cleanup) are excluded.

A runner that never picked up its job is recorded as `unavailable`, and a job
that failed records the phase it failed in, a `reason` when the job's
annotations give one (`runner lost` when the runner lost communication or got
a shutdown signal, `timed out`, `step killed` for a step killed inside a failed
job, most often out of memory; `spot interruption` on a RunsOn job whose
spot instance was interrupted), the job's error annotations, and the last 40
lines of the failing step (`logTail`: the step header, env blocks and provider
environment dumps are dropped, likely secrets, bucket names and instance ids
redacted). A job is `cancelled` only when the run was cancelled; a runner lost
mid-phase is a failure. When the annotations can't be read, a job that ended
"cancelled" is recorded as a failure with reason `unknown (annotations
unavailable)`, never dropped as cancelled. Missing providers stay visible.

## Running a suite

Dispatch the workflow by hand, or edit `triggers/<suite>.json` and push. Both
accept the same knobs:

```json
{ "runners": "default", "arch": "all", "iterations": 1 }
```

`runners` is `default` (the suite's runners), `all`, or a comma-separated
list of ids from `config/runners.json`. `iterations` is at most 5 (the burst
queues 15 jobs per runner by default; 1 to 15 are allowed).

Trigger files are one-shot requests, read only when they are pushed. A file at
rest (the content above) runs nothing when pushed, so the way to use one is:
set a subset, push, then reset it. A dispatch never reads trigger files: empty
inputs mean the suite's defaults, so a subset left in a file can't leak into a
"run the whole suite" dispatch. A check
([check.yml](.github/workflows/check.yml), GitHub-hosted, also dispatchable)
fails any push whose trigger files name a runner that isn't in
`config/runners.json`, and warns about a file that isn't at rest. The first
push of a new branch never starts a suite.

Every result file records how its runners were chosen (`selection`: the event,
whether the runners came from the schedule, the dispatch inputs or a trigger
file, and the burst shard). `selection.origin` is how the run was started: for
burst shards 2 and later, which the previous shard dispatches, it is the first
shard's event, source and run id, so a scheduled chain reads as scheduled. A run that left out some of the suite's default
runners is flagged `"subset": true`, in the file and in `results/index.json`.

## Schedule

Each measurement suite has a cron (smoke is dispatch-only), staggered so two
suites never share a provider's concurrency. While the first results build up,
the suites run every three days: Rust, TypeScript and Docker on day one (02:00,
04:00, 06:00 UTC), hardware and cache on day two (02:00, 04:00), the burst
alone on day three at 15:00. EC2 storage runs on the 1st of each month and EC2
CPU on the 10th, both at 10:00, an hour no other suite uses. CPU daily runs every day
at 08:00, also an hour of its own. Results also come from manual dispatches and trigger-file
pushes; `selection.origin.event` says which.
A scheduled run always uses the suite's default runners and ignores the
trigger files.
A burst shard publishes, and starts the next one, only once all its jobs have
finished, and a queued job that no runner picks up waits up to 24 hours. So a
label no provider serves stalls the whole chain: dispatch `smoke` before a
burst chain to confirm every label is served.
The site (runs-on/runs-on.com) checks for new results every six hours and
opens a pull request with them; they go live once it is merged.

## EC2 CPU per instance type

`ec2-cpu` measures the CPU of each EC2 instance type RunsOn can launch, one
2 vCPU instance per type (spot, RunsOn's default), once a month. RunsOn uses
these numbers for the CPU figures it shows per instance type (its instance
finder and the CPU comparisons on runs-on.com), so a figure there can be traced
to a run here. It replaces an earlier private sweep of the same instance types.

The figure used is PassMark PerformanceTest's **CPU Single Threaded** result,
in million operations per second (`metrics.cpu.passmarkSingle`), the same test
and unit the earlier sweep recorded. `bin/hw-bench.py passmark` runs
`pt_linux -r 1 -i 2 -p <vCPUs> -d 2` (CPU tests only, 2 iterations, one process
per vCPU, medium duration) and reads `CPU_SINGLETHREAD` from its
`results_cpu.yml`; the process count only changes the multi-threaded tests and
the CPU Mark (`passmarkCpuMark`). The harness installs the latest PassMark V11
build on both architectures; the earlier sweep ran V11 (11.0.1002) on x64 and
V10.2 (10.2.1003) on Graviton, and the single-threaded figures match it within
about 1% on both. sysbench (single and multi-thread events per
second) and the 7-Zip MIPS rating are recorded alongside. Memory, disk,
network, Docker and PostgreSQL groups are not run (the reusable
[hardware.yml](.github/workflows/hardware.yml) takes a `groups` input).

To add an instance type, add an `ec2-<type with dots as dashes>` runner to
`config/runners.json` with `"suites": ["ec2-cpu"]`.

## CPU daily

`cpu-daily` runs PassMark PerformanceTest's CPU tests every day at 08:00 UTC,
an hour no other suite uses, on each provider's 2 vCPU runners. The figure it
is for is **CPU Single Threaded** (`metrics.cpu.passmarkSingle`): the same
`bin/hw-bench.py passmark` group, with the same settings, as the hardware
suite, so a runner's daily points and its hardware-suite points form one
series. runs-on.com draws them as one line per runner label over time,
continuing the daily single-thread samples it has kept since July 2026 under
the same labels. A few runners at other sizes join it for that reason: AWS
CodeBuild medium and large, GitHub's standard x64 runner, StarSling's default
label and Namespace on Apple Silicon. To add a runner, add `"cpu-daily"` to its
`suites` in `config/runners.json`.

## Results format

One file per workflow run: `results/<suite>/<date>-<run id>-<attempt>.json`.
[`results/index.json`](results/index.json) lists them, newest first. A run that
should not count is never deleted: it is listed, with the reason, in
[`results/exclusions.json`](results/exclusions.json) and left out of the index.

Only runs on the default branch publish to `results/`. To try a variation (a
different image, label or workload) without it reaching the published data,
change it on a branch and dispatch the suite there, for example
`gh workflow run burst.yml --ref exp/ubuntu26 -f runners=runson-m8a-x64`: the
result is committed to that branch under `experiments/<branch>/`, never into
`results/` or its index, so merging the branch can't publish it. Schedules
only fire on the default branch. A branch run shares the providers with the
default branch's runs, so keep it clear of a burst. RunsOn's measured per-job cost isn't looked up there (the cost role
trusts the default branch only).

```jsonc
{
  "schemaVersion": 1,
  "suite": "rust",
  "workload": { "name": "…", "repository": "…", "ref": "…" },
  "run": { "id": 123, "attempt": 1, "date": "2026-09-29", "url": "…", "sha": "…", "event": "schedule" },
  "selection": { "event": "schedule", "source": "schedule", "origin": { "event": "schedule", … }, "subset": false, "missingDefaultRunners": [] },
  "runners": [ /* the config entries this run used */ ],
  "results": [
    {
      "runnerId": "namespace-x64", "provider": "Namespace", "arch": "x64", "iteration": 1,
      "status": "success",            // success | failure | cancelled | unavailable
      "failedAt": null,               // the phase a failure stopped at, e.g. "typecheck"
      // failures also carry "reason" ("runner lost", "timed out", "step killed", "spot interruption")
      // and "logTail" (the failing step's last 40 lines, headers and env dropped)
      "queueSeconds": 4, "jobSeconds": 251, "workloadSeconds": 238,
      "phases": [ { "name": "cargo test", "seconds": 131, "status": "success" } ],
      "host": { "cpu": { "model": "…" }, "memoryGiB": 15.6, "storage": { … }, "ec2": null, "runsOn": null },
      "metrics": { /* hardware suites only */ },
      "cost": { /* RunsOn only: RunsOn's own per-job estimate, see below */ }
    }
  ]
}
```

## Provider notes

- GitHub's `ubuntu-24.04` and `ubuntu-24.04-arm` labels are 4 vCPU runners
  only for public repositories.
- RunsOn rows run on spot, RunsOn's default capacity, with `retry=false`: a
  spot interruption is not retried, so it lands in the data as a failure
  (reason `spot interruption` when RunsOn's job summary says the instance was
  interrupted). Each result records the instance type, lifecycle, AMI, region
  and AZ, and the RunsOn version of the stack. RunsOn operates the stack these
  rows run on.
- Prices and cost per run are computed by the site, not here: see the
  methodology on [runs-on.com/benchmarks](https://runs-on.com/benchmarks/).
- Depot is not included: its terms do not allow benchmarking the platform.

## Memory facts

Every job's `host.json` records `memory`: swap size and devices (zram
included), available memory, `vm.overcommit_memory` and `vm.swappiness`. A
runner with swap slows down under a memory peak instead of having processes
killed, which explains why the same workload fits some 8 GB runners and not
others. RunsOn runners have no swap by design: their RAM is chosen per job.

## RunsOn job costs

RunsOn runners run on your own AWS account, so their cost is not a list price.
RunsOn's control plane estimates each job's cost from what actually ran (instance
type, spot or on-demand, availability zone and its spot price over the job's
window, EBS volumes, AWS's 60-second minimum) and logs it as a `job_summary`
event. The publish job reads those events for its own jobs through a read-only
role: [infra/runs-on-cost-reader.yml](infra/runs-on-cost-reader.yml)
(CloudFormation; GitHub OIDC, `logs:StartQuery` on the stack's control-plane log
group only). The RunsOn license is added on top by the site and disclosed there.

The lookup runs when the repository variable `AWS_COST_READER_ROLE_ARN` is set
(with `RUNS_ON_LOG_GROUP` and `AWS_REGION`): the publish job assumes the role,
[bin/runs-on-costs.py](bin/runs-on-costs.py) queries the `job_summary` events
of the run's RunsOn jobs that started (retrying for up to 6 minutes, since a
summary lands shortly after its job ends), and `collect.py` adds them to each
RunsOn result:

```jsonc
"cost": { "usd": 0.0097, "ec2Usd": 0.0079, "ebsUsd": 0.0018, "lifecycle": "spot", "interrupted": false, "source": "runs-on-control-plane" }
```

A job without a summary has no `cost` (the site falls back to its own
estimate). The lookup never fails a publish, and no AWS credentials, account
or instance ids are written to results by it.
