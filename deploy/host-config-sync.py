#!/usr/bin/env python3
"""The planner, the safety scan and the placer behind deploy/host-config-sync.sh.

Standard library only: it runs on the box (plan, scan-tar) and inside a user's
host container (scan-dir, place), whose python is upstream's venv.

Modes (all print problems to stderr and exit non-zero):
  plan --repo R --ref REF          allowlist -> JSON plan on stdout
  scan-tar TARFILE --plan-json J   the safety scan over the archive, before it is streamed
  scan-dir [DIR] --plan-json J     the same scan over the extracted staging dir, in the container
  place [--stage DIR] --plan-json J  put the staged files where Claude Code reads them
  (DIR and --stage default to ~/.claude/.ae-sync-stage, the staging dir the shell script fills)

The allowlist is claude-config/skills-manifest.txt in ae-claude-skills (scope
team, surface any or linux) plus a fixed set of team files. Nothing here
falls back to "copy everything": a missing or unparsable manifest is an error.

The scrub file (2026-10-29). The content scan refuses any file that holds one of a list of
personal markers (the owner's e-mail local parts, home and checkout paths, company names,
tenant fragments). The list is not in source, because the values themselves would be a
leak. It is a plain text file on the box: the path in $AE_HOST_SCRUB_FILE, default
config/host-scrub.txt under the repo root (gitignored; config/host-scrub.txt.example
shows the shape, deploy/README.md says who writes it). One entry per line:
  value              a marker, refused in any synced file's content
  path<TAB>value     a marker allowed in that one repo path only (the generated brand
                     rulebook states a company fact); the value must also be listed alone
  # text             a comment; blank lines are ignored; every line is stripped
Matching is the normalised form on both sides (case, spacing, slashes, zero-width
characters), exactly as before the list moved. A missing, unreadable or empty file (no
marker line) is an error and the sync refuses to run: it never falls back to an empty list.
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import unicodedata
from pathlib import Path

# ── The lists the scan is built from (2026-10-28). Edit them here, nowhere else. ──

# Plain-English folder names Claude Code's own home uses. Refused only at the ROOT of
# the synced tree (the first path component; the archive root maps to ~/.claude): a
# nested skills/<x>/.../state/ is ordinary code and passes.
DENY_ROOT_DIRS = [
    "projects",
    "sessions",
    "session-env",
    "file-history",
    "plans",
    "paste-cache",
    "shell-snapshots",
    "state",
]
# Secrets-like folders: refused at ANY depth, like the file names and substrings below.
DENY_DIRS = [
    "node_modules",
    ".azure",
    ".omnigent",
    ".codex",
    ".ssh",
    ".config/gh",
]
# A file with one of these names (anywhere) is refused. ".env" also refuses ".env.*".
DENY_FILES = [
    ".credentials.json",
    "history.jsonl",
    "settings.json",
    "settings.local.json",
    "mcp-needs-auth-cache.json",
    "global-CLAUDE.md",
    "claude-ai-profile-paste.md",
    "link-memory.sh",
    "hb-get.sh",
    ".env",
]
# A path containing one of these anywhere is refused.
DENY_SUBSTRINGS = ["policy-limits", "remote-settings", "memory-backup"]
# File content containing a personal marker is refused. The markers are not in this file
# (2026-10-29): they are the identifiers of the owner and their companies, so they live in a
# box file, see "The scrub file" in the module docstring. load_scrub() reads it when the plan
# is made, and the plan carries the values to scan 1 (on the box) and scan 2 (in the
# container), which has no copy of the file. The scan normalises the text first (normalize()
# below), so case, spacing, slash direction and zero-width characters do not hide a marker.
SCRUB_ENV = "AE_HOST_SCRUB_FILE"
SCRUB_DEFAULT = Path(__file__).resolve().parent.parent / "config" / "host-scrub.txt"
# team-settings.json may carry ONLY these top-level keys; everything else (permissions,
# hooks, model, statusLine, autoMode, apiKeyHelper, awsAuthRefresh, sandbox,
# enableAllProjectMcpServers, ...) is the user's own call and is refused.
SETTINGS_ALLOWED_KEYS = [
    "env",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "allowedMcpServers",
    "deniedMcpServers",
]
# Inside env, these prefixes are credentials or routing and are refused.
SETTINGS_REFUSED_ENV_PREFIXES = ("ANTHROPIC_",)
# Setup commands (npm ci ...) and the register script get this long, then are killed.
SETUP_TIMEOUT_SECONDS = 600

# ── Fixed paths in the ae-claude-skills repo ──

MANIFEST = "claude-config/skills-manifest.txt"
TEAM_CLAUDE = "claude-config/team-CLAUDE.md"
TEAM_SETTINGS = "claude-config/team-settings.json"
TEAM_MCP = "claude-config/team-mcp.json"
REGISTER = "scripts/register-team-mcp.sh"
MCP_DIR = "claude-config/mcp"
IMPORT_LINE = "@~/.claude/ae-team/CLAUDE.md"
MARKER = ".ae-managed"


def fail(*lines: str) -> None:
    for line in lines:
        print(f"error: {line}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# ── plan ──


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    # GIT_LITERAL_PATHSPECS: a path from the manifest is a path, never a glob or magic.
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False, env=env)


def clean_path(p: str, what: str) -> str:
    p = p.strip().rstrip("/")
    if not p or p.startswith("/") or "\\" in p or any(s in ("", ".", "..") for s in p.split("/")):
        fail(f"{what}: '{p}' is not a plain repo-relative path")
    return p


def kind_at(repo: str, ref: str, path: str) -> str | None:
    r = git(repo, "cat-file", "-t", f"{ref}:{path}")
    return r.stdout.decode().strip() if r.returncode == 0 else None


def show(repo: str, ref: str, path: str) -> bytes | None:
    r = git(repo, "show", f"{ref}:{path}")
    return r.stdout if r.returncode == 0 else None


def parse_manifest(text: str) -> list[tuple[str, str, str]]:
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            fail(f"{MANIFEST} line {n}: expected '<path> <scope> <surface>', got '{raw}'")
        path, scope, surface = parts
        if scope not in ("team", "personal"):
            fail(f"{MANIFEST} line {n}: scope '{scope}' is not team or personal")
        if surface not in ("any", "linux", "windows"):
            fail(f"{MANIFEST} line {n}: surface '{surface}' is not any, linux or windows")
        rows.append((clean_path(path, f"{MANIFEST} line {n}"), scope, surface))
    if not rows:
        fail(f"{MANIFEST} lists no skills; refusing to guess an allowlist")
    return rows


def parse_team_mcp(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw)
        servers = data["servers"]
        if not isinstance(servers, list):
            raise TypeError("servers is not a list")
        for s in servers:
            if not isinstance(s, dict) or not isinstance(s.get("name"), str):
                raise TypeError("a server has no name")
            for key in ("sync_paths", "setup"):
                v = s.get(key, [])
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise TypeError(f"{s['name']}.{key} is not a list of strings")
    except (ValueError, KeyError, TypeError) as exc:
        fail(f"{TEAM_MCP} is not usable: {exc}")
    return servers


def check_settings(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        fail(f"{TEAM_SETTINGS} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        fail(f"{TEAM_SETTINGS} must be a JSON object of settings keys")
    bad = sorted(k for k in data if k not in SETTINGS_ALLOWED_KEYS)
    if bad:
        fail(
            f"{TEAM_SETTINGS} carries {', '.join(bad)}; only {', '.join(SETTINGS_ALLOWED_KEYS)} "
            "may be synced, the rest are each user's own. Remove them from the team file."
        )
    for key in ("env", "enabledPlugins", "extraKnownMarketplaces"):
        if key in data and not isinstance(data[key], dict):
            fail(f"{TEAM_SETTINGS}: {key} must be an object")
    for key in ("allowedMcpServers", "deniedMcpServers"):
        if key in data and not (
            isinstance(data[key], list)
            and all(
                isinstance(e, dict) and (e.get("serverName") or e.get("serverUrl"))
                for e in data[key]
            )
        ):
            fail(f"{TEAM_SETTINGS}: {key} must be a list of objects with serverName or serverUrl")
    badenv = sorted(
        k
        for k in data.get("env", {})
        if any(k.upper().startswith(x) for x in SETTINGS_REFUSED_ENV_PREFIXES)
    )
    if badenv:
        fail(
            f"{TEAM_SETTINGS}: env key(s) {', '.join(badenv)} are credentials or routing; refused"
        )
    return data


def make_plan(repo: str, ref: str) -> dict:
    warnings: list[str] = []
    commit = git(repo, "rev-parse", f"{ref}^{{commit}}").stdout.decode().strip()
    if not commit:
        fail(f"cannot resolve {ref} in {repo}")

    manifest = show(repo, ref, MANIFEST)
    if manifest is None:
        fail(
            f"{MANIFEST} is missing at {ref}; refusing to sync without an allowlist "
            "(never a copy-everything fallback)"
        )
    rows = parse_manifest(manifest.decode("utf-8", "replace"))

    skills, seen = [], {}
    for path, scope, surface in rows:
        if scope != "team" or surface not in ("any", "linux"):
            continue
        if kind_at(repo, ref, path) != "tree":
            fail(f"{MANIFEST} lists {path} as team, but it is not a folder at {ref}")
        name = path.split("/")[-1]
        if name in seen:
            warnings.append(
                f"skill name '{name}' is listed twice ({seen[name]} and {path}); keeping the first"
            )
            continue
        seen[name] = path
        skills.append({"name": name, "path": path})

    if not skills:
        fail(
            f"{MANIFEST} has {len(rows)} row(s) but none is scope team with surface any or "
            "linux; refusing to sync (it would remove every managed skill). Check the "
            "scope and surface columns."
        )

    if kind_at(repo, ref, TEAM_CLAUDE) != "blob":
        fail(f"{TEAM_CLAUDE} is missing at {ref}; it is required")

    archive = [s["path"] for s in skills] + [TEAM_CLAUDE]
    sync_paths: list[str] = []
    present: dict[str, str | None] = {}
    for key, path in (("settings", TEAM_SETTINGS), ("team_mcp", TEAM_MCP), ("register", REGISTER)):
        if kind_at(repo, ref, path) == "blob":
            present[key] = path
            archive.append(path)
        else:
            present[key] = None
            warnings.append(f"{path} is not at {ref}; skipped")
    if kind_at(repo, ref, MCP_DIR) == "tree":
        present["mcp_dir"] = MCP_DIR
        archive.append(MCP_DIR)
        sync_paths.append(MCP_DIR)
    else:
        present["mcp_dir"] = None
        warnings.append(f"{MCP_DIR}/ is not at {ref}; skipped")

    if present["settings"]:
        check_settings(show(repo, ref, TEAM_SETTINGS) or b"")

    setup: list[dict] = []
    if present["team_mcp"]:
        for server in parse_team_mcp(show(repo, ref, TEAM_MCP) or b""):
            first = None
            for sp in server.get("sync_paths", []):
                sp = clean_path(sp, f"{TEAM_MCP} {server['name']}.sync_paths")
                if kind_at(repo, ref, sp) is None:
                    warnings.append(
                        f"sync path {sp} of server {server['name']} is not at {ref}; skipped"
                    )
                    continue
                if sp not in sync_paths:
                    sync_paths.append(sp)
                    archive.append(sp)
                first = first or sp
            if server.get("setup"):
                setup.append({"name": server["name"], "cwd": first, "commands": server["setup"]})

    archive = sorted(set(archive))
    ls = git(repo, "ls-tree", "-r", "-z", ref, "--", *archive)
    if ls.returncode != 0:
        fail(f"git ls-tree failed: {ls.stderr.decode().strip()}")
    expected = []
    for rec in ls.stdout.decode().split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, otype, _sha = meta.split()
        if mode == "120000" or otype != "blob":
            fail(f"{path} is a symlink or submodule at {ref}; the sync copies regular files only")
        expected.append(path)
    return {
        "commit": commit,
        "ref": ref,
        "skills": skills,
        "files": present,
        "sync_paths": sync_paths,
        "setup": setup,
        "archive_paths": archive,
        "expected_files": sorted(expected),
        "warnings": warnings,
        "scrub": load_scrub(),
    }


# ── scan ──


def deny_reason(path: str) -> str | None:
    """Why a path is refused, or None. Names are compared in lower case."""
    parts = [x.lower() for x in path.split("/")]
    base = parts[-1]
    folders = parts[:-1]
    if folders and folders[0] in {d.lower() for d in DENY_ROOT_DIRS}:
        return f"deny root path '{folders[0]}/'"
    for d in DENY_DIRS:
        segs = d.lower().split("/")
        for i in range(len(folders) - len(segs) + 1):
            if folders[i : i + len(segs)] == segs:
                return f"deny path '{d}/'"
    deny_files = {f.lower() for f in DENY_FILES}
    if base in deny_files or (base.startswith(".env.") and ".env" in deny_files):
        return f"deny file '{base}'"
    low = path.lower()
    for sub in DENY_SUBSTRINGS:
        if sub in low:
            return f"deny path containing '{sub}'"
    return None


# ── content scan: the same normalisation as ae-claude-skills scripts/check-skills-manifest.py ──

ZERO_WIDTH = re.compile("[\u200b-\u200d\u2060\ufeff]")
BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF8, "utf-8-sig"),
)
# Binary extensions and the magic number their files really start with. A file with one
# of these extensions that fails the magic check is scanned as text.
BINARY_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".ico": (b"\x00\x00\x01\x00",),
    ".pdf": (b"%PDF-",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".gz": (b"\x1f\x8b",),
    ".woff": (b"wOFF",),
    ".woff2": (b"wOF2",),
    ".ttf": (b"\x00\x01\x00\x00", b"true"),
    ".otf": (b"OTTO",),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    ".wav": (b"RIFF",),
    ".exe": (b"MZ",),
    ".dll": (b"MZ",),
    ".pyc": (),
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = ZERO_WIDTH.sub("", text).lower()
    text = re.sub(r"[\\/]+", "/", text)
    text = re.sub(r"\s*@\s*", "@", text)
    return re.sub(r"\s+", " ", text)


def is_real_binary(ext: str, data: bytes) -> bool:
    if ext not in BINARY_MAGIC:
        return False
    if ext == ".pyc":
        return len(data) >= 4 and data[2:4] == b"\r\n"
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext == ".wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    return any(data.startswith(m) for m in BINARY_MAGIC[ext])


def decode_text(data: bytes, ext: str) -> tuple[str | None, str | None]:
    """(text, error). Text is None for a real binary file, which is skipped."""
    if is_real_binary(ext, data):
        return None, None
    for bom, codec in BOMS:
        if data.startswith(bom):
            try:
                return data.decode(codec), None
            except UnicodeDecodeError as exc:
                return (
                    None,
                    f"not valid {codec} despite its BOM ({exc.reason} at byte {exc.start})",
                )
    if b"\0" in data or ext in BINARY_MAGIC:
        # NUL bytes, or a binary extension without its magic: scan as text with the NULs
        # dropped, so BOM-less UTF-16 reads as ASCII.
        return data.decode("utf-8", errors="ignore").replace("\0", ""), None
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"not valid UTF-8 ({exc.reason} at byte {exc.start}); cannot be scanned"


def load_scrub(path: str | os.PathLike[str] | None = None) -> dict:
    """Read the scrub file (see the module docstring); fail closed when it cannot be used.

    :param path: The file; default ``$AE_HOST_SCRUB_FILE``, else ``config/host-scrub.txt``.
    :returns: ``{"markers": [str, ...], "allowed": {repo path: [str, ...]}}``.
    """
    where = Path(path or os.environ.get(SCRUB_ENV) or SCRUB_DEFAULT)
    hint = (
        f"write {where} (one marker per line; config/host-scrub.txt.example shows the shape) "
        f'or point {SCRUB_ENV} at it; deploy/README.md, "Syncing the shared AE setup", '
        "says what goes in it"
    )
    try:
        text = where.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"the host sync scrub file {where} cannot be read ({exc.strerror}); {hint}")
    markers: list[str] = []
    allowed: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            rel, value = (part.strip() for part in line.split("\t", 1))
            if rel and value:
                allowed.setdefault(rel, []).append(value)
            continue
        markers.append(line)
    if not markers:
        fail(
            f"the host sync scrub file {where} has no marker lines, "
            f"so nothing would be scanned; {hint}"
        )
    return {"markers": markers, "allowed": allowed}


def plan_scrub(plan: dict) -> dict:
    """The scrub data a plan carries; a plan without it (made by an older script) is refused."""
    scrub = plan.get("scrub")
    if not isinstance(scrub, dict) or not scrub.get("markers"):
        fail("the plan carries no scrub markers; remake it with this script's plan mode")
    return scrub


def content_problems(rel: str, data: bytes, scrub: dict) -> list[str]:
    """Problems with one file's content. A file that cannot be decoded is a failure."""
    text, err = decode_text(data, Path(rel).suffix.lower())
    if err:
        return [f"{rel}: {err}"]
    if text is None:
        return []
    # The whole text is normalised with every whitespace run (newlines included) as one
    # space, so a marker wrapped over two or three lines still matches.
    whole = normalize(text)
    allowed = {normalize(m) for m in scrub.get("allowed", {}).get(rel, ())}
    return [
        f"{rel}: personal marker {m!r}"
        for m in (normalize(x) for x in scrub["markers"])
        if m in whole and m not in allowed
    ]


def report(problems: list[str], where: str) -> None:
    if problems:
        for p in problems:
            print(f"scan ({where}): {p}", file=sys.stderr)
        sys.exit(1)


def safe_member(name: str) -> bool:
    return not name.startswith("/") and ".." not in name.split("/")


def scan_tar(tar_path: str, plan: dict) -> None:
    scrub = plan_scrub(plan)
    problems, files = [], []
    with tarfile.open(tar_path, "r:") as tar:
        for m in tar:
            name = m.name.rstrip("/")
            if m.name == "pax_global_header" or m.type == tarfile.XGLTYPE or not name:
                continue
            if not safe_member(name):
                problems.append(f"{m.name}: path escapes the archive root")
                continue
            reason = deny_reason(name)
            if reason:
                problems.append(f"{name}: {reason}")
            if m.isdir():
                continue
            if not m.isreg():
                problems.append(f"{name}: not a regular file (links and devices are refused)")
                continue
            files.append(name)
            f = tar.extractfile(m)
            problems += content_problems(name, f.read() if f else b"", scrub)
    differ(files, plan, problems)
    report(problems, "tar")


def differ(files: list[str], plan: dict, problems: list[str]) -> None:
    expected = set(plan["expected_files"])
    got = set(files)
    problems += [
        f"{p}: in the archive but not predicted by the manifest allowlist"
        for p in sorted(got - expected)
    ]
    problems += [
        f"{p}: predicted by the manifest allowlist but missing from the archive"
        for p in sorted(expected - got)
    ]


def scan_dir(root: str, plan: dict) -> None:
    scrub = plan_scrub(plan)
    problems, files = [], []
    base = Path(root)
    for dirpath, dirnames, filenames in os.walk(base):
        for n in dirnames + filenames:
            rel = (Path(dirpath) / n).relative_to(base).as_posix()
            reason = deny_reason(rel)
            if reason:
                problems.append(f"{rel}: {reason}")
        for n in dirnames:
            if (Path(dirpath) / n).is_symlink():
                rel = (Path(dirpath) / n).relative_to(base).as_posix()
                problems.append(f"{rel}: not a regular directory (links are refused)")
        for n in filenames:
            full = Path(dirpath) / n
            rel = full.relative_to(base).as_posix()
            if full.is_symlink() or not full.is_file():
                problems.append(f"{rel}: not a regular file (links and devices are refused)")
                continue
            files.append(rel)
            problems += content_problems(rel, full.read_bytes(), scrub)
    differ(files, plan, problems)
    report(problems, "container")


# ── place ──


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == MARKER or "node_modules" in rel.split("/"):
            continue
        h.update(rel.encode() + b"\0")
        if p.is_file():
            h.update(b"x" if os.access(p, os.X_OK) else b"-")
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def write_file(src: Path | bytes, dst: Path, mode: int | None = None) -> bool:
    data = src if isinstance(src, bytes) else src.read_bytes()
    if mode is None:
        mode = 0o755 if (isinstance(src, Path) and os.access(src, os.X_OK)) else 0o644
    if dst.is_file() and not dst.is_symlink() and dst.read_bytes() == data:
        if (dst.stat().st_mode & 0o777) != mode:
            dst.chmod(mode)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.ae-tmp-{os.getpid()}")
    tmp.write_bytes(data)
    tmp.chmod(mode)
    os.replace(tmp, dst)
    return True


def sync_tree(src: Path, dst: Path, keep: tuple[str, ...] = ("node_modules",)) -> int:
    """Make dst hold src's files; entries only dst has go, except `keep` at its top."""
    changed = 0
    dst.mkdir(parents=True, exist_ok=True)
    names = {p.name for p in src.iterdir()}
    for p in list(dst.iterdir()):
        if p.name not in names and p.name not in keep:
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            changed += 1
    for p in src.iterdir():
        if p.is_dir():
            changed += sync_tree(p, dst / p.name, keep=())
        else:
            changed += write_file(p, dst / p.name)
    return changed


def place_skills(stage: Path, plan: dict, home: Path) -> None:
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Swap artifacts live in a folder of ours, outside skills/, so a user's own folder
    # is never mistaken for one. Everything in it was made by this script.
    swap = home / ".claude" / ".ae-sync" / "swap"
    swap.mkdir(parents=True, exist_ok=True)
    # Recover an interrupted swap: an old copy whose skill is gone goes back, any other
    # leftover is ours and is dropped.
    for p in sorted(swap.iterdir()):
        if p.name.startswith("old-") and not (skills_dir / p.name[4:]).exists():
            os.rename(p, skills_dir / p.name[4:])
            warn(f"restored skill {p.name[4:]} from an interrupted swap")
        else:
            shutil.rmtree(p, ignore_errors=True)
    wanted = {s["name"] for s in plan["skills"]}
    for s in plan["skills"]:
        src, dst = stage / s["path"], skills_dir / s["name"]
        if dst.exists() and not (dst / MARKER).exists():
            warn(
                f"{dst} is not synced-managed (no {MARKER}); left alone, "
                f"skill '{s['name']}' skipped"
            )
            continue
        if dst.exists() and tree_digest(dst) == tree_digest(src):
            print(f"unchanged skill {s['name']}")
            continue
        new = swap / f"new-{s['name']}"
        shutil.copytree(src, new, copy_function=shutil.copy2)
        (new / MARKER).write_text("")
        if dst.exists():
            old = swap / f"old-{s['name']}"
            os.rename(dst, old)
            os.rename(new, dst)
            shutil.rmtree(old)
        else:
            os.rename(new, dst)
        print(f"placed skill {s['name']}")
    for p in sorted(skills_dir.iterdir()):
        if p.is_dir() and not p.is_symlink() and (p / MARKER).exists() and p.name not in wanted:
            shutil.rmtree(p)
            print(f"removed skill {p.name} (no longer shared)")


def place_claude_md(stage: Path, home: Path) -> None:
    team = home / ".claude" / "ae-team" / "CLAUDE.md"
    print(
        f"{'placed' if write_file(stage / TEAM_CLAUDE, team) else 'unchanged'} ae-team/CLAUDE.md"
    )
    own = home / ".claude" / "CLAUDE.md"
    if not own.exists():
        own.write_text(
            f"{IMPORT_LINE}\n\n"
            "<!-- Your own notes go below this line. The line above pulls in the shared AE\n"
            "     team rules; keep it. This file is yours and is never overwritten. -->\n"
        )
        print("created ~/.claude/CLAUDE.md")
    elif IMPORT_LINE not in own.read_text():
        warn(
            f"{own} no longer imports the team rules; add the line {IMPORT_LINE} "
            "to get them. File left as is."
        )


def _identity(entry: object) -> str:
    """Identity of a list entry: serverName / serverUrl for the MCP lists, else its JSON."""
    if isinstance(entry, dict):
        for k in ("serverName", "serverUrl"):
            if entry.get(k):
                return f"{k}={entry[k]}"
    return json.dumps(entry, sort_keys=True)


def merge_settings(cur: dict, team: dict) -> dict:
    """Deep merge: dicts merge per leaf (the team value wins), lists are unioned
    (existing first, then new entries), anything else is the team value."""
    out = dict(cur)
    for key, tv in team.items():
        cv = out.get(key)
        if isinstance(cv, dict) and isinstance(tv, dict):
            out[key] = merge_settings(cv, tv)
        elif isinstance(cv, list) and isinstance(tv, list):
            seen = {_identity(e) for e in cv}
            out[key] = cv + [
                e for e in tv if _identity(e) not in seen and not seen.add(_identity(e))
            ]
        else:
            out[key] = tv
    return out


def place_settings(stage: Path, plan: dict, home: Path) -> bool:
    """Merge the team settings into settings.json. False when the user's file is unreadable."""
    src = plan["files"]["settings"]
    if not src:
        return True
    team = check_settings((stage / src).read_bytes())
    path = home / ".claude" / "settings.json"
    lock_path = home / ".claude" / ".ae-sync" / "settings.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cur: dict = {}
        mode = 0o600
        if path.exists():
            mode = path.stat().st_mode & 0o777
            try:
                cur = json.loads(path.read_text() or "{}")
                if not isinstance(cur, dict):
                    raise ValueError("not a JSON object")
            except ValueError as exc:
                print(
                    f"SETTINGS NOT MERGED: {path} is not valid JSON ({exc}). It was left "
                    "untouched; fix or remove it and the next sync merges the team settings.",
                    file=sys.stderr,
                )
                return False
        merged = merge_settings(cur, team)
        if merged == cur and path.exists():
            print("unchanged settings.json")
            return True
        write_file((json.dumps(merged, indent=2) + "\n").encode(), path, mode)
        print(f"merged {len(team)} team key(s) into settings.json")
    return True


def run(cmd: list[str] | str, cwd: Path, label: str) -> bool:
    """Run a command in cwd; timeout -k kills it and its children after SETUP_TIMEOUT_SECONDS."""
    argv = ["bash", "-c", cmd] if isinstance(cmd, str) else cmd
    full = ["timeout", "-k", "10", str(SETUP_TIMEOUT_SECONDS), *argv]
    r = subprocess.run(full, cwd=cwd, check=False)
    if r.returncode == 124:
        print(
            f"error: {label} timed out after {SETUP_TIMEOUT_SECONDS}s and was killed",
            file=sys.stderr,
        )
    elif r.returncode != 0:
        print(f"error: {label} failed with exit {r.returncode}", file=sys.stderr)
    return r.returncode == 0


def place(stage: Path, plan: dict) -> None:
    home = Path(os.environ["HOME"])
    team_dir = home / ".claude" / "ae-team"
    mcp_root = team_dir / "mcp"
    failed = False

    place_skills(stage, plan, home)
    place_claude_md(stage, home)
    files = plan["files"]
    if files["team_mcp"]:
        write_file(stage / files["team_mcp"], team_dir / "scripts" / "team-mcp.json")
    if files["register"]:
        write_file(stage / files["register"], team_dir / "scripts" / "register-team-mcp.sh", 0o755)
    changed = 0
    for sp in plan["sync_paths"]:
        src = stage / sp
        changed += (
            sync_tree(src, mcp_root / sp) if src.is_dir() else write_file(src, mcp_root / sp)
        )
    print(f"mcp files changed: {changed}")
    if not place_settings(stage, plan, home):
        failed = True

    for item in plan["setup"]:
        cwd = mcp_root / item["cwd"] if item["cwd"] else mcp_root
        if not cwd.is_dir():
            cwd = cwd.parent
        digest = hashlib.sha256(
            (tree_digest(cwd) + "\0".join(item["commands"])).encode()
        ).hexdigest()
        stamp = team_dir / ".setup" / f"{item['name']}.sha"
        if stamp.exists() and stamp.read_text().strip() == digest:
            print(f"unchanged setup for {item['name']}")
            continue
        ok = all(run(c, cwd, f"setup '{c}' for {item['name']}") for c in item["commands"])
        if ok:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(digest + "\n")
            print(f"ran setup for {item['name']}")
        else:
            failed = True
    register = team_dir / "scripts" / "register-team-mcp.sh"
    missing = [t for t in ("jq", "claude") if shutil.which(t) is None]
    if register.exists() and missing:
        print(
            f"REGISTRATION SKIPPED: {' and '.join(missing)} not found in the container, so "
            "register-team-mcp.sh did not run; the MCP files are synced but the servers are not "
            "registered. Rebuild the host image (deploy/host-container.sh <user> build).",
            file=sys.stderr,
        )
    elif register.exists():
        if run(["bash", str(register)], home, "register-team-mcp.sh"):
            print("ran register-team-mcp.sh")
        else:
            failed = True
    if failed:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "scan-tar", "scan-dir", "place"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--repo")
    ap.add_argument("--ref")
    ap.add_argument("--stage")
    ap.add_argument("--plan-file")
    a = ap.parse_args()
    if a.mode == "plan":
        if not (a.repo and a.ref):
            fail("plan needs --repo and --ref")
        json.dump(make_plan(a.repo, a.ref), sys.stdout)
        return
    if not a.plan_file:
        fail(f"{a.mode} needs --plan-file")
    plan = json.loads(Path(a.plan_file).read_text())
    if a.mode == "scan-tar":
        scan_tar(a.target, plan)
    elif a.mode == "scan-dir":
        scan_dir(a.target, plan)
    else:
        place(Path(a.stage), plan)


if __name__ == "__main__":
    main()
