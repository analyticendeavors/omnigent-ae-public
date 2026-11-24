"""The new-model watch's relay: look inside Claude Code for model ids, ask the new ones, post.

Runs on the host as a systemd user timer (``deploy/systemd/user/ae-omnigent-model-watch.timer``,
every six hours), because the server image has no ``claude`` binary. It posts with the login token
``omnigent login`` stored, like :mod:`limits_relay` (2026-11-10).

One pass:

1. Scan the installed Claude Code binary for ``claude-<family>-<major>[-<minor>]`` ids. The scan
   is skipped when the binary's size, mtime and the scan rules version are the same as last time
   (state in ``~/.omnigent/ae/model-watch.json``).
2. Known ids are the static list and what the server already holds as served. The host's cached
   model catalogs do not count: Claude Code lists a release there the moment it ships, and an id
   that counted as known for that reason would never be asked or reported. A candidate is an id
   newer than the newest known id of its family (a family with none counts).
3. Only the newest candidate per family is asked, at most three a pass; an id that did not answer
   is asked again after 24 hours. The question is one ``claude -p`` call with no tools, MCP
   servers, settings or saved session. An id is served only when the CLI exits 0, reports no
   error and names that id in ``modelUsage`` (whatever the reply says).
4. Post the pass to ``POST /v1/ae/model-watch``, even when nothing was asked, so the Admin Center
   can tell the timer is alive.

``--seed ID`` forces one id through as a candidate even when it is known (to prove the whole path
with ``claude-fable-5-2``). ``--dry-run`` prints the report instead of posting.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import limits_relay, model_watch, newer_models, omni

STATE_ENV = "AE_MODEL_WATCH_RELAY_STATE"
CLAUDE_BIN_ENV = limits_relay.CLAUDE_BIN_ENV
MAX_PROBES = 3
PROBE_TIMEOUT_S = 90.0
REPROBE_AFTER = timedelta(hours=24)
# An id that answered "not served" this many times is left alone until the binary changes.
MAX_TRIES = 10
CHUNK = 8 * 1024 * 1024
PROMPT = "Reply with only the word ok"
# Same pattern as model_watch.ID_RE, unanchored; the lookahead keeps a date suffix
# (claude-opus-4-20250514) from reading as claude-opus-4-20.
SCAN = re.compile(rb"claude-(opus|sonnet|haiku|fable|mythos)-(\d{1,2})(?:-(\d{1,2}))?(?![0-9])")
_OVERLAP = 64
# A match this close to the chunk's end could still grow ("-5" then "-2", or "-2" then "0250514").
_TAIL = 4

Version = model_watch.Version


def state_file(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = (env.get(STATE_ENV) or "").strip()
    return Path(raw) if raw else Path.home() / ".omnigent" / "ae" / "model-watch.json"


def _ids_in(data: bytes, *, final: bool) -> tuple[set[str], int]:
    """Ids in *data*, and where the next chunk's overlap starts.

    A match that ends within a few bytes of the chunk's end could be cut short ("-2" of
    "-20250514", or "-5" of "-5-2"), so it is left for the next chunk unless this is the last.
    """
    found: set[str] = set()
    for match in SCAN.finditer(data):
        if not final and match.end() > len(data) - _TAIL:
            continue
        found.add(match.group(0).decode("ascii"))
    return found, max(len(data) - _OVERLAP, 0)


def scan_file(path: Path, chunk: int = CHUNK) -> set[str]:
    """Every Claude model id in the file at *path*, read in chunks."""
    found: set[str] = set()
    carry = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            data = carry + block
            final = not block
            ids, cut = _ids_in(data, final=final)
            found |= ids
            if final:
                return found
            carry = data[cut:]


def scan_text(text: str) -> set[str]:
    ids, _cut = _ids_in(text.encode("utf-8", "ignore"), final=True)
    return ids


def newest_by_family(ids: Sequence[str]) -> dict[str, Version]:
    newest: dict[str, Version] = {}
    for model_id in ids:
        parsed = model_watch.parse_id(model_id)
        if parsed and parsed[1] > newest.get(parsed[0], (-1, -1)):
            newest[parsed[0]] = parsed[1]
    return newest


def candidates(scanned: set[str], known: set[str]) -> list[str]:
    """The newest scanned id per family that is newer than every known id of that family."""
    top = newest_by_family(sorted(known))
    best: dict[str, tuple[Version, str]] = {}
    for model_id in scanned:
        parsed = model_watch.parse_id(model_id)
        if parsed is None:
            continue
        family, version = parsed
        if version <= top.get(family, (-1, -1)):
            continue
        if family not in best or version > best[family][0]:
            best[family] = (version, model_id)
    return [best[family][1] for family in sorted(best)]


# -- the local state


def read_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    """:raises OSError: When the file cannot be written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".model-watch-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Bumped when the rule for which ids count as known changes: it is saved in the binary stamp, so
# a stamp without it (or with another value) forces one rescan even when the binary is unchanged.
# 2: the host's cached model catalogs stopped counting as known (patch 0351).
SCAN_RULES = 2


def _stamp(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_size, int(info.st_mtime), SCAN_RULES]


def _parse_time(text: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# -- the probe


# What `claude -p --model claude-nonexistent-9-9` prints on the box (2026-11-10, exit 1, is_error,
# api_error_status 404): "There's an issue with the selected model (claude-nonexistent-9-9). It may
# not exist or you may not have access to it. Run --model to pick a different model."
_UNAVAILABLE = re.compile(r"may not exist or you may not have access", re.IGNORECASE)
_OK = re.compile(r"\W*ok\W*", re.IGNORECASE)


def served_reply(raw: str, model_id: str, returncode: int = 0) -> tuple[bool, str, bool]:
    """``(served, detail, error)`` from ``claude -p --output-format json`` output.

    *error* marks an inconclusive probe (a rate limit, an expired login, a network failure, a
    crash): the server keeps the id's earlier verdict and the relay asks again later. Only a
    reply that says the model is unavailable or unknown is a "not served" verdict.
    """
    try:
        reply = json.loads(raw)
    except ValueError:
        return False, "the CLI did not answer with JSON", True
    if not isinstance(reply, dict):
        return False, "the CLI did not answer with a JSON object", True
    usage = reply.get("modelUsage")
    used = [k for k in usage if isinstance(k, str)] if isinstance(usage, dict) else []
    text = str(reply.get("result") or "")
    if reply.get("is_error") or returncode != 0:
        if _UNAVAILABLE.search(text):
            return False, f"the plan refused it: {text[:120]}".strip(), False
        return False, f"the probe failed: {text[:120] or f'claude exited {returncode}'}", True
    # The answer's modelUsage proves which model replied; the words do not matter, so a chatty
    # "Okay!" from a served model is never silenced (fix check on 0341, 2026-11-10).
    if any(k.startswith(model_id) for k in used):
        return True, "answered" if _OK.fullmatch(text.strip()) else "answered (not just ok)", False
    if not used:
        return False, "the reply named no model", True
    return False, "the answer came from another model", False


async def probe(
    claude_bin: str, model_id: str, *, timeout: float = PROBE_TIMEOUT_S
) -> tuple[bool, str, bool]:
    """Ask *model_id* one word. ``(served, detail, error)``; *error* when the CLI never ran it."""
    argv = (
        claude_bin,
        "-p",
        "--model",
        model_id,
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
    )
    with tempfile.TemporaryDirectory(prefix="ae-model-watch-") as cwd:
        try:
            async with limits_relay._child(*argv, cwd=cwd, env=limits_relay._probe_env()) as proc:
                out, _err = await asyncio.wait_for(
                    proc.communicate(PROMPT.encode()), timeout=timeout
                )
        except TimeoutError:
            return False, f"no answer in {timeout:.0f}s", True
        except OSError as exc:
            return False, f"could not start claude: {exc.strerror or type(exc).__name__}", True
    return served_reply(out.decode("utf-8", "replace"), model_id, proc.returncode or 0)


# -- talking to the server


async def get_known(client: httpx.AsyncClient) -> tuple[set[str], list[str]]:
    """``(served ids, ids the server has already asked)``; empty when the server cannot say."""
    try:
        response = await client.get("/v1/ae/model-watch")
        data = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return set(), []
    served = {i for i in data.get("served_ids", []) if isinstance(i, str)}
    asked = [
        m["id"]
        for m in data.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    ]
    return served, asked


async def post(client: httpx.AsyncClient, report: dict[str, Any]) -> dict[str, Any]:
    """``POST /v1/ae/model-watch``.

    :raises limits_relay.RelayError: On a transport failure or a non-2xx answer.
    """
    path = "/v1/ae/model-watch"
    try:
        response = await client.post(path, json=report)
    except httpx.HTTPError as exc:
        raise limits_relay.RelayError(f"POST {path} failed: {exc}") from exc
    if response.status_code == 401:
        raise limits_relay.RelayError(
            f"POST {path} answered 401: the stored login expired; run `omnigent login "
            f"{client.base_url}` as this user"
        )
    if response.status_code >= 300:
        raise limits_relay.RelayError(
            f"POST {path} answered {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def claude_version(claude_bin: str) -> str:
    try:
        done = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout.split() or [""])[0][:40]


async def run(
    *,
    claude_bin: str | None,
    settings: omni.Settings,
    state_path: Path,
    seed: str | None = None,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    prober: Any = None,
    now: datetime | None = None,
    out: Any = None,
) -> int:
    """One pass; the exit status (0 posted or dry, 1 nothing could be done)."""
    out = out or sys.stdout
    now = now or datetime.now(UTC)
    ask = prober or probe
    if claude_bin is None:
        print("no claude on PATH; set AE_LIMITS_CLAUDE_BIN or fix the unit's PATH", file=out)
        return 1
    if seed is not None and model_watch.parse_id(seed) is None:
        print(f"--seed {seed!r} is not a Claude model id such as claude-fable-5-2", file=out)
        return 1
    binary = Path(shutil.which(claude_bin) or claude_bin).resolve()
    state = read_state(state_path)
    stamp = _stamp(binary) if binary.is_file() else None
    probed: dict[str, str] = {
        k: v for k, v in (state.get("probed") or {}).items() if isinstance(v, str)
    }
    retry: list[str] = [i for i in state.get("candidates") or [] if isinstance(i, str)]
    same_binary = stamp is not None and stamp == state.get("binary")
    tries: dict[str, int] = {
        k: v
        for k, v in (state.get("tries") or {}).items()
        if isinstance(k, str) and isinstance(v, int)
    }
    if not same_binary:
        tries = {}
    did_scan = False
    scanned: set[str]
    if stamp is not None and same_binary and not retry and seed is None:
        scanned = set()
        print("claude binary unchanged since the last scan", file=out)
    elif stamp is None:
        scanned = set()
        print(f"could not read {binary}", file=out)
    else:
        scanned = await asyncio.to_thread(scan_file, binary)
        did_scan = True
    served: set[str] = set()
    client_cm = omni.http_client(settings, transport=transport) if settings.configured else None
    async with contextlib.AsyncExitStack() as stack:
        client = await stack.enter_async_context(client_cm) if client_cm is not None else None
        if client is not None:
            served, _asked = await get_known(client)
        known = set(newer_models.KNOWN_CLAUDE_MODELS) | served
        found = candidates(scanned, known) if scanned else []
        wanted = list(dict.fromkeys([*(retry if not scanned else []), *found]))
        wanted = [i for i in wanted if i not in served or i == seed]
        wanted = [i for i in wanted if tries.get(i, 0) < MAX_TRIES or i == seed]
        due: list[str] = []
        for model_id in wanted:
            at = _parse_time(probed.get(model_id))
            if model_id == seed or at is None or now - at >= REPROBE_AFTER:
                due.append(model_id)
        if seed is not None:
            due = [seed, *[i for i in due if i != seed]]
        # Only the newest per family, at most MAX_PROBES a pass; a seed always goes first.
        newest: dict[str, str] = {}
        for model_id in due:
            if model_id == seed:
                continue
            family, version = model_watch.parse_id(model_id) or ("", (0, 0))
            held = model_watch.parse_id(newest[family]) if family in newest else None
            if held is None or version > held[1]:
                newest[family] = model_id
        due = ([seed] if seed is not None else []) + list(newest.values())
        due = due[:MAX_PROBES]
        probes: list[dict[str, Any]] = []
        for model_id in due:
            parsed = model_watch.parse_id(model_id)
            if parsed is None:
                continue
            ok, detail, error = await ask(claude_bin, model_id)
            probed[model_id] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if ok:
                tries.pop(model_id, None)
            elif not error:
                tries[model_id] = tries.get(model_id, 0) + 1
            probes.append(
                {
                    "id": model_id,
                    "family": parsed[0],
                    "version": list(parsed[1]),
                    "served": ok,
                    "detail": detail,
                    "probed_at": probed[model_id],
                    "error": error,
                }
            )
            print(f"{model_id}: {'served' if ok else 'not served'} ({detail})", file=out)
        report = {
            "host": os.uname().nodename[: model_watch.MAX_HOST],
            "claude_code_version": claude_version(claude_bin),
            "scanned_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "probes": probes,
        }
        if dry_run:
            print(json.dumps(report, indent=2), file=out)
            return 0
        if client is None:
            print(settings.problem, file=out)
            return 1
        try:
            answer = await post(client, report)
        except limits_relay.RelayError as exc:
            print(str(exc), file=out)
            return 1
    answered = {p["id"] for p in probes if p["served"]}
    still = [
        i
        for i in (found if scanned else retry)
        if i not in served and i not in answered and tries.get(i, 0) < MAX_TRIES
    ]
    new_state = {
        # The stamp is kept whenever a scan ran, even one that found nothing to ask.
        "binary": stamp if did_scan else state.get("binary"),
        "tries": tries,
        "candidates": still,
        "probed": dict(list(probed.items())[-50:]),
    }
    try:
        write_state(state_path, new_state)
    except OSError as exc:
        print(f"could not save {state_path}: {exc.strerror}", file=out)
    print(f"posted {len(probes)} probe(s); new: {answer.get('new', [])}", file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ae_omni_policies.model_watch_relay",
        description="Look for new Claude model ids in Claude Code, ask them, and post the result.",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the report, do not post")
    parser.add_argument(
        "--seed",
        metavar="ID",
        default=None,
        help="ask this id even when it is known (e.g. claude-fable-5-2) to prove the path",
    )
    parser.add_argument("--claude-bin", default=None, help="the claude executable (default: PATH)")
    args = parser.parse_args(argv)
    claude_bin = args.claude_bin or os.environ.get(CLAUDE_BIN_ENV) or shutil.which("claude")
    return asyncio.run(
        run(
            claude_bin=claude_bin,
            settings=omni.Settings.from_env(),
            state_path=state_file(),
            seed=args.seed,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
