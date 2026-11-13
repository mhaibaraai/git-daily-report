"""Read GitLab evidence and retain the distinction between authorship and integration."""

from datetime import timedelta
from urllib.parse import quote

from .common import ReportError, identifier, timestamp
from .config import token_from
from .http import HttpTransport, ServiceError


PAGE_SIZE = 100


class GitlabClient:
    def __init__(self, settings, transport=None, environ=None):
        if settings is None:
            raise ReportError("Configure a GitLab source.")
        self.settings = settings
        self.transport = transport or HttpTransport()
        self.headers = {"PRIVATE-TOKEN": token_from(settings, environ)}
        self.warnings = []

    def _get(self, path, params=None):
        return self.transport.request("GET", self.settings.base_url + "/api/v4" + path,
                                      headers=self.headers, params=params or {})

    def _pages(self, path, params):
        rows, page, visited = [], 1, set()
        for _ in range(self.settings.max_pages):
            if page in visited:
                raise ServiceError("GitLab returned cyclic pagination.")
            visited.add(page)
            response = self._get(path, {**params, "per_page": PAGE_SIZE, "page": page})
            if not isinstance(response.data, list) or any(not isinstance(row, dict) for row in response.data):
                raise ServiceError("GitLab returned an invalid list response.")
            rows.extend(response.data)
            headers = {key.lower(): value for key, value in response.headers.items()}
            next_page = headers.get("x-next-page")
            if not next_page:
                if "x-next-page" in headers or len(response.data) < PAGE_SIZE:
                    return rows
                next_page = page + 1
            try:
                page = int(next_page)
                if page < 1:
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise ServiceError("GitLab returned invalid pagination.") from exc
        self.warnings.append("GitLab pagination limit reached; the material may be incomplete.")
        return rows

    def _identities(self):
        if self.settings.all_authors:
            return None
        if self.settings.authors:
            return set(self.settings.authors)
        user = self._get("/user").data
        if not isinstance(user, dict):
            raise ServiceError("GitLab returned an invalid account response.")
        identities = {str(user[key]).strip().casefold() for key in ("email", "commit_email", "name", "username")
                      if isinstance(user.get(key), str) and user[key].strip()}
        if not identities:
            raise ReportError("Cannot determine GitLab authorship; configure exact authors or all_authors.")
        return identities

    def _discover(self, span):
        events = self._pages("/events", {
            "action": "pushed", "after": str(span.since - timedelta(days=1)),
            "before": str(span.until + timedelta(days=1)),
        })
        self.warnings.append("Project discovery uses retained push events; configure projects for complete coverage.")
        return [{"id": str(event["project_id"])} for event in events
                if type(event.get("project_id")) is int]

    def collect(self, span, projects=None):
        self.warnings = []
        identities = self._identities()
        targets = [*self.settings.projects, *(projects or [])]
        if not targets:
            targets = self._discover(span)
        targets = list({(str(row["id"]), row.get("ref")): row for row in targets}.values())
        by_key, succeeded = {}, 0
        for target in targets:
            try:
                commits = self._project(target, span, identities)
                succeeded += 1
                for commit in commits:
                    key = (commit["projectId"], commit["sha"])
                    previous = by_key.get(key)
                    if previous is None or commit["contribution"] == "authored":
                        by_key[key] = commit
            except ServiceError:
                self.warnings.append("A requested GitLab project could not be fully read.")
        if targets and not succeeded:
            raise ServiceError("No requested GitLab project could be read.")
        commits = sorted(by_key.values(), key=lambda row: (row["day"], row["projectId"], row["sha"]))
        return {"commits": commits, "warnings": list(dict.fromkeys(self.warnings)),
                "authorFiltered": identities is not None}

    def _project(self, target, span, identities):
        project_key = quote(identifier(target["id"]), safe="")
        path = "/projects/" + project_key
        project = self._get(path).data
        if not isinstance(project, dict) or not project.get("path_with_namespace") or project.get("id") is None:
            raise ServiceError("GitLab returned invalid project metadata.")
        start, end = span.bounds()
        params = {"since": start.isoformat(), "until": (end - timedelta(seconds=1)).isoformat()}
        params.update({"ref_name": target["ref"]} if target.get("ref") else {"all": "true"})
        rows = self._pages(path + "/repository/commits", params)
        commits = []
        for raw in rows:
            instant = timestamp(raw.get("committed_date") or raw.get("created_at"))
            if not start <= instant < end or not self._authored(raw, identities):
                continue
            parents = raw.get("parent_ids", [])
            if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
                raise ServiceError("GitLab returned invalid commit parents.")
            day = instant.astimezone(span.timezone).date().isoformat()
            if len(parents) >= 2:
                commits.extend(self._integration(path, project, raw, day))
            else:
                commits.append(self._commit(project, raw, day, "authored"))
        return commits

    @staticmethod
    def _authored(raw, identities):
        if identities is None:
            return True
        return bool(identities & {str(raw.get(key) or "").strip().casefold()
                                  for key in ("author_email", "author_name")})

    def _integration(self, path, project, merge, day):
        parents = merge["parent_ids"]
        try:
            payload = self._get(path + "/repository/compare", {
                "from": parents[0], "to": parents[1], "straight": "true",
            }).data
            if not isinstance(payload, dict) or not isinstance(payload.get("commits"), list):
                raise ServiceError("GitLab returned an invalid comparison.")
            rows = payload["commits"]
            if payload.get("compare_timeout") or len(rows) > self.settings.merge_limit:
                self.warnings.append("A merge comparison was limited or timed out; inspect the integration before reporting.")
            expanded = [self._commit(project, raw, day, "integrated", identifier(merge.get("id")))
                        for raw in rows[:self.settings.merge_limit] if isinstance(raw, dict)]
            if expanded:
                return expanded
        except ReportError:
            self.warnings.append("A merge could not be expanded; only its integration record is available.")
        return [self._commit(project, merge, day, "integrated")]

    @staticmethod
    def _commit(project, raw, day, contribution, merge_sha=None):
        sha = identifier(raw.get("id"))
        subject = raw.get("title", "")
        message = raw.get("message", subject)
        if not isinstance(subject, str) or not isinstance(message, str):
            raise ServiceError("GitLab returned invalid commit text.")
        return {"source": "gitlab", "projectId": identifier(project["id"]),
                "project": project["path_with_namespace"], "sha": sha, "day": day,
                "subject": subject, "body": message, "contribution": contribution,
                "integrationSha": merge_sha, "committedAt": raw.get("committed_date")}
