"""Report lifecycle, validation, persistence and write-safety tests."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from integration_helpers import MemoryEom, MemoryGitlab, item, raw_order, saved_report, settings_file
from collect_git_activity import resolve_period
from reporting.common import ReportError
from reporting.config import load_profile
from reporting.http import ServiceError
from reporting.store import load_report, report_path, read_state, lock
from reporting.workflow import material, apply_items, amend, export_markdown
from reporting.submission import submit_report, journal_path


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = load_profile(settings_file(self.root))
        self.span = resolve_period(since="2026-01-06", until="2026-01-06", timezone="UTC")
        self.eom = MemoryEom({"2026-01-06": [raw_order()]})
        self.gitlab = MemoryGitlab()

    def prepare(self, items=None):
        result = material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        applied = apply_items(self.profile, self.span, {"items": items or [item()]})
        self.assertFalse(applied["failures"], applied)
        self.assertFalse(applied["missing"], applied)
        return result

    def submit(self, **kwargs):
        return submit_report(self.profile, self.span, client=self.eom, **kwargs)

    def test_material_preserves_drafts_and_configured_mapping_is_always_requested(self):
        self.profile = load_profile(settings_file(self.root, mappings=[{
            "order_id": "order-1", "gitlab_project": "example/mapped", "ref": "feature"}]))
        first = self.prepare()
        self.assertEqual(first["pending"], [{"day": "2026-01-06", "orderId": "order-1"}])
        self.assertEqual(self.gitlab.calls[0][1], [{"id": "example/mapped", "ref": "feature"}])
        self.assertEqual(str(self.gitlab.calls[0][0].since), "2026-01-03")
        result = material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        self.assertFalse(result["pending"])
        report = load_report(self.profile, self.span)
        self.assertEqual(report["days"][0]["orders"][0]["progress"]["rate"], 40)

    def test_refresh_returns_invalid_drafts_for_rewrite(self):
        self.prepare()
        self.profile = replace(self.profile, daily_hours=6)
        result = material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        self.assertEqual(result["pending"], [{"day": "2026-01-06", "orderId": "order-1"}])
        self.assertTrue(result["validation"]["failures"])

    def test_removed_drafts_require_explicit_archival_before_other_orders_can_submit(self):
        self.eom.days["2026-01-06"].append(raw_order("order-2"))
        self.prepare([item(hours=4), item(order_id="order-2", hours=4)])
        self.eom.days["2026-01-06"].pop()
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        self.assertEqual(self.submit()["days"][0]["status"], "blocked")
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom, discard_stale=True)
        report = load_report(self.profile, self.span)
        self.assertEqual(report["archivedOrders"][0]["order"]["id"], "order-2")
        self.assertEqual(self.submit()["days"][0]["status"], "submitted")

    def test_empty_schedule_does_not_require_gitlab_or_generate_work(self):
        self.eom.days = {}
        result = material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        self.assertEqual(result["pending"], [])
        self.assertEqual(self.gitlab.calls, [])

    def test_partial_apply_rechecks_daily_hours_and_different_ids_are_independent(self):
        self.eom.days["2026-01-06"].append(raw_order("order-2"))
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        result = apply_items(self.profile, self.span, {"items": [item(hours=5)]})
        self.assertEqual(len(result["missing"]), 1)
        result = apply_items(self.profile, self.span, {"items": [item(order_id="order-2", rate=10, hours=4)]})
        self.assertEqual(len(result["failures"]), 2)
        result = apply_items(self.profile, self.span, {"items": [item(order_id="order-2", rate=10, hours=3)]})
        self.assertFalse(result["failures"])
        self.assertEqual(result["passed"], 2)

    def test_schema_duplicate_unknown_and_numeric_values_are_validated(self):
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        for payload in ({"items": [item(), item()]}, {"items": [item(order_id="unknown")]},
                        {"items": [dict(item(), hours=True)]}, {"items": [dict(item(), plan="")]},
                        {"items": [dict(item(), rate=float("nan"))]}, {"items": "bad"}):
            with self.subTest(payload=payload):
                try:
                    result = apply_items(self.profile, self.span, payload)
                    self.assertTrue(result["failures"])
                except ReportError:
                    pass

    def test_historical_bounds_and_existing_hours_are_enforced(self):
        self.eom.days["2026-01-06"] = [
            {**raw_order(), "_rateMin": 30, "_rateMax": 60},
            raw_order("order-2", saved_report(hours=3, order_id="order-2", day="2026-01-06")),
        ]
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        for changes in ({"rate": 20, "hours": 5}, {"rate": 70, "hours": 5}, {"hours": 6}):
            result = apply_items(self.profile, self.span, {"items": [{**item(), **changes}]})
            self.assertTrue(result["failures"])
        result = apply_items(self.profile, self.span, {"items": [item(hours=5)]})
        self.assertFalse(result["failures"])
        progress = load_report(self.profile, self.span)["days"][0]["orders"][0]["progress"]
        self.assertEqual(progress["estimatedFields"], ["rate", "hours"])

    def test_no_daily_budget_blocks_workorders_but_not_gitlab_report(self):
        self.profile = load_profile(settings_file(self.root, daily_hours=None))
        with self.assertRaises(ReportError):
            material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        material(self.profile, self.span, mode="gitlab", gitlab=self.gitlab)
        result = apply_items(self.profile, self.span, {"markdown": "# Progress\n\nAdded search."}, mode="gitlab")
        self.assertEqual(result["passed"], 1)
        self.assertIn("Added search", export_markdown(load_report(self.profile, self.span, "gitlab")))

    def test_dry_run_does_not_change_report_or_journal(self):
        self.prepare()
        path = report_path(self.profile, self.span)
        before = path.read_bytes()
        result = self.submit(dry_run=True)
        self.assertEqual(result["days"][0]["status"], "ready")
        self.assertEqual(self.eom.writes, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(journal_path(self.profile).exists())

    def test_submit_uses_fresh_metadata_and_readback_then_skips_existing(self):
        self.prepare()
        self.eom.days["2026-01-06"][0]["ewlTitle"] = "New live title"
        result = self.submit()
        self.assertEqual(result["days"][0]["status"], "submitted")
        self.assertEqual(self.eom.writes[0][0]["ewdTitle"], "New live title")
        self.assertEqual(self.eom.writes[0][0]["op"], 4)
        self.assertEqual(self.submit()["days"][0]["status"], "skipped")
        self.assertEqual(len(self.eom.writes), 1)

    def test_changed_or_missing_schedule_blocks_submission(self):
        for change in ("add", "remove", "metadata"):
            with self.subTest(change=change):
                self.profile = load_profile(settings_file(self.root, workspace=str(self.root / ("state-" + change))))
                self.eom = MemoryEom({"2026-01-06": [raw_order()]})
                self.prepare()
                if change == "add":
                    self.eom.days["2026-01-06"].append(raw_order("new"))
                elif change == "remove":
                    self.eom.days = {}
                else:
                    self.eom.days["2026-01-06"][0].pop("ewlWorkPlanTimes")
                self.assertEqual(self.submit()["days"][0]["status"], "blocked")
                self.assertFalse(self.eom.writes)

    def test_timeout_is_persisted_and_not_automatically_retried(self):
        self.prepare()
        self.eom.failure = ServiceError("Timed out")
        self.assertEqual(self.submit()["days"][0]["status"], "unknown")
        self.assertEqual(self.submit()["days"][0]["status"], "unknown")
        self.assertEqual(len(self.eom.writes), 1)
        self.eom.failure = None
        self.assertEqual(self.submit(retry_unknown=True)["days"][0]["status"], "submitted")
        self.assertEqual(len(self.eom.writes), 2)

    def test_timeout_with_saved_readback_is_success(self):
        self.prepare()
        def timeout_after_write(client):
            raise ServiceError("Timed out after server saved")
        self.eom.after_write = timeout_after_write
        self.assertEqual(self.submit()["days"][0]["status"], "submitted")

    def test_unknown_result_blocks_later_dates_and_overlapping_reports(self):
        self.span = resolve_period(since="2026-01-06", until="2026-01-07", timezone="UTC")
        self.eom.days["2026-01-07"] = [raw_order()]
        self.prepare([item(), item(day="2026-01-07", rate=60)])
        self.eom.failure = ServiceError("Timed out")
        result = self.submit()
        self.assertEqual([day["status"] for day in result["days"]], ["unknown", "blocked"])
        self.span = resolve_period(since="2026-01-06", until="2026-01-06", timezone="UTC")
        self.prepare()
        self.assertEqual(self.submit()["days"][0]["status"], "unknown")
        self.assertEqual(len(self.eom.writes), 1)

    def test_readback_conflict_and_partial_writes_block_retry(self):
        self.eom.days["2026-01-06"].append(raw_order("order-2"))
        self.prepare([item(hours=4), item(order_id="order-2", hours=4)])
        def corrupt(client):
            client.days["2026-01-06"][0]["report"]["memo"] = "Different text"
        self.eom.after_write = corrupt
        self.assertEqual(self.submit()["days"][0]["status"], "conflict")
        self.assertEqual(self.submit(retry_unknown=True)["days"][0]["status"], "conflict")
        self.assertEqual(len(self.eom.writes), 1)

    def test_rejection_is_reported_without_repeating_write(self):
        self.prepare()
        self.eom.failure = ServiceError("Rejected", definitive=True)
        self.assertEqual(self.submit()["days"][0]["status"], "failed")
        self.assertEqual(len(self.eom.writes), 1)

    def test_crash_pending_and_corrupt_journal_fail_closed(self):
        self.prepare()
        self.eom.failure = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.submit()
        self.eom.failure = None
        self.assertEqual(self.submit()["days"][0]["status"], "unknown")
        path = journal_path(self.profile)
        path.write_text('{"2026-01-06": {"status": "pending", "rows": []}}')
        with self.assertRaises(ReportError):
            self.submit()
        self.assertEqual(len(self.eom.writes), 1)

    def test_amend_preserves_text_and_hours_and_detects_changed_original_rate(self):
        self.eom.days["2026-01-06"][0]["report"] = saved_report(day="2026-01-06")
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        proposal = {"items": [{"day": "2026-01-06", "orderId": "order-1", "rate": 30}]}
        result = amend(self.profile, self.span, proposal, client=self.eom)
        self.assertEqual(result["changes"][0]["from"], 20)
        self.assertEqual(self.submit()["days"][0]["status"], "submitted")
        row = self.eom.writes[0][0]
        self.assertEqual(row["op"], 8)
        self.assertEqual(row["ewdWorkRealTimes"], 2)
        self.assertEqual(row["memo"], saved_report()["memo"])
        amend(self.profile, self.span, {"items": [{"day": "2026-01-06", "orderId": "order-1", "rate": 40}]}, client=self.eom)
        self.eom.days["2026-01-06"][0]["report"]["ewdRate"] = 35
        self.assertEqual(self.submit()["days"][0]["status"], "blocked")

    def test_lock_conflict_and_corrupt_report_do_not_overwrite_state(self):
        self.prepare()
        path = report_path(self.profile, self.span)
        with lock(path):
            with self.assertRaises(ReportError):
                apply_items(self.profile, self.span, {"items": [item()]})
        path.write_text("not json")
        with self.assertRaises(ReportError):
            material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        self.assertEqual(path.read_text(), "not json")

    def test_scope_mismatch_does_not_allow_report_reuse(self):
        self.prepare()
        path = report_path(self.profile, self.span)
        report = read_state(path)
        report["scope"] = "another-instance"
        path.write_text(json.dumps(report))
        with self.assertRaises(ReportError):
            self.submit()

    def test_correction_can_be_reproposed_after_live_original_changes(self):
        self.eom.days["2026-01-06"][0]["report"] = saved_report(day="2026-01-06")
        material(self.profile, self.span, gitlab=self.gitlab, eom=self.eom)
        proposal = {"items": [{"day": "2026-01-06", "orderId": "order-1", "rate": 40}]}
        amend(self.profile, self.span, proposal, client=self.eom)
        self.eom.days["2026-01-06"][0]["report"]["ewdRate"] = 25
        self.assertEqual(self.submit()["days"][0]["status"], "blocked")
        result = amend(self.profile, self.span, proposal, client=self.eom)
        self.assertEqual(result["changes"][0]["from"], 25)
        self.assertEqual(self.submit()["days"][0]["status"], "submitted")

    def test_malformed_saved_correction_fails_with_a_safe_error(self):
        self.prepare()
        path = report_path(self.profile, self.span)
        report = read_state(path)
        report["days"][0]["orders"][0]["progress"] = {"source": "amend"}
        path.write_text(json.dumps(report))
        with self.assertRaises(ReportError):
            self.submit()


if __name__ == "__main__":
    unittest.main()
