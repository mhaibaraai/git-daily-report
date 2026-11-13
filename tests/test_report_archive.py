"""Persistent, versioned review artifacts are required before EOM writes."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integration_helpers import MemoryEom, MemoryGitlab, item, raw_order, settings_file
from collect_git_activity import resolve_period
from reporting.common import ReportError
from reporting.config import load_profile
from reporting.workflow import material, apply_items
from reporting.submission import submit_report


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = load_profile(settings_file(self.root))
        self.span = resolve_period(since='2026-01-06', until='2026-01-06', timezone='UTC')
        self.eom = MemoryEom({'2026-01-06': [raw_order()]})
        material(self.profile, self.span, eom=self.eom, gitlab=MemoryGitlab())

    def test_apply_creates_readable_versioned_archive(self):
        first = apply_items(self.profile, self.span, {'items': [item()]})
        path = Path(first['reviewPath'])
        self.assertTrue(path.is_relative_to(self.root / 'reports' / '2026'))
        self.assertIn('具体任务：完善筛选', path.read_text(encoding='utf-8'))
        self.assertIn('明日计划：验证筛选结果', path.read_text(encoding='utf-8'))
        self.assertEqual(apply_items(self.profile, self.span, {'items': [item()]})['reviewPath'], str(path))
        changed = apply_items(self.profile, self.span, {'items': [dict(item(), task=['Different work'])]})
        self.assertNotEqual(changed['reviewPath'], str(path))
        self.assertIn('完善筛选', path.read_text(encoding='utf-8'))

    def test_submit_recreates_archive_before_sending(self):
        result = apply_items(self.profile, self.span, {'items': [item()]})
        Path(result['reviewPath']).unlink()
        def after_write(client):
            self.assertTrue(Path(result['reviewPath']).is_file())
        self.eom.after_write = after_write
        submitted = submit_report(self.profile, self.span, client=self.eom)
        self.assertEqual(submitted['days'][0]['status'], 'submitted')
        self.assertEqual(submitted['reviewPath'], result['reviewPath'])

    def test_archive_failure_prevents_remote_write(self):
        apply_items(self.profile, self.span, {'items': [item()]})
        with mock.patch('reporting.submission.archive_report', side_effect=ReportError('Archive failed')):
            with self.assertRaisesRegex(ReportError, 'Archive failed'):
                submit_report(self.profile, self.span, client=self.eom)
        self.assertFalse(self.eom.writes)

    def test_review_command_works_without_service_calls(self):
        from reporting.cli import build_parser, _dispatch
        apply_items(self.profile, self.span, {'items': [item()]})
        result = _dispatch(build_parser().parse_args(['review']), self.profile, self.span)
        self.assertIn('完善筛选', result['markdown'])
        self.assertTrue(Path(result['path']).exists())

    def test_update_envelope_includes_report_date_and_other_matters(self):
        from types import SimpleNamespace
        from reporting.eom import EomClient
        from reporting.config import EomSettings
        transport = mock.Mock()
        transport.request.return_value = SimpleNamespace(data={'code': 200})
        client = EomClient(EomSettings(base_url='https://example.invalid', worker_id='worker-1', token='fixture'), transport=transport)
        rows = [{'ewdDate': '2026-01-06', 'op': 8, 'id': 'report-1'}]
        client.commit_report(rows)
        condition = transport.request.call_args.kwargs['body']['condition']
        self.assertEqual(condition, {'op': 'commitReport', 'rows': rows, 'reportDate': '2026-01-06', 'otherMatters': []})
        self.assertEqual(transport.request.call_args.kwargs['body']['buttonId'], 'increase')
        with self.assertRaises(ReportError):
            client.commit_report([*rows, {'ewdDate': '2026-01-07'}])

    def test_corrupt_existing_archive_and_unwritable_root_fail_safely(self):
        from dataclasses import replace
        from reporting.archive import archive_report
        from reporting.store import load_report
        first = apply_items(self.profile, self.span, {'items': [item()]})
        Path(first['reviewPath']).write_text('Corrupted', encoding='utf-8')
        with self.assertRaisesRegex(ReportError, 'digest'):
            submit_report(self.profile, self.span, client=self.eom)
        self.assertFalse(self.eom.writes)
        blocked = self.root / 'not-a-directory'
        blocked.write_text('file')
        with self.assertRaisesRegex(ReportError, 'Cannot save'):
            archive_report(replace(self.profile, archive_dir=blocked), load_report(self.profile, self.span))

    def test_custom_archive_folder_is_resolved_from_config(self):
        profile = load_profile(settings_file(self.root, archive_dir='annual-reports'))
        self.assertEqual(profile.archive_dir, self.root / 'annual-reports')
        with self.assertRaises(ReportError):
            load_profile(settings_file(self.root, archive_dir=42))
