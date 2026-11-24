"""The pictures made for a Social post: diagrams, generated pictures, screenshots (2026-11-15).

Why: an expert post has no picture and a tool post only the generic site card. The weekly
drafter on the box now makes 4 or 5 candidates per post (branded diagrams, generated pictures,
and a screenshot of the tool's own page when one is clean), ranks them and uploads them through
the relay (``POST /social/visuals``). The card shows them in rank order with the first
preselected, and Reid can pick another or none (``POST /social/visual-choice``).

The pictures are files, not part of the Social state: ``<dir>/<slot>/<sha>.jpg`` and
``<sha>.prev.jpg`` (a small preview for the picker's tiles), next to ``ae-social.json``
(``AE_SOCIAL_VISUALS_DIR`` moves them). The state keeps only the list of candidates, their
order, and the choice. A store with no directory keeps the bytes in memory (tests, and an
install with no data volume). Files of posts that left the state are swept at each upload.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ae_omni_policies import social_visual

VISUALS_DIR_ENV = "AE_SOCIAL_VISUALS_DIR"
DIR_NAME = "social-visuals"
MAX_FULL_BYTES = 600 * 1024
"""One 1200 by 627 JPEG; the drafter writes about 150 to 300 KB."""
MAX_PREVIEW_BYTES = 60 * 1024
"""One picker tile, about 360 px wide."""
MAX_CANDIDATES = 5
SWEEP_GRACE_S = 600
_SHA = re.compile(r"^[0-9a-f]{16}$")
_OWN_FILE = re.compile(r"^([0-9a-f]{16})(?:\.prev)?\.jpg$")
"""The only file names the sweep ever deletes: a shared folder's other files are never touched."""
READ_CACHE = 12
"""Pictures kept encoded: the card asks for a 600 KB picture in about 34 parts."""
_SLOT = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
JPEG_MAGIC = b"\xff\xd8\xff"


class MadeError(Exception):
    """A picture that cannot be stored or served; ``status`` is the answer's HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def decode_jpeg(b64: str, *, limit: int, what: str) -> bytes:
    """The JPEG bytes in *b64*, checked for type and size.

    :raises MadeError: 400 when it is not base64, not a JPEG, or larger than *limit*.
    """
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MadeError(400, f"{what} is not valid base64") from exc
    if len(data) > limit:
        raise MadeError(400, f"{what} is {len(data)} bytes; the limit is {limit}")
    if not data.startswith(JPEG_MAGIC):
        raise MadeError(400, f"{what} is not a JPEG; the drafter writes JPEG files")
    return data


def sha_of(data: bytes) -> str:
    """The picture's identity: the first 16 hex characters of its SHA-256."""
    return hashlib.sha256(data).hexdigest()[:16]


class MadeStore:
    """Made pictures on disk (or in memory when *root* is None), keyed by slot and sha."""

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._memory: dict[tuple[str, str], tuple[bytes, float]] = {}
        self._cache: OrderedDict[tuple[str, str], social_visual.Picture] = OrderedDict()
        self._lock = threading.Lock()

    def _path(self, slot_id: str, name: str) -> Path:
        assert self._root is not None
        if not _SLOT.match(slot_id):
            raise MadeError(400, f"{slot_id!r} is not a slot id")
        return self._root / slot_id / name

    def put(self, slot_id: str, full: bytes, preview: bytes) -> str:
        """Store one candidate's two files; its sha (the same bytes give the same sha)."""
        sha = sha_of(full)
        with self._lock:
            if self._root is None:
                stamp = time.time()
                self._memory[(slot_id, f"{sha}.jpg")] = (full, stamp)
                self._memory[(slot_id, f"{sha}.prev.jpg")] = (preview, stamp)
                return sha
            for name, data in ((f"{sha}.jpg", full), (f"{sha}.prev.jpg", preview)):
                path = self._path(slot_id, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".made-", dir=str(path.parent))
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    os.replace(tmp, path)
                except OSError as exc:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                    raise MadeError(500, f"could not save the picture: {exc}") from exc
        return sha

    def read(self, slot_id: str, sha: str, *, preview: bool = False) -> social_visual.Picture:
        """One stored picture, in the shape the visual route chunks.

        :raises MadeError: 404 when it is not stored (swept, or never uploaded).
        """
        if not _SHA.match(sha):
            raise MadeError(400, f"{sha!r} is not a picture id")
        name = f"{sha}.prev.jpg" if preview else f"{sha}.jpg"
        with self._lock:
            if (hit := self._cache.get((slot_id, name))) is not None:
                self._cache.move_to_end((slot_id, name))
                return hit
            if self._root is None:
                entry = self._memory.get((slot_id, name))
                data = entry[0] if entry is not None else None
            else:
                try:
                    data = self._path(slot_id, name).read_bytes()
                except FileNotFoundError:
                    data = None
                except OSError as exc:
                    raise MadeError(500, f"could not read the picture: {exc}") from exc
            if data is None:
                raise MadeError(404, "that picture is gone; the drafter makes new ones next run")
            picture = social_visual.Picture(
                "image/jpeg", len(data), base64.b64encode(data).decode("ascii")
            )
            self._cache[(slot_id, name)] = picture
            while len(self._cache) > READ_CACHE:
                self._cache.popitem(last=False)
            return picture

    def keep_only(
        self, slot_ids: set[str], shas: dict[str, set[str]], *, now: float | None = None
    ) -> None:
        """Delete every slot folder not in *slot_ids*, and files whose sha is not kept.

        Files written in the last :data:`SWEEP_GRACE_S` stay: another upload may have saved
        them and not yet recorded them in the state."""
        clock = time.time() if now is None else now

        def stale(slot_id: str, name: str, written: float) -> bool:
            if clock - written < SWEEP_GRACE_S:
                return False
            kept = shas.get(slot_id, set()) if slot_id in slot_ids else set()
            return name.split(".")[0] not in kept

        with self._lock:
            self._cache.clear()
            if self._root is None:
                for key in [k for k, (_, at) in self._memory.items() if stale(k[0], k[1], at)]:
                    del self._memory[key]
                return
            if not self._root.is_dir():
                return
            for folder in self._root.iterdir():
                if not folder.is_dir() or folder.is_symlink() or not _SLOT.match(folder.name):
                    continue
                for file in folder.iterdir():
                    if not _OWN_FILE.match(file.name) or not file.is_file() or file.is_symlink():
                        continue  # never ours: left alone
                    try:
                        written = file.stat().st_mtime
                    except OSError:
                        continue
                    if stale(folder.name, file.name, written):
                        with contextlib.suppress(OSError):
                            file.unlink()
                with contextlib.suppress(OSError):
                    folder.rmdir()  # only when empty


def default_root(state_file: Path | None) -> Path | None:
    """``AE_SOCIAL_VISUALS_DIR``, else ``social-visuals`` next to the state file, else memory."""
    raw = (os.environ.get(VISUALS_DIR_ENV) or "").strip()
    if raw:
        return Path(raw)
    if state_file is None:
        return None
    return state_file.parent / DIR_NAME


_store: MadeStore | None = None
_store_lock = threading.Lock()


def store(state_file: Path | None) -> MadeStore:
    """The process-wide store, rooted beside *state_file* on first use."""
    global _store
    with _store_lock:
        if _store is None:
            _store = MadeStore(default_root(state_file))
        return _store


def reset_store(replacement: MadeStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement
