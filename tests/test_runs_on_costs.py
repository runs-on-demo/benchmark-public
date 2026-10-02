"""Self-checks for bin/runs-on-costs.py and the cost merge in bin/collect.py.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


costs = load("runs_on_costs", "runs-on-costs.py")
collect = costs.collect


def row(**fields):
    return [{"field": k, "value": str(v)} for k, v in fields.items()]


# A canned get-query-results response: newest first, as the query sorts it.
RESPONSE = {
    "status": "Complete",
    "results": [
        row(**{
            "@timestamp": "2026-09-30 08:48:56.949",
            "job_id": 109805235446,
            "estimated_cost_usd": 0.06397096310786905,
            "estimated_ec2_cost_usd": 0.05719219074635969,
            "estimated_ebs_cost_usd": 0.006778772361509361,
            "instance_lifecycle": "spot",
            "instance_type": "m8i-flex.xlarge",
            "job_duration_seconds": 997,
        }),
        row(**{
            "@timestamp": "2026-09-30 08:40:00.000",
            "job_id": 109805235447,
            "estimated_cost_usd": 0.0123,
            "estimated_ec2_cost_usd": 0.011,
            "estimated_ebs_cost_usd": 0.0013,
            "instance_lifecycle": "spot",
            "interrupted": "true",
            "instance_type": "c8g.large",
            "job_duration_seconds": 312.4,
        }),
        # An older duplicate of the first job: the newest wins.
        row(**{
            "@timestamp": "2026-09-30 08:30:00.000",
            "job_id": 109805235446,
            "estimated_cost_usd": 9.99,
            "instance_lifecycle": "on-demand",
        }),
        # No cost: skipped.
        row(**{"@timestamp": "2026-09-30 08:20:00.000", "job_id": 109805235448}),
    ],
}


class ParseResults(unittest.TestCase):
    def test_canned_response(self):
        out = costs.parse_results(RESPONSE)
        self.assertEqual(set(out), {"109805235446", "109805235447"})
        first = out["109805235446"]
        self.assertEqual(first["usd"], 0.063971)
        self.assertEqual(first["ec2Usd"], 0.057192)
        self.assertEqual(first["ebsUsd"], 0.006779)
        self.assertEqual(first["lifecycle"], "spot")
        self.assertFalse(first["interrupted"])
        self.assertEqual(first["instanceType"], "m8i-flex.xlarge")
        self.assertEqual(first["durationSeconds"], 997)
        second = out["109805235447"]
        self.assertTrue(second["interrupted"])
        self.assertEqual(second["durationSeconds"], 312)

    def test_empty(self):
        self.assertEqual(costs.parse_results({"status": "Complete", "results": []}), {})


class Queries(unittest.TestCase):
    def test_filter_and_batches(self):
        queries = list(costs.build_queries(range(1, 251), batch=100))
        self.assertEqual(len(queries), 3)
        self.assertIn('metric_type = "job_summary"', queries[0])
        self.assertIn("job_id in [1, 2, ", queries[0])
        self.assertIn("job_id in [201, ", queries[2])
        self.assertTrue(queries[2].rstrip().endswith("limit 10000"))

    def test_ids_are_integers(self):
        with self.assertRaises(ValueError):
            list(costs.build_queries(["1) | fields @message"]))


class RunsOnJobs(unittest.TestCase):
    def test_only_started_runs_on_jobs(self):
        plan = {
            "jobs": [
                {"key": "runson-a--1", "provider": "RunsOn"},
                {"key": "runson-b--1", "provider": "RunsOn"},
                {"key": "runson-c--1", "provider": "RunsOn"},
                {"key": "github--1", "provider": "GitHub"},
            ]
        }
        jobs = {
            "runson-a--1": {"id": 11, "created_at": "2026-09-30T08:00:00Z", "started_at": "2026-09-30T08:00:30Z", "runner_name": "r"},
            # Cancelled before any runner took it: never started.
            "runson-b--1": {"id": 12, "created_at": "2026-09-30T08:00:00Z", "started_at": "2026-09-30T08:10:00Z", "conclusion": "cancelled", "runner_name": ""},
            "github--1": {"id": 14, "created_at": "2026-09-30T08:00:00Z", "started_at": "2026-09-30T08:00:05Z", "runner_name": "g"},
        }
        self.assertEqual(costs.runs_on_jobs(plan, jobs), {11: "2026-09-30T08:00:00Z"})


class Lookup(unittest.TestCase):
    def clock(self):
        t = [1000.0]
        return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))

    def test_retries_until_every_summary_is_in(self):
        now, sleep = self.clock()
        calls = []

        def query(q, start, end):
            calls.append(q)
            # The second job's summary lands on the second try.
            return RESPONSE if len(calls) > 1 else {"results": RESPONSE["results"][:1]}

        with mock.patch("sys.stderr"):
            found, missing = costs.lookup([109805235446, 109805235447], 0, 360, 30, query, now=now, sleep=sleep)
        self.assertEqual(missing, [])
        self.assertEqual(len(calls), 2)
        self.assertIn("job_id in [109805235447]", calls[1])  # only the missing one

    def test_gives_up_after_wait(self):
        now, sleep = self.clock()
        with mock.patch("sys.stderr"):
            found, missing = costs.lookup([1, 2], 0, 90, 30, lambda *a: {"results": []}, now=now, sleep=sleep)
        self.assertEqual((found, missing), ({}, [1, 2]))
        self.assertLessEqual(now(), 1000 + 90)

    def test_query_error_stops_without_raising(self):
        def query(*a):
            raise RuntimeError("AccessDeniedException")

        with mock.patch("sys.stderr"):
            found, missing = costs.lookup([1], 0, 360, 30, query)
        self.assertEqual((found, missing), ({}, [1]))

    def test_main_never_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "costs.json"
            argv = ["runs-on-costs.py", "--job-ids", "1", "--log-group", "", "--out", str(out)]
            with mock.patch("sys.argv", argv), mock.patch("sys.stderr"), mock.patch("sys.stdout"):
                self.assertEqual(costs.main(), 0)
            self.assertEqual(json.loads(out.read_text()), {})


class Merge(unittest.TestCase):
    def entry(self, provider="RunsOn", status="success", reason=None):
        e = {"key": "k", "provider": provider, "status": status}
        if reason:
            e["reason"] = reason
        return e

    def test_billed_time_and_rates_kept_when_logged(self):
        summary = {"usd": 0.004, "ec2Usd": 0.0035, "ebsUsd": 0.0005, "lifecycle": "spot", "interrupted": False,
                   "billedSeconds": 97.7, "ec2HourlyUsd": 0.129, "ebsHourlyUsd": 0.0184}
        e = collect.apply_cost(self.entry(), summary)
        self.assertEqual((e["cost"]["billedSeconds"], e["cost"]["ec2HourlyUsd"], e["cost"]["ebsHourlyUsd"]), (97.7, 0.129, 0.0184))
        self.assertNotIn("billedSeconds", collect.apply_cost(self.entry(), {**summary, "billedSeconds": None})["cost"])

    def test_cost_merged_without_instance_details(self):
        summary = costs.parse_results(RESPONSE)["109805235446"]
        e = collect.apply_cost(self.entry(), summary)
        self.assertEqual(
            e["cost"],
            {
                "usd": 0.063971,
                "ec2Usd": 0.057192,
                "ebsUsd": 0.006779,
                "lifecycle": "spot",
                "interrupted": False,
                "source": "runs-on-control-plane",
            },
        )
        self.assertNotIn("reason", e)

    def test_interrupted_failure_is_a_spot_interruption(self):
        summary = costs.parse_results(RESPONSE)["109805235447"]
        e = collect.apply_cost(self.entry(status="failure", reason="runner lost"), summary)
        self.assertEqual(e["reason"], "spot interruption")
        self.assertTrue(e["cost"]["interrupted"])

    def test_interrupted_success_keeps_status(self):
        summary = costs.parse_results(RESPONSE)["109805235447"]
        e = collect.apply_cost(self.entry(), summary)
        self.assertEqual(e["status"], "success")
        self.assertNotIn("reason", e)

    def test_missing_summary_or_other_provider(self):
        summary = costs.parse_results(RESPONSE)["109805235446"]
        self.assertNotIn("cost", collect.apply_cost(self.entry(), None))
        self.assertNotIn("cost", collect.apply_cost(self.entry(provider="GitHub"), summary))
        self.assertNotIn("cost", collect.apply_cost(self.entry(status="unavailable"), summary))


if __name__ == "__main__":
    unittest.main()
