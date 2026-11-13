"""Bounded JSON transport with read-only retries and no redirects."""

from dataclasses import dataclass, field
from http.client import HTTPException
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import ReportError, parse_json


TIMEOUT_SECONDS = 30
READ_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ServiceError(ReportError):
    def __init__(self, message, *, definitive=False, status=None):
        super().__init__(message)
        self.definitive = definitive
        self.status = status


@dataclass
class HttpResult:
    data: object
    headers: dict = field(default_factory=dict)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def request(self, method, url, *, headers=None, params=None, body=None, read_only=True):
        if params:
            url += "?" + urlencode(params)
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(body, allow_nan=False).encode("utf-8")
        request = Request(url, data=data, method=method, headers=request_headers)
        attempts = READ_ATTEMPTS if read_only else 1
        for attempt in range(attempts):
            try:
                return self._open(request)
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if read_only and status in RETRY_STATUSES and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                message = "Service rejected credentials or permission." if status in {401, 403} else "Service request failed."
                raise ServiceError(message, status=status, definitive=400 <= status < 500 and status not in {408, 429}) from None
            except (URLError, OSError, TimeoutError, HTTPException):
                if read_only and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise ServiceError("Service connection failed or timed out.") from None
        raise ServiceError("Service request could not finish.")

    def _open(self, request):
        with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ServiceError("Service response exceeds the supported size.")
            try:
                data = parse_json(raw)
            except ReportError as exc:
                raise ServiceError("Service returned invalid JSON.") from exc
            return HttpResult(data, {key.lower(): value for key, value in response.headers.items()})
