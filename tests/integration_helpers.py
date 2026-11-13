"""Synthetic service fixtures; no real accounts or projects are used."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def settings_file(root, **changes):
    profile = {
        "workspace": str(root / "state"), "timezone": "UTC", "daily_hours": 8,
        "gitlab": {"base_url": "https://gitlab.example.invalid", "projects": [{"id": 1}]},
        "eom": {"base_url": "https://work.example.invalid", "worker_id": "worker-1"},
    }
    profile.update(changes)
    path = root / "settings.local.json"
    path.write_text(json.dumps({"profiles": {"default": profile}}), encoding="utf-8")
    return path


def raw_order(order_id="order-1", report=None):
    return {
        "id": order_id, "ewlTitle": "Search", "eTaskTitle": "Improve lookup",
        "eProjectName": "Example project", "ewlWorkPlanTimes": 16,
        "ewlWorker": "worker-1", "ewlWorkerName": "Example Worker", "ewlDept": "department-1",
        "ewlNum": "TASK-1", "ewlStartTime": "2026-01-05", "ewlEndTime": "2026-01-09",
        "eProjectId": "project-1", "eTaskId": "task-1", "type": 1, "report": report,
    }


def saved_report(rate=20, hours=2, day="2026-01-05", order_id="order-1"):
    return {"id": "report-1", "ewlOrderId": order_id, "ewdDate": day,
            "memo": "具体任务：梳理筛选规则\n关键数据：无\n问题解决过程：无",
            "ewdProblem": "无", "tomorrowPlan": "完善筛选", "ewdRate": rate,
            "ewdWorkRealTimes": hours}


def item(day="2026-01-06", order_id="order-1", rate=40, hours=8):
    return {"day": day, "orderId": order_id, "task": ["完善筛选"], "data": [],
            "problem": [], "issue": "无", "plan": "验证筛选结果", "rate": rate, "hours": hours}


class MemoryEom:
    def __init__(self, days):
        self.days = days
        self.writes = []
        self.failure = None
        self.after_write = None

    def schedule(self, since, until, today=None):
        import copy
        return {day: copy.deepcopy(orders) for day, orders in self.days.items()
                if str(since) <= day <= str(until)}

    def commit_report(self, rows):
        import copy
        self.writes.append(copy.deepcopy(rows))
        if self.failure:
            raise self.failure
        for row in rows:
            for raw in self.days[row["ewdDate"]]:
                if raw["id"] == row["ewlOrderId"]:
                    raw["report"] = {**row, "id": row.get("id", "saved-" + raw["id"])}
        if self.after_write:
            self.after_write(self)


class MemoryGitlab:
    def __init__(self):
        self.calls = []

    def collect(self, span, projects=None):
        self.calls.append((span, projects))
        return {"commits": [{"sha": "a" * 40, "project": "example/project", "projectId": "1",
                             "day": "2026-01-06", "subject": "Add search", "body": "",
                             "contribution": "authored"}], "warnings": []}
