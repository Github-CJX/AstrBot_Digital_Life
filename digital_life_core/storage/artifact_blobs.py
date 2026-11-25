"""File-backed artifact blobs in sidecar SQLite stores.

Several sidecar databases keep large binary payloads inside their rows until a
compaction pass moves them to content-addressed files and leaves a marker in
the row. The main runtime store, the semantic store and the native-image store
all do this, and the maintenance code that performs it lives in ``storage`` —
so the marker bytes and the offload routine belong here, not behind whichever
feature happens to own the sidecar.

The marker bytes are part of the on-disk format: rows written before this
module existed already carry ``\\x00PCNIMG1/`` plus the relative path, so the
prefix stays frozen. Renaming it would orphan every already-offloaded artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from digital_life_core.storage.image_payloads import image_extension
from digital_life_core.storage.sqlite_tuning import apply_sidecar_pragmas, tuning_profile

FILE_MARKER = b"\x00PCNIMG1/"
MARKER_REL_PATTERN = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.(?:png|jpg|webp|gif|bin)$")
BUSY_TIMEOUT_MS = 5000


def is_file_marker(value: Any) -> bool:
    """True when a row value is already an offloaded-file marker."""
    return isinstance(value, (bytes, bytearray, memoryview)) and bytes(value).startswith(FILE_MARKER)


def marker_path(value: bytes) -> str:
    """Relative path carried by a marker, or ``""`` when it is not one."""
    raw = bytes(value)
    if not raw.startswith(FILE_MARKER):
        return ""
    try:
        relative = raw[len(FILE_MARKER):].decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return ""
    return relative if MARKER_REL_PATTERN.match(relative) else ""


def marker_for(relative: str) -> bytes:
    return FILE_MARKER + relative.encode("ascii")


def write_blob(media_root: Path, content: bytes, *, media_type: str = "") -> bytes:
    digest_hex = hashlib.sha256(content).hexdigest()
    extension = image_extension(content, media_type)
    relative = f"{digest_hex[:2]}/{digest_hex}{extension}"
    target_dir = media_root / digest_hex[:2]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest_hex}{extension}"
    if not target.is_file() or target.stat().st_size != len(content):
        fd, tmp_name = tempfile.mkstemp(prefix=".blob-", suffix=".tmp", dir=target_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    return marker_for(relative)


def _media_type(metadata: Any) -> str:
    try:
        return str(json.loads(metadata).get("media_type") or "")
    except (TypeError, ValueError):
        return ""


def offload_artifact_blobs(
    db_path: Path | str,
    *,
    media_root: Path | str,
    batch_size: int = 64,
    table: str = "artifacts",
) -> dict[str, int]:
    """Move inline ``data`` blobs of one sidecar table onto the filesystem.

    Safe to run against a live database: the store is only read in batches and
    the rewrite happens in short ``BEGIN IMMEDIATE`` transactions, and rows that
    already hold a marker are left alone.
    """
    path = Path(db_path)
    empty = {"migrated": 0, "already_file_backed": 0, "bytes_offloaded": 0}
    if not path.is_file():
        return dict(empty)
    profile = tuning_profile()
    media = Path(media_root)
    chunk = max(8, int(batch_size))
    migrated = already_file_backed = bytes_offloaded = 0
    last_rowid = 0
    while True:
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            apply_sidecar_pragmas(
                connection,
                profile=profile,
                enable_wal=False,
                read_only=True,
                busy_timeout_ms=BUSY_TIMEOUT_MS,
                foreign_keys=True,
            )
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if present is None:
                return dict(empty)
            rows = connection.execute(
                f"SELECT rowid, id, metadata, data FROM {table} WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                (last_rowid, chunk),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            break
        updates: list[tuple[bytes, str]] = []
        for row in rows:
            last_rowid = int(row["rowid"])
            raw = bytes(row["data"])
            if is_file_marker(raw):
                already_file_backed += 1
                continue
            marker = write_blob(media, raw, media_type=_media_type(row["metadata"]))
            updates.append((marker, row["id"]))
            migrated += 1
            bytes_offloaded += max(0, len(raw) - len(marker))
        if not updates:
            continue
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
        try:
            apply_sidecar_pragmas(
                connection,
                profile=profile,
                enable_wal=False,
                read_only=False,
                busy_timeout_ms=BUSY_TIMEOUT_MS,
                foreign_keys=True,
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(f"UPDATE {table} SET data = ? WHERE id = ?", updates)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
    return {
        "migrated": migrated,
        "already_file_backed": already_file_backed,
        "bytes_offloaded": bytes_offloaded,
    }


def count_inline_blobs(db_path: Path | str, *, table: str = "artifacts", column: str = "data") -> int:
    """Rows of ``table`` whose ``column`` still holds inline bytes."""
    path = Path(db_path)
    if not path.is_file():
        return 0
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    try:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if present is None:
            return 0
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE length({column}) > 0 "
            f"AND substr({column}, 1, {len(FILE_MARKER)}) <> ?",
            (FILE_MARKER,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()
