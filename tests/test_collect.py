"""Self-checks for bin/collect.py: how a job's outcome is classified.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("collect", ROOT / "bin" / "collect.py")
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

LOST = (
    "The self-hosted runner lost communication with the server. Verify the machine is running and has a "
    "healthy network connection."
)
PLANNED = {"key": "r--1", "id": "r", "provider": "P", "arch": "x64", "iteration": 1}


def step(number, name, conclusion, status="completed", start="2026-09-30T08:00:00Z", end="2026-09-30T08:01:00Z"):
    return {
        "number": number,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": start,
        "completed_at": end if status == "completed" else None,
    }


def job(conclusion, steps):
    return {
        "id": 1,
        "conclusion": conclusion,
        "status": "completed",
        "created_at": "2026-09-30T07:59:50Z",
        "started_at": "2026-09-30T08:00:00Z",
        "completed_at": "2026-09-30T08:10:00Z",
        "runner_name": "runner-1",
        "html_url": "https://github.com/o/r/actions/runs/1/job/1",
        "steps": steps,
    }


def summarize(j, errors=None, log=None, unavailable=False, warnings=None):
    # annotations() gives {} when the job raised none, None when the API failed.
    notes = None if unavailable else {k: v for k, v in (("errors", errors), ("warnings", warnings)) if v}
    with mock.patch.object(collect, "annotations", return_value=notes), mock.patch.object(
        collect, "job_log", return_value=log
    ), tempfile.TemporaryDirectory() as tmp:
        return collect.summarize_job(PLANNED, j, pathlib.Path(tmp), repo="o/r")


class Classify(unittest.TestCase):
    def test_success(self):
        e = summarize(job("success", [step(1, "Set up job", "success"), step(2, "phase: build", "success")]))
        self.assertEqual(e["status"], "success")
        self.assertIsNone(e["failedAt"])
        self.assertEqual(e["workloadSeconds"], 60)
        self.assertNotIn("logTail", e)

    def test_harness_step_failing_after_phases_is_success(self):
        e = summarize(job("failure", [step(1, "phase: build", "success"), step(2, "Disk report", "failure")]))
        self.assertEqual(e["status"], "success")

    def test_phase_failure(self):
        log = "2026-09-30T08:00:30.1Z compiling\n2026-09-30T08:00:59.9Z ##[error]Process completed with exit code 1.\n"
        e = summarize(
            job("failure", [step(1, "phase: build", "failure"), step(2, "phase: test", "skipped")]),
            errors=["Process completed with exit code 1."],
            log=log,
        )
        self.assertEqual(e["status"], "failure")
        self.assertEqual(e["failedAt"], "build")
        self.assertIsNone(e["workloadSeconds"])
        self.assertEqual(e["logTail"][-1], "##[error]Process completed with exit code 1.")

    def test_oom_killed_phase_is_a_failure(self):
        # A step killed by the OOM killer reports "cancelled" inside a failed job.
        e = summarize(job("failure", [step(1, "phase: install", "success"), step(2, "phase: typecheck", "cancelled")]))
        self.assertEqual(e["status"], "failure")
        self.assertEqual(e["failedAt"], "typecheck")
        self.assertEqual(e["reason"], "step killed")

    def test_runner_lost_mid_phase_is_a_failure(self):
        e = summarize(
            job(
                "failure",
                [
                    step(1, "phase: buildx setup", "success"),
                    step(2, "phase: docker build", None, status="in_progress"),
                    step(3, "Upload", None, status="pending"),
                ],
            ),
            errors=[LOST],
        )
        self.assertEqual(e["status"], "failure")
        self.assertEqual(e["reason"], "runner lost")
        self.assertEqual(e["failedAt"], "docker build")
        self.assertIn("annotations", e)

    def test_runner_lost_without_annotations_is_still_a_failure(self):
        e = summarize(job("failure", [step(1, "phase: typecheck", None, status="in_progress")]))
        self.assertEqual(e["status"], "failure")
        self.assertEqual(e["reason"], "runner lost")

    def test_runner_lost_with_killed_step_is_runner_lost(self):
        e = summarize(job("failure", [step(1, "phase: typecheck", "cancelled")]), errors=[LOST])
        self.assertEqual((e["status"], e["reason"]), ("failure", "runner lost"))

    def test_cancelled_run(self):
        e = summarize(
            job("cancelled", [step(1, "phase: build", "cancelled"), step(2, "phase: test", "skipped")]),
            errors=["The operation was canceled."],
        )
        self.assertEqual(e["status"], "cancelled")
        self.assertNotIn("queueSeconds", e)

    def test_cancelled_job_whose_runner_was_lost_is_a_failure(self):
        e = summarize(job("cancelled", [step(1, "phase: build", None, status="in_progress")]), errors=[LOST])
        self.assertEqual((e["status"], e["reason"]), ("failure", "runner lost"))

    def test_timed_out_job_is_a_failure(self):
        e = summarize(
            job("cancelled", [step(1, "phase: build", "cancelled")]),
            errors=["The job has exceeded the maximum execution time of 45m0s"],
        )
        self.assertEqual((e["status"], e["reason"]), ("failure", "timed out"))

    def test_cancelled_with_phase_in_progress_and_no_annotations_is_runner_lost(self):
        # The annotations call failed (rate limit, 5xx): an unfinished phase
        # still says the runner went away.
        e = summarize(
            job("cancelled", [step(1, "phase: build", None, status="in_progress")]), unavailable=True
        )
        self.assertEqual((e["status"], e["reason"]), ("failure", "runner lost"))

    def test_cancelled_without_annotations_is_not_called_cancelled(self):
        # Could be a timeout or a cancelled run: don't drop it on missing evidence.
        e = summarize(job("cancelled", [step(1, "phase: build", "cancelled")]), unavailable=True)
        self.assertEqual((e["status"], e["reason"]), ("failure", "unknown (annotations unavailable)"))

    def test_cache_entry_evicted_mid_restore_measured_nothing(self):
        # GitHub deleted the entry while it downloaded: the repository's cache
        # storage limit, shared by every runner on GitHub's cache storage.
        e = summarize(
            job("failure", [step(1, "phase: cache save", "success"), step(2, "phase: cache restore", "failure")]),
            errors=["Failed to restore cache entry. Exiting as fail-on-cache-miss is set. Input key: bench-r--1-1-1"],
            warnings=["Failed to restore: The specified blob does not exist."],
        )
        self.assertEqual((e["status"], e["reason"]), ("cancelled", collect.EVICTED_REASON))

    def test_blob_warning_outside_the_restore_is_still_a_failure(self):
        e = summarize(
            job("failure", [step(1, "phase: cache save", "failure")]),
            warnings=["Failed to restore: The specified blob does not exist."],
        )
        self.assertEqual(e["status"], "failure")

    def test_job_never_created_is_not_the_runners_doing(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = collect.summarize_job(PLANNED, None, pathlib.Path(tmp), repo="o/r")
        self.assertEqual((e["status"], e["reason"]), ("cancelled", collect.NEVER_CREATED))

    def test_never_started_is_unavailable(self):
        e = summarize({"id": 1, "conclusion": "cancelled", "started_at": None, "steps": []})
        self.assertEqual(e["status"], "unavailable")


class LogTail(unittest.TestCase):
    def test_window_and_redaction(self):
        text = "\n".join(
            [
                "2026-09-30T08:00:59.5Z previous step",
                "2026-09-30T08:01:00.2Z \x1b[36;1mstarting\x1b[0m",
                "2026-09-30T08:01:30.0Z curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123'",
                "2026-09-30T08:01:31.0Z GITHUB_TOKEN=ghs_abcdefghijklmnopqrstuvwxyz0123",
                "2026-09-30T08:02:00.9Z ##[error]boom",
                "2026-09-30T08:02:01.0Z Post job cleanup.",
            ]
        )
        failed = step(1, "phase: x", "failure", start="2026-09-30T08:01:00Z", end="2026-09-30T08:02:00Z")
        tail = collect.log_tail(text, failed)
        self.assertEqual(tail[0], "starting")
        self.assertEqual(tail[-1], "##[error]boom")
        self.assertNotIn("abcdefghij", " ".join(tail))
        self.assertIn("Bearer ***", tail[1])

    def test_step_header_and_env_block_are_dropped(self):
        # Shape of a real RunsOn job log (job 109805232429): the failing step's
        # header group holds its script and an env block with RunsOn's
        # variables; the step itself wrote two lines. Values are made up.
        header = [
            "##[group]Run # `bun typecheck` is `bun turbo typecheck`.",
            "\x1b[36;1m# `bun typecheck` is `bun turbo typecheck`.\x1b[0m",
            "\x1b[36;1mexit \"$status\"\x1b[0m",
            "shell: /usr/bin/bash -e {0}",
            "env:",
            "  COMMIT: 08fb47373509ba64b13441061314eeacf4264f51",
            "  RUNS_ON_VERSION: v3.4.0-rc.1",
            "  RUNS_ON_INSTANCE_ID: i-0123456789abcdef0",
            "  RUNS_ON_STACK_NAME: demo-stack",
            "  RUNS_ON_S3_BUCKET_CACHE: demo-stack-s3bucketcache-abc123def",
            "  RUNS_ON_RUNNER_NAME: runs-on--i-0123456789abcdef0--17bf370d-a",
            "##[endgroup]",
            "##[group]@opencode-ai/core:typecheck",
            "cache miss, executing 81c22208e92d78a8",
            "##[endgroup]",
            " ERROR  run failed: command  exited (137) on runs-on--i-0123456789abcdef0",
            "##[error]The operation was canceled.",
        ]
        # Real logs start with a byte-order mark.
        text = "\ufeff" + "\n".join(
            ["2026-09-30T08:31:31.1Z Current runner version: '2.337.0'"] + [f"2026-09-30T08:32:08.4Z {l}" for l in header]
        )
        failed = step(1, "phase: typecheck", "cancelled", start="2026-09-30T08:32:08Z", end="2026-09-30T08:32:08Z")
        tail = collect.log_tail(text, failed)
        joined = "\n".join(tail)
        self.assertEqual(tail[0], "Run # `bun typecheck` is `bun turbo typecheck`.")
        self.assertIn("@opencode-ai/core:typecheck", joined)  # the workload's own groups stay
        self.assertIn("cache miss", joined)
        self.assertEqual(tail[-1], "##[error]The operation was canceled.")
        for leaked in ("RUNS_ON", "s3bucketcache", "0123456789abcdef0", "demo-stack", "COMMIT", "shell:", "runner version"):
            self.assertNotIn(leaked, joined)

    def test_env_block_outside_a_group_and_provider_names_are_redacted(self):
        text = "\n".join(
            [
                "2026-09-30T08:01:00.0Z env:",
                "2026-09-30T08:01:00.0Z   RUNS_ON_AMI_ID: ami-1",
                "2026-09-30T08:01:01.0Z RUNS_ON_S3_BUCKET_CACHE=demo-s3bucketcache-xyz",
                "2026-09-30T08:01:02.0Z upload to s3://demo-v3-s3bucketcache-xyz/key from i-0abc12345678",
                "2026-09-30T08:01:03.0Z ##[error]boom",
            ]
        )
        tail = collect.log_tail(text, None)
        joined = "\n".join(tail)
        self.assertNotIn("ami-1", joined)
        self.assertNotIn("xyz", joined)
        self.assertNotIn("0abc12345678", joined)
        self.assertIn("i-***", joined)
        self.assertEqual(tail[-1], "##[error]boom")

    def test_caps_lines(self):
        text = "\n".join(f"2026-09-30T08:01:{i % 60:02d}.0Z line {i}" for i in range(100))
        self.assertEqual(len(collect.log_tail(text, None)), collect.TAIL_LINES)


if __name__ == "__main__":
    unittest.main()
