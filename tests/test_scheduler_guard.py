"""Scheduler safety tests: GitHub API and dispatch are always simulated."""
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.error import HTTPError

import scheduler_guard as guard
from collection_run import run_slot

DAY = "2026-11-17"
ENV = dict(GITHUB_REPOSITORY="example/spm", GITHUB_TOKEN="test-secret", GITHUB_RUN_ID="10",
           GITHUB_SHA="a" * 40, GITHUB_RUN_ATTEMPT="1", GITHUB_REF="refs/heads/main")


def stamp(value="09:50", day=DAY):
    return datetime.fromisoformat(f"{day}T{value}:00").replace(tzinfo=guard.KYIV)


class FakeGitHub:
    def __init__(self):
        self.markers = {}
        self.active = []
        self.runs = []
        self.jobs = []
        self.dispatches = []
        self.lock = threading.Lock()
        self.fail_dispatch = False

    def marker(self, kind, day, period):
        return self.markers.get((kind, day, period))

    def claim(self, kind, day, period, request_id):
        with self.lock:
            key = kind, day, period
            if key in self.markers:
                return False
            self.markers[key] = dict(request_id=request_id)
            return True

    def listing(self, path, field, **params):
        if field == "jobs":
            return self.jobs
        return self.active if params.get("status") == "in_progress" else [] if "status" in params else self.runs

    def request(self, path, method="GET", body=None):
        self.dispatches.append(body)
        if self.fail_dispatch:
            raise guard.GitHubError("Delivery uncertain")
        return {}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(guard.os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = FakeGitHub()

    def check(self, api=None, period="morning", now=None):
        now = now or stamp()
        return guard.check(api or self.api, period, now, now, "10-1")

    def test_completed_marker_prevents_reserve(self):
        self.api.claim("completed", DAY, "morning", "main")
        self.assertEqual(self.check(), "completed")
        self.assertFalse(self.api.dispatches)

    def test_real_steps_not_workflow_success_determine_result(self):
        steps = [dict(name="Check Kyiv slot and prepare rollback copy", conclusion="success"),
                 dict(name="Collect prices", conclusion="skipped")]
        self.assertEqual(guard.outcome(steps), "skipped")
        steps[1]["conclusion"] = "failure"
        self.assertEqual(guard.outcome(steps), "failed")
        for name in ("Validate history boundaries and capture this collection", "Persist validated observations and diagnostics", "Confirm successful collection"):
            steps.append(dict(name=name, conclusion="success"))
        steps[1]["conclusion"] = "success"
        self.assertEqual(guard.outcome(steps), "completed")
        steps[-1]["conclusion"] = "skipped"  # e.g. all SKU failed despite collector exit handling.
        self.assertEqual(guard.outcome(steps), "failed")

    def test_missing_and_skipped_collection_request_exact_period(self):
        for skipped in (False, True):
            api = FakeGitHub()
            if skipped:
                api.runs = [dict(id=1, display_title="SPM collect | morning | scheduled", created_at=stamp("09:30").isoformat(), status="completed")]
                api.jobs = [dict(name="collect", steps=[dict(name="Check Kyiv slot and prepare rollback copy", conclusion="success"), dict(name="Collect prices", conclusion="skipped")])]
            self.assertEqual(self.check(api), "reserve requested")
            self.assertEqual(api.dispatches[0], dict(ref="main", inputs=dict(period="morning", target_date=DAY, reserve_request="10-1")))

    def test_duplicate_checks_and_duplicate_reserve_execution(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.check(), range(2)))
        self.assertEqual(len(self.api.dispatches), 1)
        self.assertTrue(guard.may_collect(self.api, DAY, "morning", "10-1"))
        self.assertFalse(guard.may_collect(self.api, DAY, "morning", "10-1"))
        with self.assertRaises(guard.GitHubError):
            guard.may_collect(self.api, DAY, "evening", "10-1")

    def test_active_primary_and_race_after_claim(self):
        self.api.active = [dict(id=20)]
        self.assertEqual(self.check(), "active")
        self.assertFalse(self.api.markers)
        self.api.active = []
        original = self.api.claim
        def claim(*args):
            result = original(*args)
            self.api.active = [dict(id=20)]
            return result
        with patch.object(self.api, "claim", side_effect=claim):
            self.assertEqual(self.check(), "reserve suppressed after recheck")
        self.assertFalse(self.api.dispatches)

    def test_late_primary_after_reserve_completed_does_not_recollect(self):
        self.api.claim("completed", DAY, "morning", "reserve")
        self.assertFalse(guard.may_collect(self.api, DAY, "morning", ""))

    def test_unknown_api_state_and_ambiguous_dispatch_fail_closed(self):
        with patch.object(self.api, "listing", side_effect=guard.GitHubError("API down")):
            with self.assertRaises(guard.GitHubError):
                self.check()
        self.assertFalse(self.api.markers)
        self.assertFalse(self.api.dispatches)
        self.api.fail_dispatch = True
        with self.assertRaises(guard.GitHubError):
            self.check()
        self.assertEqual(self.check(), "reserve already requested")
        self.assertEqual(len(self.api.dispatches), 1)

    def test_claim_created_but_response_lost_never_retries_dispatch(self):
        original = self.api.claim
        def lost_response(*args):
            original(*args)
            raise guard.GitHubError("Claim response lost")
        with patch.object(self.api, "claim", side_effect=lost_response):
            with self.assertRaises(guard.GitHubError):
                self.check()
        self.assertEqual(self.check(), "reserve already requested")
        self.assertFalse(self.api.dispatches)

    def test_delayed_morning_midnight_and_dst(self):
        self.assertFalse(run_slot("30 9 * * *", stamp("09:30"), stamp("17:20"))[1])
        self.assertFalse(run_slot("", stamp("09:50"), stamp("17:20"), "morning", DAY)[1])
        self.assertEqual(guard.check(self.api, "evening", stamp("17:20"), stamp("17:20") + timedelta(days=1), "id"), "skipped: outside check window")
        for day, offset in (("2026-03-29", 3), ("2026-10-25", 2)):
            for period, clock in (("morning", "09:50"), ("evening", "17:20")):
                now = stamp(clock, day)
                self.assertEqual(now.utcoffset(), timedelta(hours=offset))
                self.assertEqual(guard.check(FakeGitHub(), period, now, now, "id"), "reserve requested")
        self.assertEqual(self.check(now=stamp("09:49")), "skipped: outside check window")
        with self.assertRaises(ValueError):
            run_slot("", stamp(), stamp())

    def test_legacy_run_is_not_evidence_of_absence(self):
        self.api.runs = [dict(id=1, display_title="Old workflow title", status="completed")]
        with self.assertRaises(guard.GitHubError):
            self.check()
        self.assertFalse(self.api.dispatches)

    def test_legacy_actual_completed_collection_prevents_duplicate(self):
        self.api.runs = [dict(id=1, display_title="Daily supplier price report", status="completed")]
        names = ["Collect prices", "Validate history boundaries and capture this collection",
                 "Persist validated observations and diagnostics", "Preserve immutable morning snapshot"]
        self.api.jobs = [dict(name="collect", conclusion="success", steps=[dict(name=n, conclusion="success", started_at=stamp("09:35").isoformat()) for n in names])]
        self.assertEqual(self.check(), "completed")
        self.assertFalse(self.api.dispatches)
        self.api.jobs[0]["conclusion"] = "failure"
        with self.assertRaises(guard.GitHubError):
            self.check()

    def test_completed_jobs_when_marker_write_failed(self):
        self.api.runs = [dict(id=1, display_title=f"SPM collect | morning | {DAY}", status="completed")]
        names = ["Collect prices", "Validate history boundaries and capture this collection",
                 "Persist validated observations and diagnostics", "Confirm successful collection"]
        self.api.jobs = [dict(name="collect", steps=[dict(name=n, conclusion="success") for n in names])]
        self.assertEqual(self.check(), "completed")
        self.assertFalse(self.api.dispatches)

    def test_atomic_ref_create_no_patch_no_retry_and_safe_errors(self):
        api = guard.GitHub()
        calls = []
        refs = set()
        def respond(request, **kwargs):
            calls.append(request)
            data = json.loads(request.data)
            if request.full_url.endswith("/git/tags"):
                return io.BytesIO(b'{"sha":"tag-sha"}')
            self.assertEqual(request.get_method(), "POST")
            self.assertTrue(request.full_url.endswith("/git/refs"))
            if data["ref"] in refs:
                raise HTTPError(request.full_url, 422, "secret", {}, None)
            refs.add(data["ref"])
            return io.BytesIO(b'{"ref":"created"}')
        with patch.object(guard, "urlopen", side_effect=respond):
            self.assertTrue(api.claim("reserve", DAY, "morning", "one"))
            self.assertFalse(api.claim("reserve", DAY, "morning", "two"))
        for error in (TimeoutError("test-secret"), HTTPError("url", 403, "test-secret", {}, None)):
            with patch.object(guard, "urlopen", side_effect=error) as request:
                with self.assertRaises(guard.GitHubError) as caught:
                    api.claim("reserve", DAY, "morning", "three")
                self.assertNotIn("test-secret", str(caught.exception))
                request.assert_called_once()

    def test_incomplete_pagination_is_not_empty(self):
        api = guard.GitHub()
        with patch.object(api, "request", return_value=dict(total_count=2, workflow_runs=[])):
            with self.assertRaises(guard.GitHubError):
                api.listing("runs", "workflow_runs")


if __name__ == "__main__":
    unittest.main()
