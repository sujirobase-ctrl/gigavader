"""Private, atomic recovery snapshots for interrupted GUI sessions."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, fields
from pathlib import Path

from src.downloaders.thread_downloader import PreparedThread, ThreadOptions
from src.resolvers import ResolvedMedia


def recovery_directory():
    override = os.environ.get("BUIDL_RECOVERY_DIR")
    return Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "BUIDL" / "recovery"


class RecoveryStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else recovery_directory()
        self.path = None
        self.document = None
        self.lock = threading.RLock()

    def pending(self):
        entries = []
        for path in self.directory.glob("*.json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                if document.get("version") == 1 and document.get("status") == "pending":
                    document["_path"] = str(path)
                    entries.append(document)
            except (OSError, ValueError, AttributeError):
                continue
        return sorted(entries, key=lambda item: item.get("updated", 0), reverse=True)

    def _write(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.document["updated"] = time.time()
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.document, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def begin(self, url, cookies_file, options, preview, resume=None):
        with self.lock:
            if resume:
                self.path = Path(resume["_path"])
                self.document = {k: v for k, v in resume.items() if k != "_path"}
            else:
                self.path = self.directory / (uuid.uuid4().hex + ".json")
                saved_options = {field.name: getattr(options, field.name) for field in fields(options)
                                 if field.name not in {"cancel_event", "pause_event"}}
                self.document = {"version": 1, "status": "pending", "phase": "scanning",
                                 "url": url, "cookies_file": cookies_file, "options": saved_options,
                                 "preview": preview, "selection": None}
            self._write()

    def save_prepared(self, prepared, preview):
        with self.lock:
            if self.document is None:
                return
            plan = []
            root = prepared.root.resolve()
            for media, folder in prepared.plan:
                data = asdict(media)
                # Cookies live only in cookies.txt / the in-memory HTTP jar.
                data["headers"] = {k: v for k, v in (media.headers or {}).items()
                                   if k.lower() not in {"cookie", "authorization", "proxy-authorization"}}
                if media.resolver and media.source_url:
                    data["url"] = media.source_url
                plan.append({"media": data, "folder": str(folder.resolve().relative_to(root))})
            self.document["prepared"] = {"title": prepared.title, "root": str(root),
                                         "post_count": prepared.post_count, "plan": plan}
            self.document["phase"] = "selection" if preview else "downloading"
            self._write()

    def select(self, keys):
        with self.lock:
            if self.document is not None:
                self.document["selection"] = sorted(keys) if keys is not None else None
                self.document["phase"] = "downloading"
                self._write()

    def complete(self):
        with self.lock:
            if self.path:
                self.path.unlink(missing_ok=True)
            self.path = self.document = None

    @staticmethod
    def restore_prepared(document):
        data = document.get("prepared")
        if not data:
            return None
        root = Path(data["root"]).resolve()
        allowed = {field.name for field in fields(ResolvedMedia)}
        plan = []
        for entry in data["plan"]:
            folder = (root / entry["folder"]).resolve()
            folder.relative_to(root)
            media = ResolvedMedia(**{k: v for k, v in entry["media"].items() if k in allowed})
            plan.append((media, folder))
        return PreparedThread(data["title"], root, plan, data["post_count"])
