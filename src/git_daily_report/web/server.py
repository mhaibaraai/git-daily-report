"""本机网页服务。

日常入口就是这个页面：选周、拉取并生成、逐块复制。只绑 127.0.0.1，凭证留在
服务端，EOM / GitLab / AI 的请求全由这里发出。

用 ThreadingHTTPServer 而不是 HTTPServer——生成是 SSE 长连接，单线程会把后续
请求全堵住。
"""

from __future__ import annotations

import json
import queue
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import Config, ConfigError
from ..generate import GenerateError
from ..period import resolve, shortcut
from ..sources._http import SourceError
from .session import ReportSession

PAGE = Path(__file__).with_name("page.html")


def serve(
    cfg: Config,
    output_dir: Path,
    since: date,
    until: date,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """起服务并阻塞，Ctrl-C 退出。"""
    session = ReportSession(output_dir, since, until)
    lock = threading.Lock()
    handler = _make_handler(cfg, session, lock)

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"EOM 周报已启动：{url}\n按 Ctrl-C 退出。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()


def _make_handler(cfg: Config, session: ReportSession, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # 静默访问日志
            pass

        def do_GET(self):
            if not self._local_host():
                return self._json(403, {"error": "仅允许本机访问"})
            path, query = _split(self.path)
            if path == "/":
                return self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
            if path == "/api/state":
                with lock:
                    return self._json(200, _state(cfg, session))
            if path == "/api/export":
                return self._export(query)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_host() or self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
                self.close_connection = True
                return self._json(403, {"error": "仅允许本机同源请求"})
            if self.headers.get_content_type() != "application/json":
                self.close_connection = True
                return self._json(415, {"error": "请求必须使用 application/json"})
            try:
                body = self._read_json()
            except ValueError as exc:
                self.close_connection = True
                return self._json(400, {"error": str(exc)})
            path, _ = _split(self.path)
            if path == "/api/period":
                return self._period(body)
            if path == "/api/sync":
                return self._stream(lambda: _sync_events(cfg, session, body))
            if path == "/api/progress":
                return self._progress(body)
            if path == "/api/submit":
                return self._submit(body)
            self._json(404, {"error": "not found"})

        def _local_host(self) -> bool:
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}",
            }

        def _progress(self, body: dict):
            try:
                day = body.get("day")
                order_id = body.get("orderId")
                if not isinstance(day, str) or len(day) != 10 or date.fromisoformat(day).isoformat() != day:
                    raise ValueError("日期格式必须是 YYYY-MM-DD")
                if not isinstance(order_id, str) or not order_id or len(order_id) > 100:
                    raise ValueError("工单 ID 格式不正确")
                with lock:
                    session.update_progress(day, order_id, body.get("rate"), body.get("hours"))
                    state = _state(cfg, session)
            except (ValueError, ConfigError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, state)

        def _submit(self, body: dict):
            try:
                retry = body.get("retryUnknown", False)
                if not isinstance(retry, bool):
                    raise ValueError("retryUnknown 必须是布尔值")
                with lock:
                    if "since" in body or "until" in body:
                        session.set_period(*_period_from(body))
                    result = session.submit(cfg, retry_unknown=retry)
            except (ValueError, ConfigError, SourceError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, result)

        def _period(self, body: dict):
            try:
                with lock:
                    session.set_period(*_period_from(body))
                    state = _state(cfg, session)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, state)

        def _export(self, query: dict):
            with lock:
                text = session.text()
                name = f"{session.since}_{session.until}.md"
            headers = {}
            if query.get("download"):
                headers["Content-Disposition"] = f'attachment; filename="{name}"'
            self._send(200, "text/plain; charset=utf-8", text.encode("utf-8"), headers)

        def _stream(self, produce: Callable[[], Iterator[dict]]):
            # SSE 没有 Content-Length，必须在发头之前就决定关连接，
            # 否则 keep-alive 下客户端会一直等下一个响应
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            with lock:
                for event in produce():
                    frame = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("请求内容过大或格式不正确")
                body = json.loads(self.rfile.read(length) or b"{}", parse_constant=_bad_constant)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("请求内容不是有效 JSON") from exc
            if not isinstance(body, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            return body

        def _json(self, status: int, payload: Any):
            self._send(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            )

        def _send(self, status: int, content_type: str, body: bytes, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _state(cfg: Config, session: ReportSession) -> dict:
    return {
        **session.state(), "gaps": cfg.missing(),
        "submitGaps": cfg.missing(need_ai=False, need_gitlab=False),
    }


def _period_from(body: dict) -> tuple[date, date]:
    for field in ("since", "until", "shortcut"):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > 32):
            raise ValueError("日期参数格式不正确")
    if body.get("shortcut"):
        return shortcut(body["shortcut"])
    return resolve(since=body.get("since"), until=body.get("until"))


_DONE = object()


def _sync_events(cfg: Config, session: ReportSession, body: dict) -> Iterator[dict]:
    """采集在工作线程里跑，进度经队列实时推给页面。"""
    try:
        if body.get("since") or body.get("until"):
            session.set_period(*_period_from(body))
        cfg.require()
    except (ValueError, ConfigError) as exc:
        yield {"type": "error", "message": str(exc)}
        return

    channel: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            summary = session.sync(cfg, channel.put)
            channel.put(("summary", summary))
        except (SourceError, GenerateError, ValueError) as exc:
            channel.put(("error", str(exc)))
        finally:
            channel.put(_DONE)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()

    while True:
        item = channel.get()
        if item is _DONE:
            break
        if isinstance(item, tuple):
            kind, text = item
            yield {"type": kind, "text": text} if kind == "summary" else {
                "type": "error",
                "message": text,
            }
        else:
            yield {"type": "progress", "text": item}

    worker.join()
    yield {"type": "done"}


def _split(raw: str) -> tuple[str, dict]:
    path, _, query = raw.partition("?")
    parsed = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key:
            parsed[key] = value
    return path, parsed


def _bad_constant(_value: str):
    raise ValueError("请求不允许 NaN 或无穷大")
