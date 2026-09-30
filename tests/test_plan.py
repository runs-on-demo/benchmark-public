"""Self-checks for bin/plan.py: where a run's selection comes from.

Run: python3 -m unittest discover -s tests
"""
import collections
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNERS = json.loads((ROOT / "config" / "runners.json").read_text())["runners"]


def run_plan(suite, trigger=None, event="workflow_dispatch", private="false", **cli):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        trig = tmp / "trigger.json"
        if trigger is not None:
            trig.write_text(json.dumps(trigger))
        out = tmp / "out.txt"
        args = [sys.executable, str(ROOT / "bin" / "plan.py"), "--suite", suite, "--trigger", str(trig),
                "--event", event, "--private", private, "--out", str(tmp / "plan.json")]
        for k, v in cli.items():
            args += [f"--{k}", str(v)]
        proc = subprocess.run(args, capture_output=True, text=True, env={**os.environ, "GITHUB_OUTPUT": str(out)})
        outputs = dict(l.split("=", 1) for l in out.read_text().splitlines()) if out.exists() else {}
        plan = json.loads((tmp / "plan.json").read_text()) if (tmp / "plan.json").exists() else None
        return proc.returncode, outputs, plan, proc.stderr


class Plan(unittest.TestCase):
    def test_push_with_trigger_at_rest_runs_nothing(self):
        rc, out, plan, _ = run_plan("rust", {"runners": "default", "arch": "all", "iterations": 1}, event="push")
        self.assertEqual((rc, out.get("run"), plan), (0, "false", None))

    def test_push_with_a_request_runs_it_and_flags_a_subset(self):
        rc, out, plan, _ = run_plan("rust", {"runners": "github-x64-8x32"}, event="push")
        self.assertEqual((rc, out.get("run")), (0, "true"))
        self.assertEqual([j["id"] for j in plan["jobs"]], ["github-x64-8x32"])
        self.assertTrue(plan["selection"]["subset"])

    def test_schedule_ignores_the_trigger_file(self):
        rc, out, plan, _ = run_plan("rust", {"runners": "github-x64-8x32", "iterations": 3}, event="schedule")
        self.assertEqual(rc, 0)
        default = [r["id"] for r in RUNNERS if "rust" in r.get("suites", [])]
        self.assertEqual(sorted(r["id"] for r in plan["runners"]), sorted(default))
        self.assertEqual(plan["iterations"], 1)
        self.assertFalse(plan["selection"]["subset"])
        self.assertEqual(plan["selection"]["source"], "schedule")

    def test_dispatch_ignores_a_stale_trigger_file(self):
        # A subset left in a trigger file never leaks into "run the whole suite".
        rc, out, plan, _ = run_plan("rust", {"runners": "github-x64-8x32", "iterations": 3})
        self.assertEqual((rc, out.get("run")), (0, "true"))
        default = [r["id"] for r in RUNNERS if "rust" in r.get("suites", [])]
        self.assertEqual(sorted(r["id"] for r in plan["runners"]), sorted(default))
        self.assertEqual(plan["iterations"], 1)
        self.assertEqual(plan["selection"]["source"], "default")
        self.assertFalse(plan["selection"]["subset"])

    def test_chained_shard_records_the_chain_origin(self):
        rc, out, plan, _ = run_plan("burst", None, runners="default", shard=2, origin="schedule:schedule:42")
        self.assertEqual(rc, 0)
        self.assertEqual(plan["selection"]["origin"], {"event": "schedule", "source": "schedule", "runId": 42})
        self.assertEqual(plan["selection"]["event"], "workflow_dispatch")
        self.assertEqual(out["origin"], "schedule:schedule:42")

    def test_first_shard_is_its_own_origin(self):
        rc, out, plan, _ = run_plan("burst", {"runners": "namespace-x64"}, event="push", shard=1)
        self.assertEqual(rc, 0)
        self.assertEqual(plan["selection"]["origin"]["event"], "push")
        self.assertEqual(plan["selection"]["origin"]["source"], "trigger")
        self.assertTrue(out["origin"].startswith("push:trigger:"))

    def test_one_arch_is_a_subset(self):
        _, _, plan, _ = run_plan("rust", None, arch="x64")
        self.assertTrue(plan["selection"]["subset"])

    def test_iterations_are_capped(self):
        rc, _, _, err = run_plan("rust", {"iterations": 6}, event="push")
        self.assertEqual(rc, 1, err)
        rc, _, _, err = run_plan("rust", None, iterations=6)
        self.assertEqual(rc, 1, err)
        rc, _, _, _ = run_plan("burst", None, iterations=16, shard=1)
        self.assertEqual(rc, 1)

    def test_unknown_runner_fails(self):
        rc, _, _, err = run_plan("rust", None, runners="nope")
        self.assertEqual(rc, 1)
        self.assertIn("unknown runner ids", err)

    def test_burst_shard_holds_one_runner_per_provider(self):
        for private in ("false", "true"):
            rc, out, plan, _ = run_plan("burst", None, private=private, shard=1)
            self.assertEqual(rc, 0)
            providers = collections.Counter(r["provider"] for r in plan["runners"])
            self.assertEqual(max(providers.values()), 1)
            self.assertEqual(plan["iterations"], 15)
            self.assertTrue(all(c == 15 for c in collections.Counter(j["id"] for j in plan["jobs"]).values()))
            self.assertEqual(plan["selection"]["shards"], int(out["shards"]))


EC2_CPU = [r for r in RUNNERS if "ec2-cpu" in r.get("suites", [])]
RACE_SUITES = ("rust", "node", "docker", "hardware", "cache", "burst")


class Ec2Cpu(unittest.TestCase):
    def test_every_ec2_cpu_runner_runs_once_public_and_private(self):
        self.assertGreater(len(EC2_CPU), 0)
        for private in ("false", "true"):
            rc, out, plan, err = run_plan("ec2-cpu", None, private=private)
            self.assertEqual((rc, out.get("run")), (0, "true"), err)
            self.assertEqual(len(plan["jobs"]), len(EC2_CPU))
            self.assertEqual(sorted(j["id"] for j in plan["jobs"]), sorted(r["id"] for r in EC2_CPU))
            self.assertFalse(plan["selection"]["subset"])
            self.assertTrue(all("{run_id}" not in j["label"] for j in plan["jobs"]))

    def test_trigger_at_rest_runs_nothing(self):
        trigger = json.loads((ROOT / "triggers" / "ec2-cpu.json").read_text())
        rc, out, plan, _ = run_plan("ec2-cpu", trigger, event="push")
        self.assertEqual((rc, out.get("run"), plan), (0, "false", None))

    def test_catalog_entries(self):
        types = [r["instanceType"] for r in EC2_CPU]
        self.assertEqual(len(types), len(set(types)), "one runner per instance type")
        for r in EC2_CPU:
            self.assertEqual(r["suites"], ["ec2-cpu"], r["id"])
            self.assertEqual(r["id"], "ec2-" + r["instanceType"].replace(".", "-"))
            self.assertEqual(r["provider"], "RunsOn")
            self.assertIn(f"family={r['instanceType']},", r["label"])
            self.assertIn(f"image=ubuntu24-full-{r['arch']},", r["label"])
        for r in RUNNERS:
            if r["id"].startswith("ec2-"):
                self.assertFalse(set(r.get("suites", [])) & set(RACE_SUITES), r["id"])


class Lanes(unittest.TestCase):
    def test_github_cache_storage_jobs_go_to_the_shared_lane(self):
        # Their 4 GiB entries share the repository's cache storage cap: never all at once.
        rc, out, plan, _ = run_plan("cache")
        self.assertEqual(rc, 0)
        lanes = {k: [j["key"] for j in json.loads(out[k])["include"]] if out.get(k) else [] for k in ("matrix", "shared")}
        self.assertEqual(sorted(lanes["matrix"] + lanes["shared"]), sorted(j["key"] for j in plan["jobs"]))
        shared = {j["id"] for j in plan["jobs"] if j.get("shared")}
        self.assertIn("github-x64.actions-cache", shared)
        self.assertIn("namespace-x64.actions-cache", shared)
        self.assertIn("warpbuild-arm64-xfast.actions-cache", shared)
        self.assertNotIn("warpbuild-arm64-xfast.warpbuilds-cache", shared)
        self.assertNotIn("warpbuild-x64.actions-cache", shared)
        self.assertNotIn("blacksmith-x64.actions-cache", shared)
        self.assertFalse(any(j["provider"] == "RunsOn" for j in plan["jobs"] if j.get("shared")))

    def test_every_plan_output_a_workflow_reads_is_declared(self):
        # A job reading needs.plan.outputs.X gets "" when the plan job doesn't
        # declare X, and its `if` silently skips the lane.
        import re
        for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = wf.read_text()
            read = set(re.findall(r"needs\.plan\.outputs\.([A-Za-z_-]+)", text))
            block = re.search(r"\n  plan:\n(.*?)\n    steps:", text, re.S)
            if not read:
                continue
            declared = set(re.findall(r"\n      ([A-Za-z_-]+): \$\{\{ steps\.plan\.outputs\.", block.group(1))) if block else set()
            self.assertEqual(read - declared, set(), wf.name)

    def test_other_suites_have_no_shared_lane(self):
        rc, out, _, _ = run_plan("rust")
        self.assertEqual((rc, out.get("shared")), (0, ""))
        self.assertTrue(out.get("matrix"))


class HardwareGroups(unittest.TestCase):
    def test_every_hw_bench_group_is_a_guarded_step(self):
        # hardware.yml runs one step per hw-bench.py group, each skipped unless
        # the caller's `groups` input (default: all) names it.
        import importlib.util
        spec = importlib.util.spec_from_file_location("hw_bench", ROOT / "bin" / "hw-bench.py")
        hw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hw)
        text = (ROOT / ".github" / "workflows" / "hardware.yml").read_text()
        default = text.split("inputs.groups || '", 1)[1].split("'", 1)[0].split(",")
        self.assertEqual(sorted(default), sorted(hw.GROUPS))
        for group in hw.GROUPS:
            self.assertIn(f'- name: "{group}"\n        if: contains(env.BENCH_GROUPS, \',{group},\')', text)

    def test_ec2_cpu_runs_only_cpu_groups(self):
        text = (ROOT / ".github" / "workflows" / "ec2-cpu.yml").read_text()
        self.assertIn("suite: ec2-cpu\n", text)
        self.assertIn("groups: cpu,passmark\n", text)
        workload = json.loads((ROOT / "config" / "workloads.json").read_text())["ec2-cpu"]
        self.assertEqual(workload["groups"], ["cpu", "passmark"])


if __name__ == "__main__":
    unittest.main()
