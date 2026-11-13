"""Business-only text and conflict-safe corrections of existing reports."""

import copy
import tempfile
import unittest
from pathlib import Path

from integration_helpers import MemoryEom, MemoryGitlab, item, raw_order, saved_report, settings_file
from collect_git_activity import resolve_period
from reporting.common import ReportError
from reporting.config import load_profile
from reporting.store import load_report, report_path, write_state
from reporting.workflow import apply_items, material
from reporting.submission import submit_report


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = load_profile(settings_file(self.root))
        self.span = resolve_period(since='2026-01-06', until='2026-01-06', timezone='UTC')
        self.eom = MemoryEom({'2026-01-06': [raw_order(report=saved_report(day='2026-01-06'))]})
        material(self.profile, self.span, eom=self.eom, gitlab=MemoryGitlab())

    def revise(self, **changes):
        from reporting.revisions import revise
        entry = {key: value for key, value in item().items() if key not in {'rate', 'hours'}}
        entry.update(changes)
        return revise(self.profile, self.span, {'items': [entry]}, client=self.eom)

    def test_generation_notes_are_rejected_in_each_business_field(self):
        self.eom.days['2026-01-06'][0]['report'] = None
        material(self.profile, self.span, eom=self.eom, gitlab=MemoryGitlab())
        for key in ('task', 'data', 'problem', 'issue', 'plan'):
            entry = item()
            entry[key] = ['按用户要求依据本周素材与排期分配'] if key in {'task', 'data', 'problem'} else '非实际提交日期或实测工时'
            result = apply_items(self.profile, self.span, {'items': [entry]})
            self.assertTrue(result['failures'], key)

    def test_business_unknowns_render_as_none_and_real_issues_remain(self):
        from reporting.validation import generated_progress
        result = generated_progress(item())
        self.assertIn('关键数据：无\n问题解决过程：无', result['memo'])
        self.assertEqual(result['issue'], '无')
        result = generated_progress(dict(item(), issue='接口超时，待确认服务状态', data=['工作量估算 10–17 人日']))
        self.assertIn('估算', result['memo'])
        self.assertIn('待确认', result['issue'])

    def test_legacy_draft_is_blocked_at_submit_preflight(self):
        self.eom.days['2026-01-06'][0]['report'] = None
        material(self.profile, self.span, eom=self.eom, gitlab=MemoryGitlab())
        apply_items(self.profile, self.span, {'items': [item()]})
        report = load_report(self.profile, self.span)
        report['days'][0]['orders'][0]['progress']['memo'] = '按用户要求进行主题分配，不代表当日提交'
        write_state(report_path(self.profile, self.span), report)
        result = submit_report(self.profile, self.span, client=self.eom, dry_run=True)
        self.assertEqual(result['days'][0]['status'], 'blocked')
        self.assertIn('memo', result['days'][0]['message'])
        self.assertFalse(self.eom.writes)

    def test_revision_survives_refresh_then_updates_text_only(self):
        original = copy.deepcopy(self.eom.days['2026-01-06'][0]['report'])
        self.assertEqual(len(self.revise()['changes']), 1)
        material(self.profile, self.span, eom=self.eom, gitlab=MemoryGitlab())
        self.assertEqual(load_report(self.profile, self.span)['days'][0]['orders'][0]['progress']['source'], 'revise')
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom, dry_run=True)['days'][0]['status'], 'ready')
        self.assertFalse(self.eom.writes)
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom)['days'][0]['status'], 'submitted')
        saved = self.eom.days['2026-01-06'][0]['report']
        for key in ('id', 'ewdRate', 'ewdWorkRealTimes'):
            self.assertEqual(saved[key], original[key])
        self.assertIn('完善筛选', saved['memo'])
        self.assertEqual(self.eom.writes[0][0]['op'], 8)

    def test_any_baseline_change_blocks_and_keeps_proposal(self):
        for field, value in [('memo', 'changed'), ('ewdProblem', 'changed'), ('tomorrowPlan', 'changed'),
                             ('id', 'other'), ('ewdRate', 30), ('ewdWorkRealTimes', 3)]:
            with self.subTest(field=field):
                self.eom.days['2026-01-06'][0]['report'] = saved_report(day='2026-01-06')
                self.revise()
                self.eom.days['2026-01-06'][0]['report'][field] = value
                result = submit_report(self.profile, self.span, client=self.eom)
                self.assertEqual(result['days'][0]['status'], 'blocked')
                self.assertEqual(load_report(self.profile, self.span)['days'][0]['orders'][0]['progress']['source'], 'revise')
        self.assertFalse(self.eom.writes)

    def test_revision_rejects_numeric_changes_and_unknown_orders(self):
        for change in ({'rate': 100}, {'hours': 8}, {'orderId': 'other'}):
            with self.subTest(change=change), self.assertRaises(ReportError):
                self.revise(**change)

    def test_unknown_revision_does_not_automatically_repeat(self):
        from reporting.http import ServiceError
        self.revise()
        self.eom.failure = ServiceError('timeout')
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom)['days'][0]['status'], 'unknown')
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom)['days'][0]['status'], 'unknown')
        self.assertEqual(len(self.eom.writes), 1)
        self.eom.failure = None
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom, retry_unknown=True)['days'][0]['status'], 'submitted')

    def test_noop_clears_revision_and_malformed_baseline_is_rejected(self):
        from reporting.validation import generated_progress
        from reporting.revisions import snapshot
        self.revise()
        progress = generated_progress(item())
        progress['reportId'] = 'report-1'
        self.eom.days['2026-01-06'][0]['report'] = snapshot(progress)
        self.assertFalse(self.revise()['changes'])
        self.revise(task=['Different task'])
        report = load_report(self.profile, self.span)
        report['days'][0]['orders'][0]['progress']['originalReport'] = {}
        write_state(report_path(self.profile, self.span), report)
        with self.assertRaises(ReportError):
            load_report(self.profile, self.span)

    def test_revision_input_is_strict_and_missing_live_report_blocks(self):
        from reporting.revisions import revision_items
        for payload in ({'items': 'bad'}, {'items': [{}]}, {'items': [item()]},
                        {'items': [{key: value for key, value in item().items() if key not in {'rate', 'hours'}}] * 2}):
            with self.subTest(payload=payload), self.assertRaises(ReportError):
                revision_items(payload)
        self.revise()
        self.eom.days['2026-01-06'][0]['report'] = None
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom)['days'][0]['status'], 'blocked')
        self.assertFalse(self.eom.writes)

    def test_revision_readback_conflict_stops_repeated_writes(self):
        self.revise()
        self.eom.after_write = lambda client: client.days['2026-01-06'][0]['report'].update(memo='Other writer')
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom)['days'][0]['status'], 'conflict')
        self.assertEqual(submit_report(self.profile, self.span, client=self.eom, retry_unknown=True)['days'][0]['status'], 'conflict')
        self.assertEqual(len(self.eom.writes), 1)
