"""Immutable local Markdown snapshots for review and annual summaries."""

import hashlib
import os
from pathlib import Path
import tempfile

from .common import ReportError


DEFAULT_ARCHIVE_FOLDER = "reports"


def archive_report(profile, report):
    from .workflow import export_markdown

    markdown = export_markdown(report)
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
    bounds = report["range"]
    root = profile.archive_dir or profile.workspace / DEFAULT_ARCHIVE_FOLDER
    folder = Path(root) / bounds["since"][:4] / report["scope"]
    path = folder / (report["mode"] + "_" + bounds["since"] + "_" + bounds["until"] + "_" + digest + ".md")
    temporary = None
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if path.read_text(encoding="utf-8") != markdown:
                raise ReportError("Review archive differs from its content digest; preserve and inspect the file.")
            return str(path)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(markdown)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != markdown:
                raise ReportError("Review archive changed concurrently; inspect it before submitting.")
        return str(path)
    except (OSError, UnicodeError) as exc:
        raise ReportError("Cannot save the local review archive; no EOM write should proceed.") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
