"""Exercise collection against disposable repositories, without network access."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_git_activity.py"
SPEC = importlib.util.spec_from_file_location("collect_git_activity", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


class PeriodTests(unittest.TestCase):
    def test_named_periods(self):
        expected = {
            "today": ("2026-11-13", "2026-11-13"),
            "yesterday": ("2026-11-12", "2026-11-12"),
            "this-week": ("2026-11-09", "2026-11-15"),
            "last-week": ("2026-11-02", "2026-11-08"),
        }
        for period, bounds in expected.items():
            with self.subTest(period=period):
                span = collector.resolve_period(period=period, today=date(2026, 11, 13))
                self.assertEqual((str(span.since), str(span.until)), bounds)

    def test_explicit_range_and_invalid_inputs(self):
        span = collector.resolve_period(since="2026-11-01", until="2026-11-13")
        self.assertEqual(span.since, date(2026, 11, 1))
        for kwargs in (
            {"since": "2026-11-13"},
            {"since": "2026-11-13", "until": "2026-11-01"},
            {"since": "bad", "until": "2026-11-01"},
            {"period": "today", "since": "2026-11-01", "until": "2026-11-13"},
            {"timezone": "Invalid/Timezone"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(collector.CollectionError):
                collector.resolve_period(**kwargs)

    def test_utc_works_without_system_timezone_database(self):
        with mock.patch.object(collector, "ZoneInfo", side_effect=collector.ZoneInfoNotFoundError):
            span = collector.resolve_period(timezone="UTC", today=date(2026, 11, 13))
        self.assertEqual(span.bounds()[0].utcoffset().total_seconds(), 0)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.make_repo("sample repo")
        self.span = collector.resolve_period(
            since="2026-11-09", until="2026-11-13", timezone="UTC"
        )

    def git(self, repo, *args, env=None):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True,
            text=True, env=dict(env or os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                                GIT_CONFIG_NOSYSTEM="1"),
        ).stdout.strip()

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Example Author")
        self.git(repo, "config", "user.email", "author@example.invalid")
        return repo

    def commit(self, message, when="2026-11-10T12:00:00+00:00", repo=None, author=None):
        repo = repo or self.repo
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        args = ["commit", "--allow-empty", "-m", message]
        if author:
            args += ["--author", author]
        self.git(repo, *args, env=env)
        return self.git(repo, "rev-parse", "HEAD")

    def collect(self, **kwargs):
        return collector.collect_activity([self.repo], self.span, **kwargs)

    def test_collects_messages_without_remote_urls_paths_or_email(self):
        sha = self.commit("feat: Add search\n\nSupport exact matches.\n保留中文。")
        self.git(self.repo, "remote", "add", "origin", "https://example.invalid/private/repo")
        result = self.collect()
        commit = result["repositories"][0]["commits"][0]
        self.assertEqual(commit["sha"], sha)
        self.assertEqual(commit["subject"], "feat: Add search")
        self.assertIn("保留中文。", commit["body"])
        self.assertEqual(commit["day"], "2026-11-10")
        serialized = json.dumps(result)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn(str(self.repo), serialized)

    def test_range_includes_midnight_and_excludes_next_day(self):
        first = self.commit("Start", "2026-11-09T00:00:00+00:00")
        last = self.commit("End", "2026-11-13T23:59:59+00:00")
        self.commit("Outside", "2026-11-14T00:00:00+00:00")
        shas = [c["sha"] for c in self.collect()["repositories"][0]["commits"]]
        self.assertEqual(shas, [first, last])

    def test_timezone_controls_report_day_and_boundaries(self):
        try:
            collector.ZoneInfo("Asia/Shanghai")
        except collector.ZoneInfoNotFoundError:
            self.skipTest("System timezone database is unavailable")
        self.commit("Local next day", "2026-11-08T17:00:00+00:00")
        span = collector.resolve_period(
            since="2026-11-09", until="2026-11-09", timezone="Asia/Shanghai"
        )
        result = collector.collect_activity([self.repo], span)
        self.assertEqual(result["repositories"][0]["commits"][0]["day"], "2026-11-09")

    def test_old_parent_timestamp_does_not_hide_in_range_ancestors(self):
        included = self.commit("Within range")
        self.commit("Clock moved backwards", "2026-11-01T12:00:00+00:00")
        commits = self.collect()["repositories"][0]["commits"]
        self.assertEqual([c["sha"] for c in commits], [included])

    def test_merged_work_is_included_once_without_merge_boilerplate(self):
        self.commit("Base")
        self.git(self.repo, "checkout", "-b", "feature")
        feature = self.commit("Add export")
        self.git(self.repo, "checkout", "main")
        self.commit("Add filter")
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-11-12T12:00:00Z",
                   GIT_COMMITTER_DATE="2026-11-12T12:00:00Z")
        self.git(self.repo, "merge", "--no-ff", "feature", "-m", "Merge feature", env=env)
        commits = self.collect()["repositories"][0]["commits"]
        self.assertEqual(len(commits), 3)
        self.assertEqual(sum(c["sha"] == feature for c in commits), 1)
        self.assertFalse(any(c["subject"] == "Merge feature" for c in commits))

    def test_author_filter_and_explicit_revision(self):
        own = self.commit("Own work")
        self.commit("Other work", author="Another Author <another@example.invalid>")
        commits = self.collect(authors=["author@example.invalid"])["repositories"][0]["commits"]
        self.assertEqual([c["sha"] for c in commits], [own])
        commits = self.collect(ref=own)["repositories"][0]["commits"]
        self.assertEqual([c["sha"] for c in commits], [own])

    def test_multiple_repos_are_deduplicated_even_from_subdirectories(self):
        self.commit("One")
        subdir = self.repo / "nested"
        subdir.mkdir()
        other = self.make_repo("another repo")
        self.commit("Two", repo=other)
        result = collector.collect_activity([self.repo, subdir, other], self.span)
        self.assertEqual(len(result["repositories"]), 2)
        self.assertEqual(result["commitCount"], 2)

    def test_empty_repo_and_no_matching_commits_are_explicit(self):
        result = self.collect()
        self.assertEqual(result["commitCount"], 0)
        self.assertTrue(result["repositories"][0]["warnings"])
        self.commit("Outside", "2026-10-01T12:00:00Z")
        result = self.collect()
        self.assertEqual(result["repositories"][0]["commits"], [])

    def test_invalid_repo_revision_and_limit_fail_clearly(self):
        self.commit("Work")
        for kwargs in ({"ref": "missing"}, {"ref": "--all"}, {"limit": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(collector.CollectionError):
                self.collect(**kwargs)
        with self.assertRaises(collector.CollectionError):
            collector.collect_activity([self.root], self.span)
        for repos, options in (([], {}), ([self.repo], {"authors": [""]}),
                               ([self.root / "missing"], {})):
            with self.subTest(repos=repos), self.assertRaises(collector.CollectionError):
                collector.collect_activity(repos, self.span, **options)

    def test_unavailable_git_and_timeouts_are_user_facing_errors(self):
        for error in (FileNotFoundError(), PermissionError(),
                      subprocess.TimeoutExpired("git", 30)):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(collector.subprocess, "run", side_effect=error):
                    with self.assertRaises(collector.CollectionError):
                        self.collect()

    def test_failed_git_read_does_not_expose_raw_error_context(self):
        self.commit("Work")
        original = collector._git

        def fail_log(repo, *args):
            if args[0] == "log":
                return subprocess.CompletedProcess(args, 1, "", "private diagnostic context")
            return original(repo, *args)

        with mock.patch.object(collector, "_git", side_effect=fail_log):
            with self.assertRaises(collector.CollectionError) as caught:
                self.collect()
        self.assertNotIn("private diagnostic context", str(caught.exception))

    def test_truncation_and_shallow_history_are_visible(self):
        self.commit("One")
        self.commit("Two")
        repo = self.collect(limit=1)["repositories"][0]
        self.assertEqual(len(repo["commits"]), 1)
        self.assertTrue(repo["truncated"])
        shallow = self.root / "shallow"
        self.git(self.root, "clone", "--depth", "1", self.repo.as_uri(), str(shallow))
        result = collector.collect_activity([shallow], self.span)
        self.assertTrue(result["repositories"][0]["shallow"])
        self.assertTrue(result["repositories"][0]["warnings"])

    def test_cli_json_and_error_contract(self):
        self.commit("完成筛选")
        command = [sys.executable, str(SCRIPT), "--repo", str(self.repo),
                   "--since", "2026-11-09", "--until", "2026-11-13", "--timezone", "UTC"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["commitCount"], 1)
        self.assertEqual(parsed["repositories"][0]["commits"][0]["subject"], "完成筛选")
        self.assertTrue(result.stdout.isascii())
        result = subprocess.run(command + ["--limit", "0"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("limit", result.stderr)

    def test_standalone_script_uses_target_working_directory(self):
        self.commit("Work")
        installed = self.root / "installed skill" / SCRIPT.name
        installed.parent.mkdir()
        shutil.copy2(SCRIPT, installed)
        command = [sys.executable, str(installed), "--since", "2026-11-09",
                   "--until", "2026-11-13", "--timezone", "UTC"]
        result = subprocess.run(command, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["repositories"][0]["name"], self.repo.name)


if __name__ == "__main__":
    unittest.main()
