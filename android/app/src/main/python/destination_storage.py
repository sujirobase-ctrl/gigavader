"""SAF delivery with per-destination receipts and recoverable local staging."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from src.config import SkipReason, TaskResult
from src.downloaders import ThreadDownloader
from src.downloaders.media_downloader import DownloadOutcome


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            pending = Path(output.name)
            json.dump(data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


class Destination:
    def __init__(self, private_dir, tree, backend):
        self.tree, self.backend = tree, backend
        self.lock = threading.RLock()
        self.root = Path(private_dir) / "destinations" / hashlib.sha256(tree.encode()).hexdigest()
        self.staging = self.root / "staging"
        self.index_path = self.root / "receipts.json"
        self.records = json.loads(self.index_path.read_text(encoding="utf-8")) if self.index_path.exists() else {}
        if not isinstance(self.records, dict):
            raise ValueError("Índice da pasta de destino inválido; os arquivos existentes não foram alterados.")

    def key(self, root, media):
        return root.name + "\0" + media.state_key

    def remember(self, key, record):
        with self.lock:
            updated = {**self.records, key: record}
            atomic_json(self.index_path, updated)
            self.records = updated

    def local_file(self, record):
        relative = record.get("staged", "") if isinstance(record, dict) else ""
        path = (self.staging / relative).resolve()
        if relative and path.is_relative_to(self.staging.resolve()) and path.is_file():
            if path.stat().st_size == record.get("size", 0) > 0:
                return path
        return None

    def complete(self, record):
        return (isinstance(record, dict) and record.get("status") == "completed"
                and bool(self.backend.exists(self.tree, record.get("uri", ""), record.get("size", 0))))

    def files(self):
        self.backend.describe(self.tree)  # Never quietly fall back after revoked access.
        unique = {record["uri"]: record for record in self.records.values() if self.complete(record)}
        return list(unique.values())


class DestinationDownloader(ThreadDownloader):
    def __init__(self, client, manager, options, destination):
        super().__init__(client, manager, options)
        self.destination = destination
        self.committed = {}

    def download_prepared(self, prepared, selected_keys=None):
        destination = self.destination
        destination.backend.describe(destination.tree)
        root = destination.staging / prepared.root.name
        plan = [(media, root / folder.relative_to(prepared.root)) for media, folder in prepared.plan]
        # Receipts are saved for each file, including before the engine's final
        # state save. A process restart must not forget already-delivered media.
        state = {media.state_key: destination.records.get(destination.key(root, media), {}) for media, _ in plan}
        atomic_json(root / ".fpd_state.json", state)
        return super().download_prepared(replace(prepared, root=root, plan=plan), selected_keys)

    def _cached_file_is_complete(self, cached, root):
        return self.destination.complete(cached)

    def _reuse_outcome(self, media, folder, root):
        record = self.destination.records.get(self.destination.key(root, media), {})
        path = self.destination.local_file(record)
        return DownloadOutcome(TaskResult.COMPLETED, path=path) if path else None

    def _finalize_outcome(self, outcome, media, root):
        # Keep each worker occupied until delivery, bounding staging to active
        # workers instead of downloading the whole thread while SAF is copying.
        with self.destination.lock:
            return self._store_outcome(outcome, media, root)

    def _store_outcome(self, outcome, media, root):
        if not outcome.path or outcome.result not in (TaskResult.COMPLETED, TaskResult.SKIPPED):
            return outcome
        if outcome.reason not in (None, SkipReason.ALREADY_DOWNLOADED):
            return outcome
        destination = self.destination
        path = outcome.path.resolve()
        relative = path.relative_to(destination.staging.resolve()).as_posix()
        key = destination.key(root, media)
        previous = self.committed.get(path)
        if previous and destination.complete(previous):
            destination.remember(key, previous)
            return replace(outcome, result=TaskResult.SKIPPED, reason=SkipReason.ALREADY_DOWNLOADED)
        staged = {"status": "staged", "staged": relative, "size": path.stat().st_size}
        destination.remember(key, staged)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        receipt = json.loads(str(destination.backend.publish(destination.tree, str(path), relative, mime)))
        record = {**receipt, "status": "completed", "mime": mime}
        if record.get("size") != staged["size"] or not destination.complete(record):
            destination.backend.discard(destination.tree, record.get("uri", ""))
            raise OSError("A pasta de destino não confirmou a gravação completa. O temporário foi mantido.")
        try:
            destination.remember(key, record)
        except OSError:
            destination.backend.discard(destination.tree, record["uri"])
            raise
        destination.backend.commit(record["uri"])
        self.committed[path] = record
        return replace(outcome, result=TaskResult.COMPLETED, reason=None)

    def _completed_state(self, path, root):
        record = self.committed[path.resolve()]
        # Delivery and its durable receipt both succeeded. Only our staging
        # file is removed; files in previous destinations are never touched.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self.live_manager.update_log(event="Temporary retained", details="Arquivo entregue; temporário local não pôde ser removido.")
        return record
