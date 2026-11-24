"""Serve the separately installed Rocky TTS engine over a local Unix socket."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import signal
import socketserver
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import ModuleType
from typing import Any

_logger = logging.getLogger(__name__)
MAX_BODY = 4096
MAX_TEXT = 600
RATES = (1.0, 1.25, 1.5)


def load_rocky(repo: Path) -> ModuleType:
    script = repo / "rocky_say.py"
    if not script.is_file():
        raise ValueError(f"Rocky TTS script missing: {script}")
    if not (repo / "assets" / "rocky_training_audio_scrubbed.wav").is_file():
        raise ValueError("Rocky TTS reference audio is missing")
    spec = importlib.util.spec_from_file_location("rocky_say", script)
    if spec is None or spec.loader is None:
        raise ValueError("Rocky TTS script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RockyHandler(BaseHTTPRequestHandler):
    server: RockyServer

    def log_message(self, format: str, *args: Any) -> None:
        _logger.info(format, *args)

    def _reply(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._reply(404, b"", "text/plain")
            return
        self._reply(200, b'{"voice":"rocky"}', "application/json")

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self._reply(404, b"", "text/plain")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Invalid request")
            text = body.get("text")
            rate = body.get("rate")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise ValueError("Invalid text")
            if type(rate) not in (float, int) or rate not in RATES:
                raise ValueError("Invalid rate")
        except (ValueError, UnicodeError):
            self._reply(400, b"Invalid speech request", "text/plain")
            return
        try:
            with self.server.synthesis_lock:
                transformed = self.server.rocky.rocky_transform(text.strip())
                with tempfile.NamedTemporaryFile(suffix=".wav") as target:
                    self.server.model.tts_to_file(
                        text=transformed,
                        speaker_wav=str(self.server.reference),
                        language="en",
                        file_path=target.name,
                    )
                    wav = target.read()
                # The menu rate is relative to Rocky's own base speed (1.5), so Fastest is 2.25.
                wav = self.server.rocky.apply_speed(wav, 1.5 * rate)
        except Exception as exc:
            _logger.exception("Rocky synthesis failed: %s", type(exc).__name__)
            self._reply(503, b"Rocky synthesis failed", "text/plain")
            return
        self._reply(200, wav, "audio/wav")


class RockyServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, rocky: ModuleType, model: Any, reference: Path):
        self.rocky = rocky
        self.model = model
        self.reference = reference
        self.synthesis_lock = threading.Lock()
        super().__init__(str(socket_path), RockyHandler)


def prepare_socket_dir(socket_path: Path) -> None:
    if not socket_path.is_absolute():
        raise ValueError("Socket path must be absolute")
    socket_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = socket_path.parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or directory.st_mode & 0o077
    ):
        raise ValueError(
            f"Socket directory must be private and owned by this user: {socket_path.parent}"
        )


def serve(repo: Path, socket_path: Path) -> None:
    os.umask(0o077)
    prepare_socket_dir(socket_path)
    rocky = load_rocky(repo)
    from TTS.api import TTS

    model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    if socket_path.exists() or socket_path.is_symlink():
        mode = socket_path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise ValueError(f"Refusing to replace non-socket path: {socket_path}")
        socket_path.unlink()
    with RockyServer(
        socket_path, rocky, model, repo / "assets" / "rocky_training_audio_scrubbed.wav"
    ) as server:
        os.chmod(socket_path, 0o600)
        signal.signal(
            signal.SIGTERM,
            lambda _signum, _frame: threading.Thread(target=server.shutdown, daemon=True).start(),
        )
        try:
            server.serve_forever()
        finally:
            socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Rocky voice bridge for OmniGent")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--socket", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    serve(args.repo.resolve(), args.socket)


if __name__ == "__main__":
    main()
