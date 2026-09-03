"""本机网页服务。

日常入口就是这个页面：选区间、拉数、预览、导出、对话都在上面完成，命令行不用
记。只绑 127.0.0.1，凭证留在服务端，Gitee / GitLab / AI 的请求全由这里发出。

用 ThreadingHTTPServer 而不是 HTTPServer——采集与对话都是 SSE 长连接，单线程
会把后续请求全堵住。
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

from ..config import ALL_SOURCES, Config, ConfigError
from ..period import resolve, shortcut
from ..sources._http import SourceError
from .llm import Chat
from .tools import ReportSession, ToolError

PAGE = Path(__file__).with_name("page.html")


def serve(
    cfg: Config,
    output_dir: Path,
    config_dir: Path,
    since: date,
    until: date,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """起服务并阻塞，Ctrl-C 退出。"""
    session = ReportSession(output_dir, config_dir, since, until)
    lock = threading.Lock()
    handler = _make_handler(cfg, session, lock)

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"日报工作台已启动：{url}\n按 Ctrl-C 退出。")
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
            path, query = _split(self.path)
            if path == "/":
                return self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
            if path == "/api/state":
                return self._json(200, _state(cfg, session))
            if path == "/api/export":
                return self._export(query)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            path, _ = _split(self.path)
            body = self._read_json()
            if path == "/api/period":
                return self._period(body)
            if path == "/api/sync":
                return self._stream(lambda: _sync_events(cfg, session, body))
            if path == "/api/chat":
                return self._stream(lambda: _chat_events(cfg, session, body))
            self._json(404, {"error": "not found"})

        def _period(self, body: dict):
            try:
                with lock:
                    session.set_period(*_period_from(body))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, _state(cfg, session))

        def _export(self, query: dict):
            with lock:
                text = session.memo() if query.get("style") == "memo" else session.markdown()
            headers = {}
            if query.get("download"):
                name = f"{session.since}_{session.until}.md"
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
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

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
    report = session.read_report()
    return {
        "range": report["range"],
        "days": report["days"],
        "stats": session.stats(),
        "gaps": cfg.missing(ALL_SOURCES),
        "ai": cfg.ai.ready,
    }


def _period_from(body: dict) -> tuple[date, date]:
    if body.get("shortcut"):
        return shortcut(body["shortcut"])
    return resolve(since=body.get("since"), until=body.get("until"))


_DONE = object()


def _sync_events(cfg: Config, session: ReportSession, body: dict) -> Iterator[dict]:
    """采集在工作线程里跑，进度经队列实时推给页面。"""
    try:
        if body.get("since") or body.get("until"):
            session.set_period(*_period_from(body))
        cfg.require(("gitee",))  # Gitee 提供任务容器，缺了没得可归
    except (ValueError, ConfigError) as exc:
        yield {"type": "error", "message": str(exc)}
        return

    channel: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            summary = session.sync(cfg, ALL_SOURCES, channel.put)
            channel.put(("summary", summary))
        except (SourceError, ValueError) as exc:
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
    yield {"type": "done", "stats": session.stats()}


def _chat_events(cfg: Config, session: ReportSession, body: dict) -> Iterator[dict]:
    message = (body.get("message") or "").strip()
    if not message:
        yield {"type": "error", "message": "说点什么吧"}
        return
    try:
        chat = Chat(session, cfg)
        for event in chat.run([{"role": "user", "content": message}]):
            yield {"type": event.type, **event.data}
    except (ToolError, SourceError) as exc:
        yield {"type": "error", "message": str(exc)}
        yield {"type": "done", "stats": session.stats()}


def _split(raw: str) -> tuple[str, dict]:
    path, _, query = raw.partition("?")
    parsed = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key:
            parsed[key] = value
    return path, parsed
