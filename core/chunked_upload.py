"""Chunked-upload staging for large or unreliable uploads.

Chunked uploads happen in three steps:

1. ``init``   → reserve an upload session, get an ``upload_id``
2. ``chunk``  → upload one chunk (by index) onto disk
3. ``complete`` → assemble all chunks into one file → artifact

Chunks always land on disk (never in RAM), so very large uploads stay within
memory limits. Sessions that are never completed are pruned by
:func:`ChunkedUploadManager.prune_stale`.
"""

from __future__ import annotations

import base64
import binascii
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from relay_server.config import settings


def _now_ts() -> float:
    return time.time()


class ChunkedUploadError(Exception):
    """Raised when a chunked-upload operation cannot be completed."""


class ChunkedUploadManager:
    """Track in-progress chunked uploads and stage their chunks on disk.

    Session metadata is held in memory (``self._sessions``); the chunk bytes
    themselves are written to ``settings.chunked_uploads_dir / upload_id``.
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        # Resolve lazily in init_upload so test-time overrides of
        # settings.chunked_uploads_dir are respected after import.
        self._base_dir = base_dir
        self._sessions: Dict[str, Dict[str, Any]] = {}

    # -- internals -----------------------------------------------------

    def _resolve_base_dir(self) -> Path:
        base = self._base_dir or settings.chunked_uploads_dir
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _session_dir(self, upload_id: str) -> Path:
        return self._resolve_base_dir() / upload_id

    @staticmethod
    def _decode_chunk(data: bytes | str) -> bytes:
        """Accept raw bytes or a base64-encoded string and return bytes."""
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        if isinstance(data, str):
            try:
                return base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ChunkedUploadError("chunk data is not valid base64") from exc
        raise ChunkedUploadError(f"unsupported chunk data type: {type(data).__name__}")

    # -- public API ----------------------------------------------------

    def init_upload(
        self,
        name: str,
        mime_type: Optional[str],
        total_chunks: int,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not name:
            raise ChunkedUploadError("name is required")
        if total_chunks <= 0:
            raise ChunkedUploadError("total_chunks must be positive")
        if total_chunks > 10000:
            raise ChunkedUploadError("total_chunks exceeds maximum of 10000")

        upload_id = f"upl_{secrets.token_urlsafe(12)}"
        session_dir = self._session_dir(upload_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._sessions[upload_id] = {
            "name": name,
            "mime_type": mime_type,
            "total_chunks": total_chunks,
            "received": set(),
            "session_dir": session_dir,
            "created_at": _now_ts(),
            "created_by": created_by,
        }
        return {"upload_id": upload_id, "status": "init"}

    def store_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        data: bytes | str,
    ) -> Dict[str, Any]:
        session = self._sessions.get(upload_id)
        if session is None:
            raise ChunkedUploadError("Upload session not found", upload_id)
        if not isinstance(chunk_index, int):
            raise ChunkedUploadError("chunk_index must be an integer")
        if chunk_index < 0 or chunk_index >= session["total_chunks"]:
            raise ChunkedUploadError(
                f"chunk_index {chunk_index} out of range "
                f"(0..{session['total_chunks'] - 1})"
            )

        payload = self._decode_chunk(data)
        chunk_path = session["session_dir"] / f"chunk_{chunk_index:04d}"
        chunk_path.write_bytes(payload)
        session["received"].add(chunk_index)
        return {
            "upload_id": upload_id,
            "chunk_index": chunk_index,
            "received": len(session["received"]),
            "status": "received",
        }

    def complete_upload(
        self,
        upload_id: str,
        checksum: Optional[str] = None,
    ) -> Path:
        """Concatenate all chunks in order and return the assembled file path.

        Optionally verifies a client-supplied SHA256 hex digest of the full
        file; raises :class:`ChunkedUploadError` on mismatch.
        """
        import hashlib

        session = self._sessions.get(upload_id)
        if session is None:
            raise ChunkedUploadError("Upload session not found", upload_id)

        received: Set[int] = session["received"]
        total = session["total_chunks"]
        if len(received) != total:
            missing = sorted(set(range(total)) - received)
            raise ChunkedUploadError(
                f"Missing chunks: have {len(received)}, need {total} "
                f"(missing: {missing})"
            )

        output_path = session["session_dir"] / "complete"
        h = hashlib.sha256()
        with output_path.open("wb") as dst:
            for i in range(total):
                chunk_path = session["session_dir"] / f"chunk_{i:04d}"
                dst.write(chunk_path.read_bytes())
                # Update hash chunkwise without re-reading the assembled file.
                h.update(_file_hash_chunk(chunk_path))
        if checksum is not None:
            actual = h.hexdigest()
            if actual.lower() != checksum.lower():
                raise ChunkedUploadError(
                    f"Checksum mismatch: computed {actual}, expected {checksum}"
                )
        return output_path

    def get_session(self, upload_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(upload_id)

    def discard_session(self, upload_id: str) -> None:
        """Remove a session and delete its staged chunks from disk."""
        session = self._sessions.pop(upload_id, None)
        if session is None:
            return
        session_dir: Path = session["session_dir"]
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    def prune_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Drop sessions older than ``max_age_seconds`` (default 1h).

        Returns the number of pruned sessions.
        """
        cutoff = _now_ts() - max_age_seconds
        stale = [uid for uid, s in self._sessions.items() if s["created_at"] < cutoff]
        for uid in stale:
            self.discard_session(uid)
        return len(stale)


def _file_hash_chunk(path: Path) -> bytes:
    """Return the chunk bytes for incremental hashing (small chunks only)."""
    return path.read_bytes()


# Module-level singleton used by the storage router.
chunked_manager = ChunkedUploadManager()