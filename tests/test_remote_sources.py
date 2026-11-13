"""Contract tests for optional remote-source adapters."""

from datetime import date
import io
from http.client import IncompleteRead
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from integration_helpers import raw_order, saved_report, settings_file
from collect_git_activity import resolve_period
from reporting.common import ReportError
from reporting.config import load_profile
from reporting.http import HttpResult, HttpTransport, ServiceError
from reporting.gitlab import GitlabClient
from reporting.eom import EomClient


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = settings_file(self.root)
        self.profile = load_profile(self.path)
        self.span = resolve_period(since="2026-01-05", until="2026-01-09", timezone="UTC")
        self.env = {"GITLAB_TOKEN": "fixture-credential", "EOM_TOKEN": "fixture-credential"}


class ConfigurationTests(FixtureCase):
    def test_config_has_no_credentials_and_paths_are_relative_to_config(self):
        self.path = settings_file(self.root, workspace="state")
        profile = load_profile(self.path)
        self.assertEqual(profile.workspace, self.root / "state")
        self.assertNotIn("fixture-credential", repr(profile))
        self.assertEqual(profile.daily_hours, 8)

    def test_missing_daily_budget_is_allowed_for_gitlab_only(self):
        profile = load_profile(settings_file(self.root, daily_hours=None, eom=None))
        self.assertIsNone(profile.daily_hours)

    def test_rejects_invalid_values_and_unknown_profiles(self):
        cases = ({"daily_hours": True}, {"daily_hours": -1}, {"timezone": "Unknown/Zone"},
                 {"gitlab": {"base_url": "https://example.invalid", "token": ""}},
                 {"workspace": str(Path(__file__).resolve().parents[1] / "scripts")},
                 {"eom": {"base_url": "https://example.invalid"}},
                 {"gitlab": {"base_url": "https://user:do-not-echo@example.invalid"}})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ReportError) as caught:
                load_profile(settings_file(self.root, **changes))
            self.assertNotIn("do-not-echo", str(caught.exception))
        with self.assertRaises(ReportError):
            load_profile(self.path, "missing")

    def test_instance_and_worker_determine_eom_scope(self):
        first = self.profile.scope("work-orders")
        other = load_profile(settings_file(self.root, eom={
            "base_url": "https://work.example.invalid", "worker_id": "worker-2"}))
        self.assertNotEqual(first, other.scope("work-orders"))

    def test_invalid_credential_header_is_rejected_without_echoing_value(self):
        for credential in ("private\ncontext", "凭证", "private\tcontext"):
            with self.subTest(credential=credential):
                with self.assertRaises(ReportError) as caught:
                    GitlabClient(self.profile.gitlab, environ={"GITLAB_TOKEN": credential})
                self.assertNotIn(credential, str(caught.exception))

    def test_direct_tokens_authenticate_both_clients_without_environment_and_stay_out_of_repr(self):
        credentials = {"gitlab": "fixture-direct-gitlab", "eom": "fixture-direct-eom"}
        profile = load_profile(settings_file(self.root,
            gitlab={"base_url": "https://gitlab.example.invalid", "token": credentials["gitlab"]},
            eom={"base_url": "https://work.example.invalid", "worker_id": "worker-1", "token": credentials["eom"]}))
        gitlab = GitlabClient(profile.gitlab, environ={"GITLAB_TOKEN": "ignored-environment-value"})
        eom = EomClient(profile.eom, environ={})
        self.assertEqual(gitlab.headers["PRIVATE-TOKEN"], credentials["gitlab"])
        self.assertEqual(eom.headers["token"], credentials["eom"])
        for credential in credentials.values():
            self.assertNotIn(credential, repr(profile))
            self.assertNotIn(credential, repr(profile.gitlab))
            self.assertNotIn(credential, repr(profile.eom))

    def test_direct_token_and_environment_selector_are_mutually_exclusive(self):
        for service in ("gitlab", "eom"):
            section = {"base_url": "https://example.invalid", "token": "fixture-direct-secret",
                       "token_env": "SERVICE_TOKEN"}
            if service == "eom":
                section["worker_id"] = "worker-1"
            with self.subTest(service=service):
                with self.assertRaisesRegex(ReportError, "either token or token_env") as caught:
                    load_profile(settings_file(self.root, **{service: section}))
                self.assertNotIn("fixture-direct-secret", str(caught.exception))

    def test_invalid_direct_tokens_are_rejected_without_echoing_the_value(self):
        for value in (None, "", True, 123, "fixture\nsecret", "fixture secret", "凭证"):
            with self.subTest(value_type=type(value).__name__):
                for service in ("gitlab", "eom"):
                    section = {"base_url": "https://example.invalid", "token": value}
                    if service == "eom":
                        section["worker_id"] = "worker-1"
                    with self.assertRaises(ReportError) as caught:
                        load_profile(settings_file(self.root, **{service: section}))
                    if isinstance(value, str) and value:
                        self.assertNotIn(value, str(caught.exception))


class HttpTests(unittest.TestCase):
    def test_read_retries_but_write_is_attempted_once(self):
        for read_only, expected in ((True, 3), (False, 1)):
            transport = HttpTransport()
            with mock.patch.object(transport.opener, "open", side_effect=URLError("private context")) as call:
                with mock.patch("reporting.http.time.sleep"), self.assertRaises(ServiceError) as caught:
                    transport.request("POST", "https://example.invalid", read_only=read_only)
            self.assertEqual(call.call_count, expected)
            self.assertNotIn("private context", str(caught.exception))

    def test_redirects_and_auth_errors_are_not_retried(self):
        for status in (302, 401, 403):
            transport = HttpTransport()
            error = HTTPError("https://example.invalid", status, "private", {}, io.BytesIO(b"private"))
            with mock.patch.object(transport.opener, "open", side_effect=error) as call:
                with self.assertRaises(ServiceError):
                    transport.request("GET", "https://example.invalid")
            self.assertEqual(call.call_count, 1)

    def test_bad_json_is_not_reported_as_empty_success(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"not json"
        response.headers = {}
        transport = HttpTransport()
        with mock.patch.object(transport.opener, "open", return_value=response):
            with self.assertRaises(ServiceError):
                transport.request("GET", "https://example.invalid")

    def test_interrupted_response_becomes_an_unknown_write_without_retry(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = IncompleteRead(b"partial")
        transport = HttpTransport()
        with mock.patch.object(transport.opener, "open", return_value=response) as call:
            with self.assertRaises(ServiceError):
                transport.request("POST", "https://example.invalid", read_only=False)
        self.assertEqual(call.call_count, 1)


class GitlabTests(FixtureCase):
    def client(self, responder, settings=None):
        transport = mock.Mock()
        transport.request.side_effect = responder
        return GitlabClient(settings or self.profile.gitlab, transport, self.env), transport

    def test_explicit_branch_is_read_without_push_events_and_authors_are_filtered(self):
        profile = load_profile(settings_file(self.root, gitlab={"base_url": "https://gitlab.example.invalid",
                                       "projects": [{"id": "example/project", "ref": "feature"}]}))
        requests = []

        def respond(method, url, **kwargs):
            requests.append((url, kwargs.get("params", {})))
            if url.endswith("/user"):
                return HttpResult({"id": 1, "email": "author@example.invalid"})
            if url.endswith("/repository/commits"):
                commit = {"id": "a" * 40, "title": "Add search", "message": "Add search",
                          "committed_date": "2026-01-06T12:00:00Z", "author_email": "author@example.invalid"}
                return HttpResult([commit, {**commit, "id": "b" * 40, "author_email": "other@example.invalid"}])
            return HttpResult({"id": 1, "path_with_namespace": "example/project"})

        client, _ = self.client(respond, profile.gitlab)
        result = client.collect(self.span)
        self.assertEqual(len(result["commits"]), 1)
        self.assertTrue(any(params.get("ref_name") == "feature" for _, params in requests))
        self.assertFalse(any(url.endswith("/events") for url, _ in requests))

    def test_discovery_pagination_and_merge_expansion_keep_integration_provenance(self):
        profile = load_profile(settings_file(self.root, gitlab={"base_url": "https://gitlab.example.invalid"}))
        def respond(method, url, **kwargs):
            if url.endswith("/user"):
                return HttpResult({"id": 1, "email": "author@example.invalid"})
            if url.endswith("/events"):
                if kwargs["params"]["page"] == 1:
                    return HttpResult([{"project_id": 1}], {"x-next-page": "2"})
                return HttpResult([])
            if url.endswith("/repository/commits"):
                return HttpResult([{"id": "m" * 40, "title": "Merge feature", "parent_ids": ["p1", "p2"],
                                    "committed_date": "2026-01-06T12:00:00Z",
                                    "author_email": "author@example.invalid"}])
            if url.endswith("/repository/compare"):
                return HttpResult({"commits": [{"id": "a" * 40, "title": "Add search",
                                                "committed_date": "2026-01-01T12:00:00Z"}]})
            return HttpResult({"id": 1, "path_with_namespace": "example/project"})
        client, transport = self.client(respond, profile.gitlab)
        result = client.collect(self.span)
        self.assertEqual(result["commits"][0]["contribution"], "integrated")
        self.assertEqual(result["commits"][0]["day"], "2026-01-06")
        self.assertEqual(sum(c.kwargs.get("params", {}).get("page") == 2 for c in transport.request.call_args_list), 1)

    def test_partial_failure_and_pagination_limits_are_visible(self):
        profile = load_profile(settings_file(self.root, gitlab={"base_url": "https://gitlab.example.invalid",
                                       "max_pages": 1, "all_authors": True, "projects": [{"id": 1}]}))
        def respond(method, url, **kwargs):
            if url.endswith("/repository/commits"):
                return HttpResult([], {"x-next-page": "2"})
            return HttpResult({"id": 1, "path_with_namespace": "example/project"})
        client, _ = self.client(respond, profile.gitlab)
        self.assertTrue(client.collect(self.span)["warnings"])
        client, _ = self.client(lambda *a, **k: (_ for _ in ()).throw(ServiceError("Read failed")))
        with self.assertRaises(ReportError):
            client.collect(self.span)


class EomTests(FixtureCase):
    def client(self, months):
        def respond(method, url, **kwargs):
            condition = kwargs["body"]["condition"]
            return HttpResult({"code": 200, "datas": {"2.1": months.get((condition["year"], condition["month"]), [])}})
        transport = mock.Mock()
        transport.request.side_effect = respond
        return EomClient(self.profile.eom, transport, self.env), transport

    def test_cross_month_padding_future_dates_and_historical_bounds(self):
        history = raw_order(report=saved_report())
        history["ewlStartTime"] = "2025-12-01"
        current = raw_order()
        current["ewlStartTime"] = "2025-12-01"
        client, transport = self.client({
            (2025, 12): [{"time": "2025-12-31", "orders": [history]},
                         {"time": "2026-01-06", "orders": [current]}],
            (2026, 1): [{"time": "2025-12-31", "orders": [history]},
                        {"time": "2026-01-06", "orders": [current]},
                        {"time": "2026-01-07", "orders": [raw_order("other-id", saved_report(rate=90))]},
                        {"time": "2026-01-10", "orders": [current]}],
        })
        result = client.schedule(date(2025, 12, 31), date(2026, 1, 10), today=date(2026, 1, 9))
        self.assertEqual(set(result), {"2025-12-31", "2026-01-06", "2026-01-07"})
        self.assertEqual(result["2026-01-06"][0]["_rateMin"], 20)
        self.assertIsNone(result["2026-01-06"][0]["_rateMax"])
        self.assertEqual(transport.request.call_count, 2)

    def test_wrong_worker_malformed_response_and_auth_fail_closed(self):
        for payload in ({"code": 200, "datas": {"2.1": "invalid"}}, {"code": 401}, []):
            transport = mock.Mock()
            transport.request.return_value = HttpResult(payload)
            client = EomClient(self.profile.eom, transport, self.env)
            with self.subTest(payload=payload), self.assertRaises(ReportError):
                client.schedule(date(2026, 1, 6), date(2026, 1, 6), today=date(2026, 1, 9))
        wrong = raw_order()
        wrong["ewlWorker"] = "another-worker"
        client, _ = self.client({(2026, 1): [{"time": "2026-01-06", "orders": [wrong]}]})
        with self.assertRaises(ReportError):
            client.schedule(date(2026, 1, 6), date(2026, 1, 6), today=date(2026, 1, 9))

    def test_malformed_order_text_or_saved_identity_is_rejected_before_persistence(self):
        for raw in (dict(raw_order(), ewlTitle={"invalid": "text"}),
                    raw_order(report={**saved_report(), "id": True})):
            client, _ = self.client({(2026, 1): [{"time": "2026-01-06", "orders": [raw]}]})
            with self.subTest(raw=raw), self.assertRaises(ReportError):
                client.schedule(date(2026, 1, 6), date(2026, 1, 6), today=date(2026, 1, 9))


if __name__ == "__main__":
    unittest.main()
