"""Exercise the real CLI against a disposable HTTP service."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from integration_helpers import item, raw_order, settings_file
from reporting.cli import main


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report.py"


class FakeService(BaseHTTPRequestHandler):
    orders = None
    writes = None
    fail_read = False
    redirect = False

    def log_message(self, format, *args):
        pass

    def respond(self, payload, status=200):
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = self.path.split("?")[0]
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "/unexpected")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.fail_read:
            self.respond({}, 403)
        elif path.endswith("/user"):
            self.respond({"id": 1, "email": "author@example.invalid"})
        elif path.endswith("/repository/commits"):
            self.respond([{"id": "a" * 40, "title": "Add search", "message": "Add search",
                           "parent_ids": [], "committed_date": "2026-01-06T12:00:00Z",
                           "author_email": "author@example.invalid"}])
        else:
            self.respond({"id": 1, "path_with_namespace": "example/project"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        condition = body["condition"]
        if condition["op"] == "schedule":
            days = [{"time": "2026-01-06", "orders": self.orders}] if condition["year"] == 2026 and condition["month"] == 1 else []
            self.respond({"code": 200, "datas": {"2.1": days}})
        else:
            if (condition.get("reportDate") != "2026-01-06" or condition.get("otherMatters") != []):
                self.respond({"code": 400})
                return
            self.writes.append(condition["rows"])
            for row in condition["rows"]:
                for order in self.orders:
                    if order["id"] == row["ewlOrderId"]:
                        order["report"] = {**row, "id": row.get("id", "report-1")}
            self.respond({"code": 200})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.handler = type("IsolatedService", (FakeService,), {
            "orders": [raw_order()], "writes": [], "fail_read": False, "redirect": False,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        url = "http://127.0.0.1:" + str(self.server.server_port)
        self.config = settings_file(self.root, gitlab={"base_url": url, "allow_http": True, "projects": [{"id": 1}]},
                                    eom={"base_url": url, "allow_http": True, "worker_id": "worker-1"})
        self.options = ["--config", str(self.config), "--since", "2026-01-06", "--until", "2026-01-06"]
        self.environment = mock.patch.dict(os.environ, {"GITLAB_TOKEN": "fixture-credential", "EOM_TOKEN": "fixture-credential"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_cli(self, *args, expected=0):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main([*args, *self.options])
        self.assertEqual(code, expected, error.getvalue())
        self.assertNotIn("fixture-credential", output.getvalue() + error.getvalue())
        return json.loads(output.getvalue()) if output.getvalue() else error.getvalue()

    def test_material_apply_show_export_submit_and_amend_end_to_end(self):
        self.assertEqual(len(self.run_cli("material")["pending"]), 1)
        file = self.root / "items.json"
        file.write_text(json.dumps({"items": [item()]}))
        self.assertEqual(self.run_cli("apply", str(file))["passed"], 1)
        self.assertTrue(self.run_cli("show")["days"])
        review = self.run_cli("review")
        self.assertTrue(Path(review["path"]).exists())
        exported = self.root / "report.md"
        self.run_cli("export", "--output", str(exported))
        self.assertIn("估算", exported.read_text(encoding="utf-8"))
        self.assertEqual(self.run_cli("submit", "--dry-run")["days"][0]["status"], "ready")
        self.assertFalse(self.handler.writes)
        self.assertEqual(self.run_cli("submit")["days"][0]["status"], "submitted")
        file.write_text(json.dumps({"items": [{"day": "2026-01-06", "orderId": "order-1", "rate": 50}]}))
        self.assertEqual(len(self.run_cli("amend", str(file))["changes"]), 1)
        self.assertEqual(self.run_cli("submit")["days"][0]["status"], "submitted")
        self.assertEqual(self.handler.writes[-1][0]["op"], 8)

    def test_revise_text_end_to_end_preserves_rate_and_hours(self):
        self.run_cli("material")
        file = self.root / "items.json"
        file.write_text(json.dumps({"items": [item()]}))
        self.run_cli("apply", str(file))
        self.run_cli("submit")
        entry = {key: value for key, value in item().items() if key not in {"rate", "hours"}}
        entry["task"] = ["Revise business text"]
        file.write_text(json.dumps({"items": [entry]}))
        proposal = self.run_cli("revise", str(file))
        self.assertEqual(len(proposal["changes"]), 1)
        self.run_cli("material")
        self.assertEqual(self.run_cli("submit", "--dry-run")["days"][0]["status"], "ready")
        self.assertEqual(len(self.handler.writes), 1)
        self.assertEqual(self.run_cli("submit")["days"][0]["status"], "submitted")
        row = self.handler.writes[-1][0]
        self.assertEqual(row["op"], 8)
        self.assertEqual(row["ewdRate"], 40)
        self.assertEqual(row["ewdWorkRealTimes"], 8)
        self.assertIn("Revise business text", row["memo"])
        self.run_cli("revise", str(file), "--mode", "gitlab", expected=1)

    def test_validation_failure_and_blocked_submission_are_nonzero_json(self):
        self.run_cli("material")
        file = self.root / "items.json"
        file.write_text(json.dumps({"items": [item(hours=10)]}))
        self.assertTrue(self.run_cli("apply", str(file), expected=1)["failures"])
        self.assertEqual(self.run_cli("submit", expected=1)["days"][0]["status"], "blocked")

    def test_gitlab_only_needs_no_eom_credential(self):
        os.environ.pop("EOM_TOKEN", None)
        self.run_cli("material", "--mode", "gitlab")
        file = self.root / "text.json"
        file.write_text(json.dumps({"markdown": "# Progress\n\nAdded search."}))
        self.run_cli("apply", str(file), "--mode", "gitlab")
        self.assertIn("Added search", self.run_cli("export", "--mode", "gitlab")["markdown"])
        self.run_cli("submit", "--mode", "gitlab", expected=1)

    def test_offline_view_needs_no_tokens_and_submission_needs_only_eom(self):
        self.run_cli("material")
        file = self.root / "items.json"
        file.write_text(json.dumps({"items": [item()]}))
        self.run_cli("apply", str(file))
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ.pop("EOM_TOKEN", None)
        self.assertTrue(self.run_cli("show")["days"])
        self.assertTrue(self.run_cli("export")["markdown"])
        self.run_cli("submit", expected=1)
        os.environ["EOM_TOKEN"] = "fixture-credential"
        self.assertEqual(self.run_cli("submit")["days"][0]["status"], "submitted")

    def test_direct_config_tokens_work_without_exports_and_never_enter_saved_report(self):
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        for service in ("gitlab", "eom"):
            section = payload["profiles"]["default"][service]
            section.pop("token_env", None)
            section["token"] = "fixture-credential"
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ.pop("EOM_TOKEN", None)
        collected = self.run_cli("material")
        file = self.root / "items.json"
        file.write_text(json.dumps({"items": [item()]}))
        self.run_cli("apply", str(file))
        self.run_cli("show")
        self.run_cli("export")
        self.assertNotIn("fixture-credential", Path(collected["path"]).read_text(encoding="utf-8"))
        self.assertEqual(self.run_cli("submit")["days"][0]["status"], "submitted")
        for path in (self.root / "state").rglob("*.json"):
            self.assertNotIn("fixture-credential", path.read_text(encoding="utf-8"))

    def test_real_transport_does_not_follow_redirect_or_turn_permission_failure_into_empty_material(self):
        for flag in ("redirect", "fail_read"):
            setattr(self.handler, flag, True)
            message = self.run_cli("material", expected=1)
            self.assertIsInstance(message, str)
            setattr(self.handler, flag, False)

    def test_standalone_help_and_arguments_before_subcommand(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("material", result.stdout)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([*self.options, "material"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
