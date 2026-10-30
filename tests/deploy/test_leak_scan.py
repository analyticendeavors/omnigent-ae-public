"""scripts/leak-scan.py: nothing internal leaves the box (2026-10-28).

Every fake identifier below is assembled at run time from fragments and written to
tmp_path, so no literal of any class sits in this file. Otherwise the tree scan, the
release scan and the public repository's push protection would trip on the test itself.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "leak-scan.py"
HOOK = ROOT / "scripts" / "hooks" / "pre-push"
CI = ROOT / ".github" / "workflows" / "ci.yml"
ZERO = "0" * 40

# 2026-10-29: the private denylist holds these two words whole, so the suffixes are built from parts.
KV_SUFFIX = ".vault" + ".azure" + ".net"
TENANT_SUFFIX = "." + "onmicro" + "soft" + ".com"

# One fake value per class, each built from pieces.
DENY_WORD = "zorb" + "widget"
FAKES = {
    "denylist": f"see the {DENY_WORD} plan",
    "secret": "token ghp" + "_" + "A1b2C3d4" * 5,
    "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "tailnet-host": "host " + "box7" + ".tailz9" + ".ts" + ".net",
    "tailnet-ip": "addr " + "100." + "101.2.3",
    "lan-ip": "addr " + "10." + "7.8.9",
    "email": "mail " + "joe.smith" + "@" + "acme-corp" + ".io",
    "home-path": "dir " + "/ho" + "me/" + "zed42",
    "azure-endpoint": "url " + "zedcorp" + KV_SUFFIX,
    "cloud-id": "tenant_id = " + "6fa459ea-" + "ee8a-3ca4-894e-db77e160355e",
    "private-repo-link": "see https://github." + "com/analytic" + "endeavors/" + "secret-repo-x",
}
PATH_FAKE = "notes-" + DENY_WORD + ".txt"


def scan(*args: str, cwd: Path | None = None, stdin: str | None = None, env=None):
    # CI sets GITHUB_ACTIONS, which switches the output to annotations
    env = {k: v for k, v in (env or os.environ).items() if not k.startswith("GITHUB_")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def digest(value: str) -> str:
    """The allowlist key of a value: sha256 of its lower-cased text."""
    return "sha256:" + hashlib.sha256(value.lower().encode()).hexdigest()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def bundle(tmp_path: Path, *lines: str) -> Path:
    return write(tmp_path / "bundle.txt", "# fake bundle\n" + "\n".join(lines) + "\n")


def empty_allowlist(tmp_path: Path) -> Path:
    return write(tmp_path / "allow.txt", "# none\n")


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, message: str, **files: str) -> str:
        for name, text in files.items():
            write(self.path / name.replace("__", "/"), text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


HIT = re.compile(r"^\S.*:\d+: [a-z-]+ \S+$")


def hit_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stdout.splitlines() if HIT.match(line)]


def no_value_echoed(proc: subprocess.CompletedProcess[str], *values: str) -> None:
    out = proc.stdout + proc.stderr
    for value in values:
        assert value.lower() not in out.lower(), "a matched value was printed"


# --- image builders -----------------------------------------------------------------


def png(*extra: tuple[bytes, bytes]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + body))
        return struct.pack(">I", len(body)) + kind + body + crc

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x10\x20\x30")
    parts = [chunk(b"IHDR", ihdr), *(chunk(k, b) for k, b in extra), chunk(b"IDAT", idat)]
    return b"\x89PNG\r\n\x1a\n" + b"".join(parts) + chunk(b"IEND", b"")


def jpeg(*segments: tuple[int, bytes]) -> bytes:
    out = b"\xff\xd8"
    for marker, body in segments:
        out += bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body
    scan_header = b"\x01\x01\x00\x00\x3f\x00"
    return out + b"\xff\xda" + struct.pack(">H", 8) + scan_header + b"\x12\x34\x56\xff\xd9"


def webp(*, exif: bool, xmp: bool) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        pad = b"\0" if len(body) & 1 else b""
        return kind + struct.pack("<I", len(body)) + body + pad

    flags = 0x20 | (0x08 if exif else 0) | (0x04 if xmp else 0)
    body = chunk(b"VP8X", bytes([flags]) + b"\0" * 9) + chunk(b"VP8 ", b"pixeldata12")
    if exif:
        body += chunk(b"EXIF", b"Exif\0\0data")
    if xmp:
        body += chunk(b"XMP ", b"<x:xmpmeta/>")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def animated_webp(*, anim: bool = True) -> bytes:
    """A WebP with the animation structure only; it has to parse, not decode."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        pad = b"\0" if len(body) & 1 else b""
        return kind + struct.pack("<I", len(body)) + body + pad

    body = chunk(b"VP8X", bytes([0x02]) + b"\0" * 9)
    if anim:
        body += chunk(b"ANIM", b"\0" * 6) + chunk(b"ANMF", b"\0" * 16 + b"pixeldata12")
    else:
        body += chunk(b"VP8 ", b"pixeldata12")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


# --- the classes -----------------------------------------------------------------------


def test_every_class_fires_once_and_no_value_is_echoed(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    files = {f"{cls}.txt": text + "\n" for cls, text in FAKES.items()}
    repo.commit("Add fixtures", **files)
    write(repo.path / PATH_FAKE, "plain\n")
    (repo.path / "meta.png").write_bytes(png((b"tEXt", b"Comment\0hello")))
    repo.commit("Add more")
    proc = scan(
        "--denylist-bundle",
        str(bundle(tmp_path, DENY_WORD)),
        "--allowlist",
        str(empty_allowlist(tmp_path)),
        "tree",
        "--ref",
        "HEAD",
        cwd=repo.path,
    )
    assert proc.returncode == 1
    shown = hit_lines(proc)
    for cls in [*FAKES, "path-name", "media-metadata"]:
        assert sum(f" {cls} " in f"{line} " for line in shown) == 1, (cls, shown)
    no_value_echoed(
        proc,
        DENY_WORD,
        "joe.smith",
        "zed42",
        "6fa459ea",
        "secret-repo-x",
        "zedcorp",
        "box7",
        "101.2.3",
        "7.8.9",
        "A1b2C3d4",
    )


def test_documentation_values_and_placeholders_pass(tmp_path: Path) -> None:
    text = "\n".join(
        [
            "ips 192.0.2.5 198.51.100.7 203.0.113.9 2001:db8::1 127.0.0.1 0.0.0.0",
            "cidr 100.64.0.0/10 and 10.0.0.0/8 and fd7a:115c:a1e0::/48",
            "version pkg@1.2.3.4 and v10.1.2.3 and 10.1.2.3.4",
            "mail user" + "@example.com a@b.example no" + "reply" + "@" + "anthropic.com",
            "bot 1+x" + "@users.noreply.github.com and git" + "@github.com:org/repo.git",
            "host <box-hostname>.<tailnet>.ts.net and box.tailnet.ts" + ".net",
            "paths /home/user/x /home/demo C:\\Users\\you and /home/$USER and /home/<user>",
            "ids 00000000-0000-0000-0000-000000000000 tenant 11111111-1111-1111-1111-111111111111",
            "azure contoso" + KV_SUFFIX + " and your-name" + TENANT_SUFFIX,
            "link github.com/analytic"
            + "endeavors/omnigent-ae/pull/1 (public set, default allowlist)",
        ]
    )
    path = write(tmp_path / "doc.txt", text)
    proc = scan("text", str(path))
    assert proc.returncode == 0, proc.stdout


def test_a_lockfile_skips_the_ip_classes(tmp_path: Path) -> None:
    line = "version: 10." + "7.8.9\n"
    repo = Repo(tmp_path / "r")
    repo.commit("Add", pnpm_lock="x\n")
    repo.commit("Lock", **{"pnpm-lock.yaml": line, "notes.txt": line})
    proc = scan(
        "--allowlist", str(empty_allowlist(tmp_path)), "tree", "--ref", "HEAD", cwd=repo.path
    )
    assert [h.split(":")[0] for h in hit_lines(proc)] == ["notes.txt"]


# --- markers and the allowlist -----------------------------------------------------------


def marker(classes: str, reason: str | None = "a reviewed fixture") -> str:
    tail = f' reason="{reason}"' if reason else ""
    return "leak-scan" + ": allow=" + classes + tail


def test_a_marker_with_a_reason_clears_a_soft_class(tmp_path: Path) -> None:
    value = FAKES["email"]
    same = write(tmp_path / "same.txt", f"{value}  # {marker('email')}\n")
    above = write(tmp_path / "above.txt", f"# {marker('email')}\n{value}\n")
    assert scan("text", str(same)).returncode == 0
    assert scan("text", str(above)).returncode == 0


def test_a_marker_without_a_reason_fails_and_clears_nothing(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", f"{FAKES['email']}  # {marker('email', None)}\n")
    proc = scan("text", str(path))
    assert proc.returncode == 1
    out = proc.stdout
    assert "marker no-reason" in out and "email address" in out


def test_a_marker_cannot_clear_the_hard_classes(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", f"{FAKES['secret']}  # {marker('secret')}\n")
    proc = scan("text", str(path))
    assert proc.returncode == 1
    assert "secret github-token" in proc.stdout and "marker cannot-clear-secret" in proc.stdout


def test_the_allowlist_clears_by_literal_and_by_path(tmp_path: Path) -> None:
    address = FAKES["email"].split()[1]
    allow = write(
        tmp_path / "allow.txt", f"email {digest(address)} sub/* # a reviewed fixture address\n"
    )
    here = write(tmp_path / "sub" / "a.txt", FAKES["email"] + "\n")
    elsewhere = write(tmp_path / "b.txt", FAKES["email"] + "\n")
    # the glob is matched against the label given to the scanner, here the file name
    assert scan("--allowlist", str(allow), "text", str(here)).returncode == 1
    glob_all = write(
        tmp_path / "all.txt", f"email {digest(address)} # a reviewed fixture address\n"
    )
    assert scan("--allowlist", str(glob_all), "text", str(elsewhere)).returncode == 0


@pytest.mark.parametrize(
    "line",
    [
        "denylist something # reason",
        "secret /./ # reason",
        "email somebody@example.invalid",
        "nonsense foo # reason",
        "email # reason",
    ],
)
def test_a_bad_allowlist_line_is_a_scan_failure(tmp_path: Path, line: str) -> None:
    allow = write(tmp_path / "allow.txt", line + "\n")
    proc = scan("--allowlist", str(allow), "text", str(write(tmp_path / "a.txt", "x\n")))
    assert proc.returncode == 2
    assert "leak-scan failed" in proc.stderr


def test_an_allowlist_that_matches_a_denylist_entry_is_refused(tmp_path: Path) -> None:
    for line in (
        f"email {DENY_WORD}* # reason",
        f"email /{DENY_WORD}/ # reason",
        f"email /{DENY_WORD[:5]}.*/ # reason",
        f"email other # mentions {DENY_WORD} in its reason",
    ):
        allow = write(tmp_path / "allow.txt", line + "\n")
        proc = scan(
            "--denylist-bundle",
            str(bundle(tmp_path, DENY_WORD)),
            "--allowlist",
            str(allow),
            "text",
            str(write(tmp_path / "a.txt", "x\n")),
        )
        assert proc.returncode == 2, line
        no_value_echoed(proc, DENY_WORD)


# --- patch files -------------------------------------------------------------------------


def patch_text(context: str, added: str, header_extra: str = "") -> str:
    return (
        "From 0 Mon Sep 17 00:00:00 2001\n"
        "From: A Dev <" + "dev" + "@" + "upstream-corp" + ".io>\n"
        "Subject: [PATCH] [ae-patch] Something\n"
        f"{header_extra}\n---\n a | 2 +-\n\n"
        "diff --git a/a b/a\nindex 1..2 100644\n--- a/a\n+++ b/a\n"
        f"@@ -1,2 +1,2 @@\n {context}\n-removed\n+{added}\n-- \n2.43.0\n"
    )


def test_patch_context_is_upstreams_but_added_lines_are_ours(tmp_path: Path) -> None:
    email = FAKES["email"].split()[1]
    benign = write(tmp_path / "patches" / "0001-a.patch", patch_text(f"ctx {email}", "fine"))
    added = write(tmp_path / "patches" / "0002-b.patch", patch_text("ctx", f"new {email}"))
    allow = empty_allowlist(tmp_path)
    ok = scan("--allowlist", str(allow), "text", str(benign))
    assert ok.returncode == 1, "text mode treats the file as prose; the patch rules need a patch"
    for name, want in ((benign, 0), (added, 1)):
        repo = Repo(tmp_path / f"r{want}")
        repo.commit("Add", **{"patches__0001-a.patch": name.read_text()})
        proc = scan("--allowlist", str(allow), "tree", "--ref", "HEAD", cwd=repo.path)
        assert proc.returncode == want, (want, proc.stdout)
        if want:
            assert hit_lines(proc) == ["patches/0001-a.patch:15: email address"]


def test_a_patch_header_author_line_is_exempt_but_the_denylist_still_applies(
    tmp_path: Path,
) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("Add", **{"patches__0001-a.patch": patch_text(f"ctx {DENY_WORD}", "ok")})
    args = ("--denylist-bundle", str(bundle(tmp_path, DENY_WORD)))
    proc = scan(
        *args,
        "--allowlist",
        str(empty_allowlist(tmp_path)),
        "tree",
        "--ref",
        "HEAD",
        cwd=repo.path,
    )
    assert proc.returncode == 1
    assert [h.split()[1:] for h in hit_lines(proc)] == [["denylist", "entry-1"]]


# --- diff, tree, text, push -----------------------------------------------------------------


def test_diff_flags_only_what_the_change_adds(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    old = FAKES["email"]
    repo.commit("Base", **{"old.txt": f"{old}\nline\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "-b", "topic")
    new_value = FAKES["lan-ip"]
    repo.commit(
        "Add a line with " + FAKES["email"].split()[1],
        **{"old.txt": f"{old}\nline\n{new_value}\n", f"dir__{PATH_FAKE}": "x\n"},
    )
    head = repo.git("rev-parse", "HEAD")
    env = {**os.environ, "PR_TITLE": "Title " + FAKES["tailnet-ip"], "PR_BODY": "ok"}
    proc = scan(
        "--denylist-bundle",
        str(bundle(tmp_path, DENY_WORD)),
        "--allowlist",
        str(empty_allowlist(tmp_path)),
        "diff",
        "--base",
        base,
        "--head",
        head,
        "--title-env",
        "PR_TITLE",
        "--body-env",
        "PR_BODY",
        cwd=repo.path,
        env=env,
    )
    shown = hit_lines(proc)
    assert any(h.startswith("old.txt@") and h.endswith(":3: lan-ip rfc1918") for h in shown)
    assert not any(":1: " in line and line.startswith("old.txt") for line in shown), "an old line"
    assert any(line.startswith("commit ") and " email " in line for line in shown)
    assert "pr-title:1: tailnet-ip cgnat-v4" in shown
    assert any(" path-name " in line and "***" in line for line in shown)
    no_value_echoed(proc, DENY_WORD, "101.2.3", "7.8.9", "joe.smith")


def test_new_lines_scope_fires_in_diff_not_in_tree_and_outside_patches_skips_patches(
    tmp_path: Path,
) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", base="x\n")
    repo.commit(
        "Add",
        **{"a.txt": f"{DENY_WORD}-one\n", "patches__0001-a.patch": f"{DENY_WORD}-two\n"},
    )
    head = repo.git("rev-parse", "HEAD")
    lines = f"@new-lines {DENY_WORD}-one", f"@outside-patches {DENY_WORD}-two"
    common = ("--denylist-bundle", str(bundle(tmp_path, *lines)))
    allow = ("--allowlist", str(empty_allowlist(tmp_path)))
    diff = scan(*common, *allow, "diff", "--base", base, "--head", head, cwd=repo.path)
    assert [re.sub(r"@[0-9a-f]{10}", "", h) for h in hit_lines(diff)] == [
        "a.txt:1: denylist entry-1"
    ]
    tree = scan(*common, *allow, "tree", "--ref", "HEAD", cwd=repo.path)
    assert hit_lines(tree) == []
    assert tree.returncode == 0, "outside-patches skips patches/, new-lines skips tree mode"


def test_tree_reads_a_bare_repository_by_tree_id(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("Add", **{"a.txt": FAKES["email"] + "\n"})
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo.path), str(bare)], check=True)
    tree_id = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "HEAD^{tree}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    proc = scan(
        "--allowlist",
        str(empty_allowlist(tmp_path)),
        "tree",
        "--git-dir",
        str(bare),
        "--tree",
        tree_id,
    )
    assert hit_lines(proc) == ["a.txt:1: email address"]


def test_text_reads_stdin_and_names_the_source(tmp_path: Path) -> None:
    proc = scan("text", "-", stdin=FAKES["lan-ip"] + "\n")
    assert hit_lines(proc) == ["<stdin>:1: lan-ip rfc1918"]


def test_push_scans_the_outgoing_commits(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("Base", a="x\n")
    sha = repo.commit("Add", b=FAKES["email"] + "\n")
    stdin = f"refs/heads/main {sha} refs/heads/main {ZERO}\n"
    allow = ("--allowlist", str(empty_allowlist(tmp_path)))
    proc = scan(*allow, "push", "fork", cwd=repo.path, stdin=stdin)
    assert proc.returncode == 1
    assert re.search(r"b@[0-9a-f]{10}:1: email address", proc.stdout)
    clean = repo.commit("Clean", c="fine\n")
    ok = scan(
        *allow,
        "push",
        "fork",
        cwd=repo.path,
        stdin=f"refs/heads/main {clean} refs/heads/main {sha}\n",
    )
    assert ok.returncode == 0


def test_the_pre_push_hook_blocks_a_push(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = Repo(tmp_path / "r")
    repo.git("config", "core.hooksPath", str(HOOK.parent))
    repo.git("remote", "add", "origin", str(remote))
    repo.commit("Base", a="x\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
    }
    env.pop("AE_LEAK_DENYLIST_FILE", None)
    env["AE_LEAK_ALLOW_NO_DENYLIST"] = "1"
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo.path), *a], capture_output=True, text=True, env=env, check=False
    )
    assert run("push", "origin", "main").returncode == 0
    repo.commit("Leak", b=FAKES["tailnet-ip"] + "\n")
    blocked = run("push", "origin", "main")
    assert blocked.returncode != 0
    assert "tailnet-ip" in blocked.stdout + blocked.stderr
    assert "no denylist bundle" in blocked.stderr


# --- media -------------------------------------------------------------------------------------


def test_png_text_chunks_are_found_and_stripped_losslessly(tmp_path: Path) -> None:
    data = png((b"tEXt", b"Comment\0hello"), (b"zzZz", b"junk"))
    path = tmp_path / "a.png"
    path.write_bytes(data)
    found = scan("media", "--ocr", "off", str(path))
    assert sorted(h.rsplit(" ", 1)[1] for h in hit_lines(found)) == [
        "png-tEXt",
        "png-unknown-chunk",
    ]
    stripped = scan("media", "--strip", "--ocr", "off", str(path))
    assert stripped.returncode == 0
    assert path.read_bytes() == png(), "the metadata and unknown chunks went"


def test_jpeg_metadata_is_found_and_the_encoder_comment_is_allowed(tmp_path: Path) -> None:
    exif = jpeg((0xE0, b"JFIF\0"), (0xE1, b"Exif\0\0data"), (0xFE, b"made by someone"))
    path = tmp_path / "a.jpg"
    path.write_bytes(exif)
    rules = sorted(h.split()[-1] for h in hit_lines(scan("media", "--ocr", "off", str(path))))
    assert rules == ["jpeg-comment", "jpeg-exif"]
    scan("media", "--strip", "--ocr", "off", str(path))
    assert path.read_bytes() == jpeg((0xE0, b"JFIF\0")), "scan data is byte-identical"
    lavc = tmp_path / "b.jpg"
    lavc.write_bytes(jpeg((0xE0, b"JFIF\0"), (0xFE, b"Lavc61.3.100\0")))
    assert scan("media", "--ocr", "off", str(lavc)).returncode == 0


def test_webp_exif_and_xmp_are_found_and_the_flags_cleared(tmp_path: Path) -> None:
    path = tmp_path / "a.webp"
    path.write_bytes(webp(exif=True, xmp=True))
    rules = sorted(h.split()[-1] for h in hit_lines(scan("media", "--ocr", "off", str(path))))
    assert rules == ["webp-exif", "webp-xmp"]
    scan("media", "--strip", "--ocr", "off", str(path))
    assert path.read_bytes() == webp(exif=False, xmp=False)


def test_a_damaged_image_is_a_hit_not_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(b"not an image")
    proc = scan("media", "--ocr", "off", str(path))
    assert proc.returncode == 1 and "media-metadata unparseable" in proc.stdout


def test_ocr_text_is_checked_when_tesseract_exists(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    exe = write(fake_bin / "tesseract", f"#!/bin/sh\necho '{FAKES['lan-ip']}'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    image = tmp_path / "a.png"
    image.write_bytes(png())
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    proc = scan("media", "--ocr", "auto", str(image), env=env)
    assert hit_lines(proc) == [f"{image}:1: media-text lan-ip-rfc1918"]
    no_value_echoed(proc, "7.8.9")
    assert scan("media", "--ocr", "off", str(image), env=env).returncode == 0
    bare = {**os.environ, "PATH": str(Path(shutil.which("git") or "/usr/bin/git").parent)}
    if shutil.which("tesseract", path=bare["PATH"]) is None:
        assert scan("media", "--ocr", "auto", str(image), env=bare).returncode == 0


def _exe(path: Path, text: str) -> Path:
    write(path, text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_ocr_bin(
    tmp_path: Path, *, dump: bool = True, dump_exit: int = 0, fail_on: str = ""
) -> tuple[dict, Path]:
    """A bin dir with a fake tesseract (logs each call and its thread limit, exits 1 on a
    file named *fail_on*) and, optionally, a fake anim_dump."""
    fake_bin = tmp_path / "bin"
    log = tmp_path / "tesseract.log"
    fail = f'case "$1" in *{fail_on}) exit 1;; esac\n' if fail_on else ""
    _exe(
        fake_bin / "tesseract",
        f"#!/bin/sh\n{fail}echo \"$1 ${{OMP_THREAD_LIMIT:-unset}}\" >> '{log}'\n"
        f"echo '{FAKES['lan-ip']}'\n",
    )
    if dump:
        # Three frames, the first two identical. Wrong flags write nothing and exit 1.
        _exe(
            fake_bin / "anim_dump",
            f"#!{sys.executable}\n"
            "import sys\n"
            "a = sys.argv[1:]\n"
            "if len(a) != 5 or a[0] != '-folder' or a[2] != '-prefix':\n"
            "    sys.exit(1)\n"
            f"if {dump_exit}:\n"
            f"    sys.exit({dump_exit})\n"
            "for n, body in enumerate((b'one', b'one', b'two')):\n"
            "    open(f'{a[1]}/{a[3]}{n:04d}.png', 'wb').write(body)\n",
        )
    base = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    path = f"{fake_bin}{os.pathsep}{base}"
    return {**os.environ, "PATH": path}, log


def test_an_animated_webp_is_read_frame_by_frame(tmp_path: Path) -> None:
    env, log = _fake_ocr_bin(tmp_path)
    image = tmp_path / "a.webp"
    image.write_bytes(animated_webp())
    proc = scan("media", "--ocr", "require", str(image), env=env)
    # two distinct frames, each echoing one line plus the joining newline
    assert len(hit_lines(proc)) == 2, proc.stderr
    no_value_echoed(proc, "7.8.9")
    lines = log.read_text().splitlines()
    assert len(lines) == 2, "tesseract runs once per distinct frame"
    assert all(line.endswith(" 1") for line in lines), "frames run single-threaded"


def test_an_animated_webp_without_anim_dump_fails_closed(tmp_path: Path) -> None:
    env, _ = _fake_ocr_bin(tmp_path, dump=False)
    image = tmp_path / "a.webp"
    image.write_bytes(animated_webp())
    for mode in ("require", "auto"):
        proc = scan("media", "--ocr", mode, str(image), env=env)
        assert proc.returncode == 2 and "webp" in proc.stderr, mode


def test_a_failing_anim_dump_is_a_failure(tmp_path: Path) -> None:
    env, _ = _fake_ocr_bin(tmp_path, dump_exit=3)
    image = tmp_path / "a.webp"
    image.write_bytes(animated_webp())
    proc = scan("media", "--ocr", "require", str(image), env=env)
    assert proc.returncode == 2 and "anim_dump failed" in proc.stderr


def test_the_vp8x_animation_flag_alone_takes_the_animated_path(tmp_path: Path) -> None:
    env, log = _fake_ocr_bin(tmp_path)
    image = tmp_path / "a.webp"
    image.write_bytes(animated_webp(anim=False))
    proc = scan("media", "--ocr", "require", str(image), env=env)
    assert len(hit_lines(proc)) == 2, proc.stderr
    assert len(log.read_text().splitlines()) == 2


def test_a_still_webp_does_not_use_anim_dump(tmp_path: Path) -> None:
    env, log = _fake_ocr_bin(tmp_path, dump=False)
    image = tmp_path / "a.webp"
    image.write_bytes(webp(exif=False, xmp=False))
    proc = scan("media", "--ocr", "require", str(image), env=env)
    assert hit_lines(proc) == [f"{image}:1: media-text lan-ip-rfc1918"], proc.stderr
    assert log.read_text().splitlines()[0].endswith(" unset"), "a still image keeps its threads"
    assert len(log.read_text().splitlines()) == 1


def test_a_truncated_webp_does_not_crash_the_ocr_step(tmp_path: Path) -> None:
    env, _ = _fake_ocr_bin(tmp_path, dump=False)
    image = tmp_path / "a.webp"
    image.write_bytes(animated_webp()[:-5])
    proc = scan("media", "--ocr", "require", str(image), env=env)
    assert "Traceback" not in proc.stderr and proc.returncode in (1, 2)


def test_an_animated_webp_is_found_by_its_bytes_not_its_name(tmp_path: Path) -> None:
    env, log = _fake_ocr_bin(tmp_path)
    image = tmp_path / "a.png"
    image.write_bytes(animated_webp())
    scan("media", "--ocr", "require", str(image), env=env)
    assert len(log.read_text().splitlines()) == 2, "the frames were read, not the file"


def test_a_frame_tesseract_cannot_read_fails_the_scan(tmp_path: Path) -> None:
    env, _ = _fake_ocr_bin(tmp_path, fail_on="f0002.png")
    image = tmp_path / "a.webp"
    image.write_bytes(animated_webp())
    proc = scan("media", "--ocr", "require", str(image), env=env)
    assert proc.returncode == 2 and "tesseract failed" in proc.stderr
    no_value_echoed(proc, "7.8.9")


# --- failure modes and output ----------------------------------------------------------------------


def test_an_unreadable_input_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    proc = scan("text", str(tmp_path / "missing.txt"))
    assert proc.returncode == 2 and "Cannot read" in proc.stderr


def test_a_required_denylist_that_is_missing_or_empty_fails_closed(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "fine\n")
    assert scan("--require-denylist", "text", str(path)).returncode == 2
    empty = bundle(tmp_path)
    assert (
        scan("--require-denylist", "--denylist-bundle", str(empty), "text", str(path)).returncode
        == 2
    )
    gone = str(tmp_path / "gone.txt")
    assert scan("--denylist-bundle", gone, "text", str(path)).returncode == 2


def test_a_bad_bundle_is_a_failure(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "fine\n")
    for line in ("ab", "@sideways something"):
        proc = scan("--denylist-bundle", str(bundle(tmp_path, line)), "text", str(path))
        assert proc.returncode == 2, line


def test_output_is_capped_at_fifty_lines_with_counts(tmp_path: Path) -> None:
    lines = "".join(f"addr 10.{i}.8.9\n" for i in range(1, 121))
    path = write(tmp_path / "many.txt", lines)
    report = tmp_path / "report.txt"
    proc = scan("--report", str(report), "text", str(path))
    assert proc.returncode == 1
    assert len(hit_lines(proc)) == 50
    assert "70 more hits" in proc.stdout and "lan-ip=120" in proc.stdout
    assert len(report.read_text().splitlines()) == 120


def test_the_committed_tree_is_clean_on_the_generic_classes() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    proc = scan("tree", "--ref", "HEAD", cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the CI job -------------------------------------------------------------------------------------


def test_the_leaks_job_meets_the_workflow_standards() -> None:
    wf = yaml.safe_load(CI.read_text())
    job = wf["jobs"]["leaks"]
    assert job["timeout-minutes"] <= 10 and job["if"] == "github.event_name == 'push'"
    assert wf["permissions"] == {"contents": "read"} and "concurrency" in wf
    runs = [s for s in job["steps"] if "run" in s]
    assert runs, "the job runs the scanner"
    for step in runs:
        assert step["run"].lstrip().startswith("set -euo pipefail")
        assert "${{" not in step["run"], "values reach run bodies through env:"
    env = {k: str(v) for s in runs for k, v in s.get("env", {}).items()}
    assert "secrets.AE_LEAK_DENYLIST_B64" in env["DENYLIST_B64"]
    assert "vars.AE_LEAK_DENYLIST_SHA256" in env["DENYLIST_SHA256"]
    assert "github.repository" in env["PRIVATE_REPO"]
    checkout = next(
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0 and "submodules" not in checkout["with"]


def test_the_leaks_job_hands_the_secret_and_its_hash_to_the_scanner() -> None:
    body = yaml.safe_load(CI.read_text())["jobs"]["leaks"]["steps"][-1]["run"]
    assert "--allowlist-from head" in body, "a push to main uses the merged head's allowlist"
    assert "--denylist-b64-env DENYLIST_B64" in body
    assert "--denylist-sha256-env DENYLIST_SHA256" in body
    assert "--denylist-missing fail" in body, "a missing secret fails the push scan"
    assert "--denylist-missing warn" not in body
    assert "tree --ref HEAD" in body and " diff " in body


def test_the_workflow_encodes_a_bundle_the_scanner_accepts(tmp_path: Path) -> None:
    """What the secret holds (one base64 line) decodes to a bundle the scanner loads."""
    raw = "# fake\n" + DENY_WORD + "\n@new-lines other" + "thing\n"
    one_line = base64.b64encode(raw.encode()).decode()
    decoded = write(tmp_path / "d.txt", base64.b64decode(one_line).decode())
    proc = scan(
        "--denylist-bundle", str(decoded), "text", str(write(tmp_path / "a.txt", DENY_WORD))
    )
    assert hit_lines(proc) == ["%s:1: denylist entry-1" % (tmp_path / "a.txt")]


# ======================================================================================
# Round 1 review fixes (2026-10-28). Each test fails without its fix.
# ======================================================================================

LAN = "10." + "7.8.9"
CGNAT = "100." + "101.2.3"
GUID_FAKE = "6fa459ea-" + "ee8a-3ca4-894e-db77e160355e"
NO_ALLOW = ("--allowlist", "/dev/null")


def load_scanner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("leak_scan_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["leak_scan_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def run_scan(*args, extra=None, **kwargs):
    """Like scan(), with extra environment variables set after GITHUB_* are removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=kwargs.get("cwd"),
        input=kwargs.get("stdin"),
    )


def diff_args(repo: Repo, base: str, head: str = "HEAD", *more: str) -> tuple[str, ...]:
    return (*NO_ALLOW, "diff", "--base", base, "--head", head, *more)


# --- 1. every commit, not just the net diff ---------------------------------------------


def test_a_value_added_then_removed_is_caught_in_diff_and_push(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    first = repo.commit("Add", b=FAKES["tailnet-ip"] + "\n")
    repo.commit("Remove", b="gone\n")
    diff = scan(*diff_args(repo, base), cwd=repo.path)
    assert diff.returncode == 1 and f"b@{first[:10]}:1: tailnet-ip cgnat-v4" in diff.stdout
    head = repo.git("rev-parse", "HEAD")
    push = scan(
        *NO_ALLOW,
        "push",
        "fork",
        cwd=repo.path,
        stdin=f"refs/heads/t {head} refs/heads/t {ZERO}\n",
    )
    assert push.returncode == 1 and f"b@{first[:10]}:1: tailnet-ip" in push.stdout


def test_a_merge_adds_only_what_neither_parent_had(tmp_path: Path) -> None:
    """Merging main into a topic branch must not rescan main's own history as the PR's."""
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.git("checkout", "-q", "-b", "topic")
    repo.commit("Topic", t="fine\n")
    repo.git("checkout", "-q", "main")
    repo.commit("Main moves", m=FAKES["email"] + "\n")  # in main, not the PR
    repo.git("checkout", "-q", "topic")
    repo.git("merge", "-q", "--no-edit", "main")
    proc = scan(*diff_args(repo, "main"), cwd=repo.path)
    assert proc.returncode == 0, proc.stdout
    assert base


# --- 2. the push range ------------------------------------------------------------------


def _two_remotes(tmp_path: Path):
    repo = Repo(tmp_path / "r")
    for name in ("origin", "fork"):
        bare = tmp_path / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        repo.git("remote", "add", name, str(bare))
    return repo


def test_a_commit_only_on_the_private_origin_is_scanned_on_its_way_to_the_fork(
    tmp_path: Path,
) -> None:
    repo = _two_remotes(tmp_path)
    repo.commit("Base", a="x\n")
    leaky = repo.commit("Leaks", b=FAKES["email"] + "\n")
    repo.git("push", "-q", "origin", "main")  # now refs/remotes/origin/main holds it
    line = f"refs/heads/main {leaky} refs/heads/ae/topic {ZERO}\n"
    to_fork = scan(*NO_ALLOW, "push", "fork", cwd=repo.path, stdin=line)
    assert to_fork.returncode == 1 and "email address" in to_fork.stdout
    to_origin = scan(*NO_ALLOW, "push", "origin", cwd=repo.path, stdin=line)
    assert to_origin.returncode == 0, "origin already has it, so nothing is new to origin"


@pytest.mark.parametrize(
    "line",
    [
        "refs/heads/main {sha} refs/heads/main " + "1" * 40,  # remote tip unknown here
        "refs/heads/main {sha} refs/heads/main",  # too few fields
        "refs/heads/main not-a-sha refs/heads/main " + ZERO,
        "refs/heads/main " + "2" * 40 + " refs/heads/main " + ZERO,  # local object missing
    ],
)
def test_a_push_that_cannot_be_ranged_fails_closed(tmp_path: Path, line: str) -> None:
    repo = _two_remotes(tmp_path)
    sha = repo.commit("Base", a="x\n")
    proc = scan(*NO_ALLOW, "push", "fork", cwd=repo.path, stdin=line.format(sha=sha) + "\n")
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_push_needs_the_destination_remote(tmp_path: Path) -> None:
    repo = _two_remotes(tmp_path)
    sha = repo.commit("Base", a="x\n")
    proc = scan(
        *NO_ALLOW, "push", cwd=repo.path, stdin=f"refs/heads/m {sha} refs/heads/m {ZERO}\n"
    )
    assert proc.returncode == 2 and "remote" in proc.stderr


@pytest.mark.parametrize(
    ("holder", "already_there"),
    [
        ("refs/remotes/origin/main", True),
        ("refs/remotes/fork/main", False),
        ("refs/remotes/origin/deleted", False),
    ],
)
def test_an_update_skips_only_the_destinations_default_branch(
    tmp_path: Path, holder: str, already_there: bool
) -> None:
    # 2026-10-29: merging main into a pushed branch put main's commits in the update's
    # range, and the hook refused the push on a main commit's author. A commit on the
    # destination's default branch is not new there. One held only by another remote is,
    # and so is one under another tracking ref of the destination: that branch may have
    # been deleted on the remote since this clone fetched it (second review, 2026-10-29).
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.git("checkout", "-q", "-b", "topic")
    topic_tip = repo.commit("Topic", t="fine\n")
    repo.git("update-ref", "refs/remotes/origin/topic", topic_tip)
    repo.git("checkout", "-q", "main")
    write(repo.path / "b", FAKES["email"] + "\n")
    repo.git("add", "-A")
    repo.git("-c", f"user.email={FAKES['email']}", "commit", "-q", "-m", "On main")
    repo.git("update-ref", holder, repo.git("rev-parse", "HEAD"))
    repo.git("checkout", "-q", "topic")
    repo.git("merge", "-q", "--no-edit", "main")
    merged = repo.git("rev-parse", "HEAD")
    assert base != merged
    proc = scan(
        *NO_ALLOW,
        "push",
        "origin",
        cwd=repo.path,
        stdin=f"refs/heads/topic {merged} refs/heads/topic {topic_tip}\n",
    )
    if already_there:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode == 1
        assert re.search(r"b@[0-9a-f]{10}:1: email address", proc.stdout)
        assert "commit-identity email" in proc.stdout
    no_value_echoed(proc, FAKES["email"])


def test_unparseable_raw_diff_output_is_a_scan_failure() -> None:
    mod = load_scanner()

    class Fake:
        def run(self, *_a, **_k):
            return b"this is not raw diff output\0"

    with pytest.raises(mod.ScanError):
        mod.raw_changes(Fake(), "a", "b")


# --- 3. no value leaves through a file name ---------------------------------------------


def test_a_file_name_never_prints_a_matched_value(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    name = "joe.smith" + "@" + "acme-corp" + ".io" + "-" + DENY_WORD + ".txt"
    repo.commit("Add", **{name: FAKES["lan-ip"] + "\n"})
    summary = tmp_path / "summary.md"
    report = tmp_path / "report.txt"
    proc = run_scan(
        "--denylist-bundle",
        str(bundle(tmp_path, DENY_WORD)),
        *diff_args(repo, base),
        "--report",
        str(report),
        extra={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)},
        cwd=repo.path,
    )
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr + report.read_text() + summary.read_text()
    for value in ("joe.smith", "acme-corp", DENY_WORD):
        assert value not in combined.lower(), value
    assert "***" in combined


def test_an_annotation_cannot_be_injected_through_a_file_name(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.commit("Add", **{"a%b,c:d.txt": FAKES["lan-ip"] + "\n"})
    proc = run_scan(*diff_args(repo, base), extra={"GITHUB_ACTIONS": "true"}, cwd=repo.path)
    errors = [ln for ln in proc.stdout.splitlines() if ln.startswith("::error")]
    assert errors and all(ln.startswith("::error file=a%25b%2Cc%3Ad.txt,line=1,") for ln in errors)


def test_an_error_message_redacts_the_name_too(tmp_path: Path) -> None:
    name = tmp_path / ("joe.smith" + "@" + "acme-corp" + ".io.txt")
    proc = scan("text", str(name))
    assert proc.returncode == 2
    assert "joe.smith" not in proc.stderr


# --- 4. addresses followed by a slash or preceded by @ ------------------------------------


@pytest.mark.parametrize(
    ("text", "cls"),
    [
        (f"curl http://{LAN}/api/v1", "lan-ip"),
        (f"ssh admin@{LAN}", "lan-ip"),
        (f"https://{CGNAT}/x and user@{CGNAT}", "tailnet-ip"),
        (f"addr {LAN}/24 has host bits set", "lan-ip"),
        ("net 100.64.0.0/10 and 10.0.0.0/8 and 10.0.0.0/24", None),
        ("pkg@10.0.0 and v10.1.2.3 and 10.1.2.3.4", None),
    ],
)
def test_ip_edges(tmp_path: Path, text: str, cls: str | None) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    if cls is None:
        assert proc.returncode == 0, proc.stdout
    else:
        assert proc.returncode == 1 and f"{cls} " in proc.stdout


def test_an_ipv6_tailnet_address_with_a_path_is_caught(tmp_path: Path) -> None:
    text = "http://[fd7a:" + "115c:a1e0::1234]/x and fd7a:" + "115c:a1e0::abcd/api"
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert proc.returncode == 1 and "tailnet-ip cgnat-v6" in proc.stdout


def test_a_public_address_warns_without_failing(tmp_path: Path) -> None:
    text = "dns " + "8." + "8.4.4 and " + "203.0.113.9\n"
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text)))
    assert proc.returncode == 0
    assert proc.stdout.count("warning: ") == 1 and ("8." + "8.4.4") not in proc.stdout


# --- 5. secret shapes ----------------------------------------------------------------------


def secret_cases() -> list[tuple[str, str]]:
    tok = "A1b2C3d4E5f6G7h8I9j0K1l2"
    return [
        ("json-token", '{"to' + 'ken": "' + tok + '"}'),
        ("json-password", '{"pass' + 'word": "' + tok + 'xyz"}'),
        ("env-style", "SECRET" + "_KEY = '" + tok + "'"),
        ("bearer", "Authorization: Bea" + "rer " + tok + "mnop"),
        ("gitlab", "glp" + "at-" + tok),
        ("entra", "Ab1" + "8Q~" + "x" * 8 + "Y.z" * 8 + "~"),
        ("entra-key-line", "client_" + "secret = abC" + "8Q~" + "r5T" * 12),
    ]


@pytest.mark.parametrize(("name", "text"), secret_cases(), ids=[c[0] for c in secret_cases()])
def test_secret_shapes_the_first_round_missed(tmp_path: Path, name: str, text: str) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert proc.returncode == 1 and " secret " in proc.stdout, name
    no_value_echoed(proc, "A1b2C3d4E5f6G7h8I9j0K1l2")


@pytest.mark.parametrize(
    "text",
    [
        "token = self.token_value.strip()",
        'password = os.environ["PASSWORD_VARIABLE_12345"]',
        "token: ${{ secrets.EXAMPLE_TOKEN_NAME_LONG }}",
        '"azure-client-secret": "' + GUID_FAKE + '"',
        "SECRET_KEY = settings.SECRET_KEY_FOR_SIGNING",
    ],
)
def test_code_that_names_a_secret_is_not_one(tmp_path: Path, text: str) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert " secret " not in proc.stdout, text


def test_a_test_marker_value_is_skipped_only_in_test_files_and_only_for_assignments(
    tmp_path: Path,
) -> None:
    repo = Repo(tmp_path / "r")
    fake_assign = "to" + 'ken = "test-value-A1b2C3d4E5f6G7h8I9"'
    shape = "ghp" + "_" + "test" + "A1b2C3d4" * 5
    repo.commit(
        "Add",
        **{
            "tests__test_a.py": fake_assign + "\n",
            "src__a.py": fake_assign + "\n",
            "tests__test_b.py": shape + "\n",
        },
    )
    proc = scan(*NO_ALLOW, "tree", "--ref", "HEAD", cwd=repo.path)
    shown = hit_lines(proc)
    assert any(h.startswith("src/a.py:") for h in shown)
    assert not any(h.startswith("tests/test_a.py:") for h in shown)
    assert any(h.startswith("tests/test_b.py:") and "github-token" in h for h in shown), (
        "a production token shape is never skipped, whatever the file or the value says"
    )


# --- 6. commit identity ---------------------------------------------------------------------


def _identity_repo(tmp_path: Path, name: str, email: str) -> tuple[Repo, str]:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.git("config", "user.name", name)
    repo.git("config", "user.email", email)
    repo.commit("Add", b="fine\n")
    return repo, base


def test_a_personal_commit_address_is_a_hit_and_noreply_is_not(tmp_path: Path) -> None:
    personal = "joe.smith" + "@" + "acme-corp" + ".io"
    repo, base = _identity_repo(tmp_path, "Joe", personal)
    proc = scan(*diff_args(repo, base), cwd=repo.path)
    assert proc.returncode == 1
    assert "commit-identity email" in proc.stdout
    no_value_echoed(proc, "joe.smith", "acme-corp")
    ok_repo, ok_base = _identity_repo(
        tmp_path / "ok", "bot", "1234+bot" + "@users.noreply" + ".github.com"
    )
    assert scan(*diff_args(ok_repo, ok_base), cwd=ok_repo.path).returncode == 0


def test_no_identity_skips_the_author_check_but_not_the_lines(tmp_path: Path) -> None:
    # A push to private main (2026-10-29): a merge commit's author is the merger's GitHub
    # address, which failed every merge; the lines and messages are still scanned.
    personal = "joe.smith" + "@" + "acme-corp" + ".io"
    repo, base = _identity_repo(tmp_path, "Joe", personal)
    proc = scan(*diff_args(repo, base), "--no-identity", cwd=repo.path)
    assert proc.returncode == 0, proc.stdout
    leak_repo, leak_base = _identity_repo(tmp_path / "leak", "Joe", personal)
    leak_repo.commit("Add an address", c="host " + "192.168" + ".1.9\n")
    proc = scan(*diff_args(leak_repo, leak_base), "--no-identity", cwd=leak_repo.path)
    assert proc.returncode == 1 and "lan-ip" in proc.stdout
    assert "commit-identity" not in proc.stdout


def test_a_listed_identity_and_the_denylist_on_a_name(tmp_path: Path) -> None:
    personal = "joe.smith" + "@" + "acme-corp" + ".io"
    repo, base = _identity_repo(tmp_path, "Joe " + DENY_WORD, personal)
    allow = write(
        tmp_path / "allow.txt", f"commit-identity {digest(personal)} # a reviewed identity\n"
    )
    listed = scan(
        "--allowlist", str(allow), "diff", "--base", base, "--head", "HEAD", cwd=repo.path
    )
    assert listed.returncode == 0
    named = scan(
        "--allowlist",
        str(allow),
        "--denylist-bundle",
        str(bundle(tmp_path, DENY_WORD)),
        "diff",
        "--base",
        base,
        "--head",
        "HEAD",
        cwd=repo.path,
    )
    assert named.returncode == 1 and "denylist entry-1" in named.stdout
    no_value_echoed(named, DENY_WORD)


# --- 7. enforcement beyond the hook ----------------------------------------------------------


def _fork_repo(tmp_path: Path):
    """A clone holding a fake public fork's branches under refs/remotes/fork/."""
    fork = Repo(tmp_path / "forksrc")
    base = fork.commit("Upstream", a="x\n")
    fork.git("checkout", "-q", "-b", "ae/clean")
    fork.commit("Clean work", b="fine\n")
    fork.git("checkout", "-q", "-b", "ae/leaky", base)
    fork.commit("Leaky work", c=FAKES["tailnet-ip"] + "\n")
    local = Repo(tmp_path / "local")
    local.git("fetch", "-q", str(fork.path), "+refs/heads/*:refs/remotes/fork/*")
    return local, base


def test_the_fork_backstop_names_the_branch_and_keeps_no_state_on_a_hit(tmp_path: Path) -> None:
    local, base = _fork_repo(tmp_path)
    state = tmp_path / "state.txt"
    proc = scan(*NO_ALLOW, "fork", "--base", base, "--state", str(state), cwd=local.path)
    assert proc.returncode == 1
    assert "public fork branch to delete: ae/leaky" in proc.stdout
    assert "ae/clean" not in proc.stdout
    assert not state.exists(), "state advances only after a clean run"
    no_value_echoed(proc, "100." + "101.2.3")


def test_the_fork_backstop_scans_only_what_is_new_since_the_last_clean_run(
    tmp_path: Path,
) -> None:
    local, base = _fork_repo(tmp_path)
    local.git("update-ref", "-d", "refs/remotes/fork/ae/leaky")
    state = tmp_path / "state.txt"
    deny = ("--denylist-bundle", str(bundle(tmp_path, DENY_WORD)))
    first = scan(*NO_ALLOW, *deny, "fork", "--base", base, "--state", str(state), cwd=local.path)
    assert first.returncode == 0 and state.read_text().startswith("ae/clean ")
    # A leak inside commits the state already covers is not rescanned; a new commit is.
    clean_tip = local.git("rev-parse", "refs/remotes/fork/ae/clean")
    local.git("checkout", "-q", "--detach", clean_tip)
    new = local.commit("New", d=FAKES["lan-ip"] + "\n")
    local.git("update-ref", "refs/remotes/fork/ae/clean", new)
    second = scan(*NO_ALLOW, *deny, "fork", "--base", base, "--state", str(state), cwd=local.path)
    assert second.returncode == 1 and "ae/clean" in second.stdout


def test_the_fork_push_script_refuses_a_leaky_push_without_any_hook(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "fork-push.sh"
    assert script.exists() and os.access(script, os.X_OK)
    repo = _two_remotes(tmp_path)
    repo.commit("Base", a="x\n")
    repo.git("push", "-q", "fork", "main")
    repo.git("checkout", "-q", "-b", "ae/topic")
    repo.commit("Leak", b=FAKES["tailnet-ip"] + "\n")
    env = {**os.environ, "AE_LEAK_ALLOW_NO_DENYLIST": "1", "AE_FORK_PUSH_REMOTE": "fork"}
    blocked = subprocess.run(
        [str(script), "ae/topic"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert blocked.returncode != 0 and "tailnet-ip" in blocked.stdout
    pushed = subprocess.run(
        ["git", "-C", str(tmp_path / "fork.git"), "branch", "--list", "ae/topic"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pushed.stdout.strip() == "", "nothing reached the fork"
    repo.commit("Fix", b="fine\n")
    ok = subprocess.run(
        [str(script), "ae/topic"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert ok.returncode != 0, "the leaking first commit is still in the range"


def test_the_backstop_workflow_meets_the_standards() -> None:
    path = ROOT / ".github" / "workflows" / "fork-backstop.yml"
    wf = yaml.safe_load(path.read_text())
    on = wf.get("on", wf.get(True))
    assert on["schedule"][0]["cron"].split()[1] == "*", "hourly"
    assert wf["permissions"] == {"contents": "read"} and "concurrency" in wf
    job = next(iter(wf["jobs"].values()))
    assert job["timeout-minutes"] <= 20
    for step in job["steps"]:
        if "run" in step:
            assert step["run"].lstrip().startswith("set -euo pipefail")
            assert "${{" not in step["run"]
    assert (
        "cron" in path.read_text().split("schedule:")[1].split("\n")[1].lower()
        or "#" in path.read_text()
    )


def test_the_docs_own_up_to_the_no_verify_gap() -> None:
    text = (ROOT / "docs" / "LEAK-SCAN.md").read_text()
    assert "--no-verify" in text and "backstop" in text.lower()


# --- 8. the denylist configuration fails closed ---------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_a_present_secret_without_its_hash_is_a_failure(tmp_path: Path) -> None:
    f = write(tmp_path / "a.txt", "fine\n")
    proc = run_scan(
        "--denylist-b64-env", "DL", "text", str(f), extra={"DL": _b64(DENY_WORD + "\n")}
    )
    assert proc.returncode == 2 and "hash variable" in proc.stderr


def test_a_hash_mismatch_exits_two_not_one(tmp_path: Path) -> None:
    import hashlib

    f = write(tmp_path / "a.txt", f"{DENY_WORD}\n")
    good = _b64(DENY_WORD + "\n")
    bad = "0" * 64
    proc = run_scan(
        "--denylist-b64-env", "DL", "--denylist-sha256", bad, "text", str(f), extra={"DL": good}
    )
    assert proc.returncode == 2 and "sha256" in proc.stderr
    right = hashlib.sha256((DENY_WORD + "\n").encode()).hexdigest()
    ok = run_scan(
        "--denylist-b64-env",
        "DL",
        "--denylist-sha256-env",
        "DLH",
        "text",
        str(f),
        extra={"DL": good, "DLH": right},
    )
    assert ok.returncode == 1 and "denylist entry-1" in ok.stdout


def test_a_missing_secret_fails_unless_told_to_warn(tmp_path: Path) -> None:
    f = write(tmp_path / "a.txt", "fine\n")
    fail = run_scan("--denylist-b64-env", "DL", "text", str(f))
    assert fail.returncode == 2
    warn = run_scan("--denylist-b64-env", "DL", "--denylist-missing", "warn", "text", str(f))
    assert warn.returncode == 0 and "NOT applied" in warn.stderr


def test_a_bundle_file_is_checked_against_a_given_hash(tmp_path: Path) -> None:
    f = write(tmp_path / "a.txt", "fine\n")
    proc = scan(
        "--denylist-bundle",
        str(bundle(tmp_path, DENY_WORD)),
        "--denylist-sha256",
        "1" * 64,
        "text",
        str(f),
    )
    assert proc.returncode == 2


def test_the_leaks_job_keys_on_the_repository_id_and_scans_pushes() -> None:
    wf = yaml.safe_load(CI.read_text())
    env = {
        k: str(v) for s in wf["jobs"]["leaks"]["steps"] if "env" in s for k, v in s["env"].items()
    }
    assert "github.repository_id" in env["PRIVATE_REPO"]
    run = "\n".join(s["run"] for s in wf["jobs"]["leaks"]["steps"] if "run" in s)
    assert "BEFORE_SHA" in run and "diff" in run, "commit messages on a push to main"


LEAK_PR = ROOT / ".github" / "workflows" / "leak-pr.yml"


def test_the_pr_scan_workflow_meets_the_standards_and_the_ruleset_name() -> None:
    wf = yaml.safe_load(LEAK_PR.read_text())
    on = wf.get("on", wf.get(True))
    assert on["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "edited",
        "labeled",
        "unlabeled",
        "ready_for_review",
    ]
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert "concurrency" in wf
    job = wf["jobs"]["leaks"]
    assert job["name"] == "leaks", "the ruleset requires this check name"
    assert job["timeout-minutes"] <= 40
    for step in job["steps"]:
        if "run" in step:
            assert step["run"].lstrip().startswith("set -euo pipefail")
            assert "${{" not in step["run"], "values reach run bodies through env:"
    assert "edited" not in CI.read_text().split("pull_request:")[1].split("push:")[0]


def test_every_workflow_fails_on_a_missing_denylist() -> None:
    """The secret is set (2026-10-29), so a missing one is an error, never a warning."""
    for name in ("ci.yml", "leak-pr.yml", "fork-backstop.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert "--denylist-missing fail" in text, name
        assert "--denylist-missing warn" not in text, name


def _pr_steps() -> dict[str, dict]:
    steps = yaml.safe_load(LEAK_PR.read_text())["jobs"]["leaks"]["steps"]
    return {s["name"]: s for s in steps}


def test_the_pr_scan_reads_changed_images_with_the_base_scanner() -> None:
    steps = _pr_steps()
    listing = steps["List the images the pull request adds or changes"]
    install = steps["Install tesseract"]
    ocr = steps["Read the text in the changed images"]
    guard = "steps.images.outputs.any == 'true'"
    assert install["if"] == guard and ocr["if"] == guard, "no image changed: no install"
    assert "tesseract-ocr" in install["run"] and install["timeout-minutes"] <= 10
    assert re.search(r"\bwebp\b", install["run"]), "anim_dump comes from the webp package"
    assert ocr["timeout-minutes"] == 25
    seconds = float(ocr["env"]["AE_LEAK_SCAN_SECONDS"])
    assert 120 < seconds < 25 * 60, "the scanner deadline outlasts the default, not the step"
    job_minutes = yaml.safe_load(LEAK_PR.read_text())["jobs"]["leaks"]["timeout-minutes"]
    assert job_minutes >= install["timeout-minutes"] + ocr["timeout-minutes"]
    body = ocr["run"]
    assert "base-scanner/scripts/leak-scan.py" in body, (
        "the base commit's scanner reads the pixels"
    )
    assert "${PRIVATE_REPO:?" in body and body.index("${PRIVATE_REPO:?") < body.index(
        '"$PRIVATE_REPO" = "true"'
    ), "an unset flag stops the job instead of running the generic classes"
    assert "media --ocr require -- " in body, "a name that starts with a dash is not an option"
    assert "repository_id" not in str(ocr), "the repository id is computed once, in the scan step"
    assert "--denylist-b64-env DENYLIST_B64" in body and "--denylist-missing fail" in body
    assert "base-scanner/scripts/leak-allowlist.txt" in body, "the base allowlist is in force"
    for step in (listing, install, ocr):
        assert step["run"].lstrip().startswith("set -euo pipefail")
        assert "${{" not in step["run"], "values reach run bodies through env:"
    assert "secrets.AE_LEAK_DENYLIST_B64" in ocr["env"]["DENYLIST_B64"]
    assert "--diff-filter=d" in listing["run"], "deleted files are skipped"


def test_the_image_listing_picks_changed_images_only(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", **{"keep.png": "x", "gone.png": "x", "doc.txt": "a"})
    (repo.path / "gone.png").unlink()
    (repo.path / "doc.txt").write_text("b")
    (repo.path / "new.PNG").write_text("y")
    (repo.path / "with space.jpeg").write_text("z")
    (repo.path / "-dash.png").write_text("d")
    (repo.path / "new\nline.gif").write_text("l")
    (repo.path / "phone.heic").write_text("h")
    (repo.path / "notes.md").write_text("n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "Change things")
    head = repo.git("rev-parse", "HEAD")
    run = _pr_steps()["List the images the pull request adds or changes"]["run"]
    out = tmp_path / "out"
    proc = subprocess.run(
        ["bash", "-c", run],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(out),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().strip() == "any=true"
    listed = (tmp_path / "changed-images.txt").read_bytes().split(b"\0")[:-1]
    assert sorted(listed) == sorted(
        [b"new.PNG", b"with space.jpeg", b"-dash.png", b"new\nline.gif"]
    ), "heic, heif and avif are left to the metadata scan"
    # A change with no image sets any=false.
    out.write_text("")
    env = {
        **os.environ,
        "BASE_SHA": head,
        "HEAD_SHA": head,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
    }
    proc = subprocess.run(
        ["bash", "-c", run], cwd=repo.path, capture_output=True, text=True, check=False, env=env
    )
    assert proc.returncode == 0 and out.read_text().strip() == "any=false"


def test_codeowners_gives_the_scanner_paths_to_the_approver_team() -> None:
    lines = [
        ln.split()
        for ln in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    owned = {path: owners for path, *owners in lines}
    assert "*" not in owned, "a catch-all owner would need a review the box account cannot give"
    assert owned == {
        path: ["@analyticendeavors/leak-approvers"]
        for path in (
            "/.github/workflows/",
            "/.github/CODEOWNERS",
            "/scripts/leak-scan.py",
            "/scripts/leak-allowlist.txt",
            "/scripts/leak-fork-baseline.txt",
        )
    }


def _hook_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        **extra,
    }
    for key in ("AE_LEAK_DENYLIST_FILE", "AE_LEAK_ALLOW_NO_DENYLIST", "AE_LEAK_DENYLIST_SHA256"):
        if key not in extra:
            env.pop(key, None)
    return env


def _hook_push(tmp_path: Path, env: dict[str, str]):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = Repo(tmp_path / "r")
    repo.git("config", "core.hooksPath", str(HOOK.parent))
    repo.git("remote", "add", "origin", str(remote))
    repo.commit("Base", a="x\n")
    return subprocess.run(
        ["git", "-C", str(repo.path), "push", "origin", "main"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_the_hook_refuses_without_a_denylist_unless_allowed(tmp_path: Path) -> None:
    blocked = _hook_push(tmp_path / "a", _hook_env(tmp_path))
    assert blocked.returncode != 0 and "AE_LEAK_ALLOW_NO_DENYLIST" in blocked.stderr
    allowed = _hook_push(tmp_path / "b", _hook_env(tmp_path, AE_LEAK_ALLOW_NO_DENYLIST="1"))
    assert allowed.returncode == 0, allowed.stderr


def test_the_hook_checks_the_bundle_against_its_pinned_hash(tmp_path: Path) -> None:
    import hashlib

    bundle_file = bundle(tmp_path, DENY_WORD)
    good = hashlib.sha256(bundle_file.read_bytes()).hexdigest()
    env = _hook_env(tmp_path, AE_LEAK_DENYLIST_FILE=str(bundle_file))
    bad = _hook_push(tmp_path / "a", {**env, "AE_LEAK_DENYLIST_SHA256": "0" * 64})
    assert bad.returncode != 0 and "sha256" in bad.stderr
    ok = _hook_push(tmp_path / "b", {**env, "AE_LEAK_DENYLIST_SHA256": good})
    assert ok.returncode == 0, ok.stderr


# --- 9. binary and encoded content ------------------------------------------------------------


def test_a_new_binary_file_is_unreviewed_unless_safe_or_allowlisted_by_exact_path(
    tmp_path: Path,
) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02" * 50)
    (repo.path / "font.woff2").write_bytes(b"\x00\x01\x02" * 50)
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "Add binaries")
    proc = scan(*diff_args(repo, base), cwd=repo.path)
    shown = hit_lines(proc)
    assert any(h.startswith("blob.bin@") and "binary unreviewed" in h for h in shown)
    assert not any(h.startswith("font.woff2") for h in shown)
    allow = write(tmp_path / "allow.txt", "binary blob.bin # a reviewed fixture\n")
    ok = scan("--allowlist", str(allow), "diff", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert ok.returncode == 0
    wide = write(tmp_path / "wide.txt", "binary *.bin # too wide\n")
    bad = scan("--allowlist", str(wide), "diff", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert bad.returncode == 2


def test_utf16_text_is_decoded_before_it_is_scanned(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    (repo.path / "u.txt").write_bytes(("v " + LAN + "\n").encode("utf-16"))
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "Add")
    proc = scan(*NO_ALLOW, "tree", "--ref", "HEAD", cwd=repo.path)
    assert proc.returncode == 1 and "lan-ip" in proc.stdout


def _gif(*, comment: bool = False, trailing: bool = False) -> bytes:
    out = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    if comment:
        out += b"\x21\xfe\x05hello\x00"
    out += b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x44\x01\x00"
    out += b"\x3b"
    return out + (b"junk" if trailing else b"")


def _tiff(tag: int) -> bytes:
    return b"II*\x00" + struct.pack("<IH", 8, 1) + struct.pack("<HHII", tag, 2, 1, 0) + b"\0\0\0\0"


def _media_rules(tmp_path: Path, name: str, data: bytes) -> list[str]:
    path = tmp_path / name
    path.write_bytes(data)
    proc = scan(*NO_ALLOW, "media", "--ocr", "off", str(path))
    return sorted(h.rsplit(" ", 1)[1] for h in hit_lines(proc))


def test_more_image_formats_and_trailing_bytes(tmp_path: Path) -> None:
    assert _media_rules(tmp_path, "a.gif", _gif()) == []
    assert _media_rules(tmp_path, "b.gif", _gif(comment=True)) == ["gif-comment"]
    assert _media_rules(tmp_path, "c.gif", _gif(trailing=True)) == ["gif-trailing-data"]
    assert _media_rules(tmp_path, "d.tiff", _tiff(0x013B)) == ["tiff-artist"]
    assert _media_rules(tmp_path, "e.tiff", _tiff(0x0100)) == []
    exif = b"\x00\x00\x00\x18ftypheic" + b"\0" * 12 + b"Exif\x00\x00" + b"data"
    assert _media_rules(tmp_path, "f.heic", exif) == ["heif-exif"]
    assert _media_rules(tmp_path, "g.avif", b"\x00\x00\x00\x18ftypavif" + b"\0" * 12) == []
    assert _media_rules(tmp_path, "h.png", png() + b"tail") == ["png-trailing-data"]
    assert _media_rules(tmp_path, "i.png", png((b"iCCP", b"name\0\0data"))) == ["png-iCCP"]
    assert _media_rules(tmp_path, "j.png", png((b"zzZz", b"x"))) == ["png-unknown-chunk"]
    plain = jpeg((0xE0, b"JFIF\0"))
    assert _media_rules(tmp_path, "k.jpg", plain + b"tail") == ["jpeg-trailing-data"]
    assert _media_rules(tmp_path, "l.jpg", jpeg((0xE2, b"ICC_PROFILE\0x"))) == ["jpeg-app2"]
    assert _media_rules(tmp_path, "m.jpg", jpeg((0xE1, b"other\0"))) == []
    assert _media_rules(tmp_path, "n.jpg", jpeg((0xED, b"Photoshop 3.0\0"))) == ["jpeg-iptc"]


def test_pdf_info_and_xmp_are_read(tmp_path: Path) -> None:
    pdf = (
        b"%PDF-1.4\n1 0 obj<</Author (" + b"Joe" + b") /Producer (tool 1.0)>>endobj\n<x:xmpmeta/>"
    )
    rules = _media_rules(tmp_path, "a.pdf", pdf)
    assert rules == ["pdf-author", "pdf-producer", "pdf-xmp"]
    assert _media_rules(tmp_path, "b.pdf", b"%PDF-1.4\n1 0 obj<<>>endobj\n") == []


def test_a_media_hit_can_be_keyed_on_its_content_hash(tmp_path: Path) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(jpeg((0xE2, b"ICC_PROFILE\0same")))
    hashed = scan(*NO_ALLOW, "media", "--ocr", "off", "--hash-hits", str(path))
    digest = re.search(r"sha256:([0-9a-f]{64})", hashed.stdout)
    assert digest
    allow = write(
        tmp_path / "allow.txt", f"media-metadata sha256:{digest.group(1)} # a known profile\n"
    )
    assert scan("--allowlist", str(allow), "media", "--ocr", "off", str(path)).returncode == 0
    path.write_bytes(jpeg((0xE2, b"ICC_PROFILE\0different")))
    assert scan("--allowlist", str(allow), "media", "--ocr", "off", str(path)).returncode == 1


# --- 10. CI trust boundary --------------------------------------------------------------------


def test_the_leaks_job_runs_the_base_scanner_when_the_base_has_one() -> None:
    wf = yaml.safe_load(LEAK_PR.read_text())
    steps = wf["jobs"]["leaks"]["steps"]
    uses = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert len(uses) == 2, "head, and the base commit into a second directory"
    base = next(s for s in uses if s["with"].get("path"))
    assert "base.sha" in base["with"]["ref"] and base["with"]["path"] == "base-scanner"
    run = "\n".join(s["run"] for s in steps if "run" in s)
    assert "base-scanner/scripts/leak-scan.py" in run and "-f " in run
    assert "--title-env PR_TITLE --body-env PR_BODY" in run


def test_an_allowlist_change_is_noticed_without_its_pattern(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit(
        "Base", **{"scripts__leak-allowlist.txt": "# none\n", "scripts__leak-scan.py": "# stub\n"}
    )
    base = repo.git("rev-parse", "HEAD")
    secretish = "joe.smith" + "@" + "acme-corp" + ".io"
    repo.commit(
        "Allow it",
        **{"scripts__leak-allowlist.txt": f"email {digest(secretish)} a/* # reviewed\n"},
    )
    proc = scan("diff", "--allowlist-approved", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert "notice: allowlist entry added or changed: class email, path a/*" in proc.stdout
    no_value_echoed(proc, "joe.smith", "acme-corp")


# --- 11. no catch-all allowlist entries ---------------------------------------------------------


@pytest.mark.parametrize("pattern", ["/./", "/.*/", "/.+/", "/[a-z]*/", "/\\S/"])
def test_a_catch_all_regex_is_refused(tmp_path: Path, pattern: str) -> None:
    allow = write(tmp_path / "allow.txt", f"private-repo-link {pattern} a/* # too wide\n")
    proc = scan("--allowlist", str(allow), "text", str(write(tmp_path / "a.txt", "x\n")))
    assert proc.returncode == 2 and "matches almost anything" in proc.stderr


def test_a_hash_entry_clears_exactly_one_value(tmp_path: Path) -> None:
    text = write(tmp_path / "a.txt", FAKES["email"] + "\nand zed@" + "acme-corp" + ".io\n")
    hashed = scan(*NO_ALLOW, "text", "--hash-hits", str(text))
    digests = re.findall(r"email address sha256:([0-9a-f]{64})", hashed.stdout)
    assert len(digests) == 2
    allow = write(tmp_path / "allow.txt", f"email sha256:{digests[0]} # a reviewed fixture\n")
    proc = scan("--allowlist", str(allow), "text", str(text))
    assert proc.returncode == 1 and len(hit_lines(proc)) == 1


def test_the_real_allowlist_parses_and_has_no_catch_alls() -> None:
    mod = load_scanner()
    entries = mod.parse_allowlist((ROOT / "scripts" / "leak-allowlist.txt").read_text())
    assert entries
    for entry in entries:
        if entry.regex is not None:
            assert not entry.regex.search("a")


# --- 12. cloud ids ---------------------------------------------------------------------------------


def test_a_bare_guid_is_a_cloud_id_outside_generated_files(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit(
        "Add",
        **{
            "src__a.py": 'APP = "' + GUID_FAKE + '"\n',
            "src__ids.json": '{"x": "' + "6fa459ee8a3ca4894edb77e160355e12" + '"}\n',
            "src__b.py": 'TENANT = "' + "6fa459ee8a3ca4894" + "edb77e160355e12" + '"\n',
            "uv.lock": 'id = "' + GUID_FAKE + '"\n',
            "web__dist__app.js": 'x="' + GUID_FAKE + '"\n',
            "src__ok.py": 'ID = "00000000-0000-0000-0000-000000000001"\n',
        },
    )
    proc = scan(*NO_ALLOW, "tree", "--ref", "HEAD", cwd=repo.path)
    shown = hit_lines(proc)
    assert any(h.startswith("src/a.py:") and "cloud-id guid" in h for h in shown)
    assert any(h.startswith("src/b.py:") and "cloud-id hex32" in h for h in shown)
    assert not any(
        h.startswith(("src/ids.json", "uv.lock", "web/dist", "src/ok.py")) for h in shown
    )


# --- 13. PR text and direct pushes ---------------------------------------------------------------------


def test_the_pr_title_and_body_are_scanned_from_the_environment(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.commit("Add", b="fine\n")
    proc = run_scan(
        *diff_args(repo, base, "HEAD", "--title-env", "T", "--body-env", "B"),
        extra={"T": "fine title", "B": "body with " + LAN},
        cwd=repo.path,
    )
    assert proc.returncode == 1 and "pr-body:1: lan-ip" in proc.stdout


def test_commit_messages_of_a_push_range_are_scanned(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.commit("Mention " + LAN, b="fine\n")
    proc = scan(*diff_args(repo, base), cwd=repo.path)
    assert proc.returncode == 1 and re.search(r"commit [0-9a-f]{10}:1: lan-ip", proc.stdout)


# --- 14. placeholders must be the host or the tailnet label ----------------------------------------


@pytest.mark.parametrize(
    ("host", "hit"),
    [
        ("box." + "realnet" + ".ts.net", True),
        ("srv." + "tailnet" + ".ts.net", False),
        ("host." + "example" + ".ts.net", False),
        ("contoso" + KV_SUFFIX, False),
        ("box." + "realtenant" + TENANT_SUFFIX, True),
        ("your-vault" + KV_SUFFIX, False),
        ("real" + "vault" + KV_SUFFIX, True),
    ],
)
def test_the_placeholder_must_be_the_host_label(tmp_path: Path, host: str, hit: bool) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", f"see {host}\n")))
    assert (proc.returncode == 1) is hit, host


# --- 15. home path forms -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "/c/Us" + "ers/zed42/x",
        "/mnt/c/Us" + "ers/zed42",
        "C--Us" + "ers-zed42-repos",
        "-ho" + "me-zed42-repos",
    ],
)
def test_more_home_path_forms(tmp_path: Path, text: str) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert proc.returncode == 1 and "home-path" in proc.stdout, text
    no_value_echoed(proc, "zed42")


# --- 16. repository links anywhere ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "uses: analytic" + "endeavors/secret-repo-x/.github/workflows/a.yml@main",
        "repository: analytic" + "endeavors/secret-repo-x",
        "https://api.github.com/repos/analytic" + "endeavors/secret-repo-x/pulls",
        "git clone git@github.com:analytic" + "endeavors/secret-repo-x.git",
    ],
)
def test_a_repository_of_the_organisation_is_a_hit_unless_listed(
    tmp_path: Path, text: str
) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert proc.returncode == 1 and "private-repo-link" in proc.stdout, text
    allow = write(
        tmp_path / "allow.txt",
        "private-repo-link analytic" + "endeavors/secret-repo-x # fixture\n",
    )
    if "personal" not in text:
        assert (
            scan(
                "--allowlist", str(allow), "text", str(write(tmp_path / "a.txt", text + "\n"))
            ).returncode
            == 0
        )


def test_the_public_repositories_pass_with_the_shipped_allowlist(tmp_path: Path) -> None:
    text = (
        "https://github.com/analytic"
        + "endeavors/omnigent-ae/pull/1 and analytic"
        + "endeavors/omnigent\n"
    )
    proc = scan("text", str(write(tmp_path / "a.txt", text)))
    assert proc.returncode == 0, proc.stdout


# --- 17. local names ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["nas01.lo" + "cal", "printer.l" + "an", "db01.inter" + "nal"])
def test_local_host_names_are_hits(tmp_path: Path, host: str) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", f"open {host}:8080\n")))
    assert proc.returncode == 1 and "internal-host" in proc.stdout


@pytest.mark.parametrize(
    "text",
    [
        "host.docker." + "internal:11434",
        "compose.lo" + "cal.yml and config/compose.lo" + "cal.yml.example",
        "from pkg.artifact_store.lo" + "cal import X",
        "self.lo" + "cal = 1 and ErrorCode.INTER" + "NAL",
        "ae-x@member.service and a@b.timer",
    ],
)
def test_code_and_file_names_are_not_local_hosts(tmp_path: Path, text: str) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert proc.returncode == 0, proc.stdout


# --- low items ------------------------------------------------------------------------------------------------


def test_long_dotted_and_hyphenated_runs_scan_in_linear_time(tmp_path: Path) -> None:
    import time

    body = ("a-" * 20000) + "\n" + ("a." * 20000) + "\n" + ("-" * 40000) + "\n"
    path = write(tmp_path / "a.txt", body)
    start = time.monotonic()
    proc = scan(*NO_ALLOW, "text", str(path))
    assert proc.returncode == 0 and time.monotonic() - start < 10


def test_the_whole_scan_has_a_time_limit_and_exits_two(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "fine\n")
    proc = run_scan(
        *NO_ALLOW, "tree", "--ref", "HEAD", extra={"AE_LEAK_SCAN_SECONDS": "-1"}, cwd=ROOT
    )
    assert proc.returncode == 2 and "limit" in proc.stderr
    assert path.exists()


def test_a_marker_on_an_unchanged_line_does_not_clear_a_new_line(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    marker_line = "# " + marker("lan-ip") + "\n"
    base = repo.commit("Base", a=marker_line + "next line\n")
    repo.commit("Add", a=marker_line + LAN + "\n")  # new line directly under the old marker
    proc = scan(*diff_args(repo, base), cwd=repo.path)
    assert proc.returncode == 1 and "lan-ip" in proc.stdout
    ok_base = repo.commit("Reset", a="plain\n")
    repo.commit("Add with its own marker", a="plain\n# " + marker("lan-ip") + "\n" + LAN + "\n")
    assert scan(*diff_args(repo, ok_base), cwd=repo.path).returncode == 0


def test_tag_messages_and_ref_names_are_scanned_on_push(tmp_path: Path) -> None:
    repo = _two_remotes(tmp_path)
    sha = repo.commit("Base", a="x\n")
    repo.git("tag", "-a", "v1", "-m", "release for " + LAN)
    tag = repo.git("rev-parse", "v1")
    tag_push = scan(
        *NO_ALLOW, "push", "fork", cwd=repo.path, stdin=f"refs/tags/v1 {tag} refs/tags/v1 {ZERO}\n"
    )
    assert tag_push.returncode == 1 and re.search(r"tag [0-9a-f]{10}:\d+: lan-ip", tag_push.stdout)
    named = scan(
        "--denylist-bundle",
        str(bundle(tmp_path, DENY_WORD)),
        *NO_ALLOW,
        "push",
        "fork",
        cwd=repo.path,
        stdin=f"refs/heads/main {sha} refs/heads/{DENY_WORD}-x {ZERO}\n",
    )
    assert named.returncode == 1 and "branch or tag name:1: denylist" in named.stdout
    no_value_echoed(named, DENY_WORD)


def test_a_patch_body_with_a_dash_line_stays_in_the_header() -> None:
    mod = load_scanner()
    text = (
        "From " + "0" * 40 + " Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] x\n\nbody line one\n---\nbody line two after a dash line\n---\n"
        " a | 2 +-\n\ndiff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
    )
    modes, _files = mod.patch_modes(text.split("\n"))
    lines = text.split("\n")
    assert modes[lines.index("body line two after a dash line")] == "full"
    assert modes[lines.index("+new")] == "full" and modes[lines.index("-old")] == "deny"


def test_the_allowlist_file_is_scanned_for_pasted_values(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    pasted = "joe.smith" + "@" + "acme-corp" + ".io"
    repo.commit(
        "Add",
        **{
            "scripts__leak-allowlist.txt": f"email sha256:{'a' * 64} a/* # contact {pasted} here\n"
        },
    )
    proc = scan("tree", "--ref", "HEAD", cwd=repo.path)
    assert proc.returncode == 1 and "email address" in proc.stdout
    own = Repo(tmp_path / "own")
    own.commit(
        "Add",
        **{"scripts__leak-allowlist.txt": f"email {digest(pasted)} a/* # a reviewed fixture\n"},
    )
    assert scan("tree", "--ref", "HEAD", cwd=own.path).returncode == 0, (
        "its own entry field is exempt"
    )


# ======================================================================================
# Round 2 review fixes (2026-10-28, second review). Each test fails without its fix.
# ======================================================================================

SCAN_STUB = {"scripts__leak-scan.py": "# stub\n"}


def _pr_with_allowlisted_leak(tmp_path: Path, **base_files: str):
    """A base that has a scanner and an empty allowlist, and a PR that adds a LAN address
    together with the allowlist entry that would clear it."""
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", **{"scripts__leak-allowlist.txt": "# none\n"}, **SCAN_STUB)
    repo.commit(
        "Leak and allow it",
        leak=FAKES["lan-ip"] + "\n",
        **{"scripts__leak-allowlist.txt": f"lan-ip {digest(LAN)} # a made-up fixture\n"},
    )
    return repo, base


# --- M1. a PR cannot allowlist its own leak ----------------------------------------------


def test_a_pr_that_allowlists_its_own_leak_still_fails_and_names_the_entry(tmp_path: Path) -> None:
    repo, base = _pr_with_allowlisted_leak(tmp_path)
    proc = scan("diff", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert proc.returncode == 1
    assert "lan-ip rfc1918" in proc.stdout, "the base's allowlist is in force, so the leak stands"
    assert "allowlist unapproved entry for class lan-ip" in proc.stdout
    no_value_echoed(proc, LAN, digest(LAN).split(":")[1])


def test_an_approved_allowlist_change_takes_effect_and_is_noticed(tmp_path: Path) -> None:
    repo, base = _pr_with_allowlisted_leak(tmp_path)
    proc = scan("diff", "--allowlist-approved", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert proc.returncode == 0, proc.stdout
    assert "allowlist entry added or changed: class lan-ip, path * (approved by label)" in (
        proc.stdout
    )


def test_a_changed_allowlist_line_counts_as_a_new_entry(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    keep = f"email {digest('a' + '@' + 'b-corp.io')} # fixture\n"
    base = repo.commit("Base", **{"scripts__leak-allowlist.txt": keep}, **SCAN_STUB)
    repo.commit("Widen the path", **{"scripts__leak-allowlist.txt": keep.replace(" #", " * #")})
    proc = scan("diff", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert proc.returncode == 1 and "allowlist unapproved entry for class email" in proc.stdout


def test_a_base_without_a_scanner_uses_the_heads_allowlist(tmp_path: Path) -> None:
    """The PR that introduces the scanner has no base allowlist to read."""
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", a="x\n")
    repo.commit(
        "Add",
        leak=FAKES["lan-ip"] + "\n",
        **{"scripts__leak-allowlist.txt": f"lan-ip {digest(LAN)} # a made-up fixture\n"},
    )
    assert scan("diff", "--base", base, "--head", "HEAD", cwd=repo.path).returncode == 0


def test_a_push_to_main_trusts_the_merged_allowlist(tmp_path: Path) -> None:
    repo, base = _pr_with_allowlisted_leak(tmp_path)
    proc = scan(
        "diff", "--allowlist-from", "head", "--base", base, "--head", "HEAD", cwd=repo.path
    )
    assert proc.returncode == 0, proc.stdout


def test_a_head_allowlist_with_a_wildcard_fails_the_scan_even_unapproved(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", **{"scripts__leak-allowlist.txt": "# none\n"}, **SCAN_STUB)
    repo.commit("Wide", **{"scripts__leak-allowlist.txt": "lan-ip * # everything\n"})
    proc = scan("diff", "--base", base, "--head", "HEAD", cwd=repo.path)
    assert proc.returncode == 2 and "sha256" in proc.stderr


def test_the_pr_workflow_gates_approval_on_listed_ids_and_voids_it_on_a_push() -> None:
    text = LEAK_PR.read_text()
    wf = yaml.safe_load(text)
    steps = {s.get("id"): s for s in wf["jobs"]["leaks"]["steps"]}
    env = steps["approval"]["env"]
    assert "vars.AE_LEAK_ALLOWLIST_APPROVERS" in env["APPROVERS"]
    assert env["LABEL"] == "leak-allowlist-approved"
    assert "github.event.before" in env["BEFORE_SHA"]
    run = steps["approval"]["run"]
    assert '"$ids" != ",,"' in run, "an unset or empty variable approves nobody"
    assert "events" in run and "labeled" in run, "the latest applier is read from the issue events"
    assert "leak-allowlist.txt" in run and "-X DELETE" in run, "a push that changes it voids it"
    scan_run = next(
        s["run"] for s in wf["jobs"]["leaks"]["steps"] if s.get("name", "").startswith("Scan the")
    )
    assert "--allowlist-approved" in scan_run
    group = wf["concurrency"]["group"]
    assert "synchronize" in group, "a push's run cannot be cancelled by another event"
    assert "synchronize" in str(wf["concurrency"]["cancel-in-progress"])


# --- M2. wildcard allowlist entries ------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "lan-ip * # everything",
        "email /^10\\./ # a regex on a hash-only class",
        "tailnet-host *.ts.net # all of them",
        "cloud-id " + GUID_FAKE + " # a literal on a hash-only class",
        "home-path zed # a literal on a hash-only class",
        "azure-endpoint *" + KV_SUFFIX + " # wildcard",
        "internal-host ?*? # only wildcards",
        "private-repo-link ab* # fewer than 4 real characters",
        "binary-less-class-check x",  # malformed on purpose: no reason, no class
    ],
)
def test_wildcard_and_literal_entries_are_refused_where_they_could_clear_a_class(
    tmp_path: Path, line: str
) -> None:
    allow = write(tmp_path / "allow.txt", line + "\n")
    proc = scan("--allowlist", str(allow), "text", str(write(tmp_path / "a.txt", "x\n")))
    assert proc.returncode == 2, proc.stderr


def test_a_sha256_entry_and_a_specific_literal_still_work(tmp_path: Path) -> None:
    text = write(tmp_path / "a.txt", FAKES["lan-ip"] + "\n" + FAKES["private-repo-link"] + "\n")
    link = "analytic" + "endeavors/" + "secret-repo-x"
    allow = write(
        tmp_path / "allow.txt",
        f"lan-ip {digest(LAN)} # a made-up fixture\nprivate-repo-link {link} # a made-up repo\n",
    )
    assert scan("--allowlist", str(allow), "text", str(text)).returncode == 0


# --- M3. the backstop keeps no state without a denylist ------------------------------------


def test_the_fork_backstop_writes_no_state_when_no_denylist_was_loaded(tmp_path: Path) -> None:
    local, base = _fork_repo(tmp_path)
    local.git("update-ref", "-d", "refs/remotes/fork/ae/leaky")
    state = tmp_path / "state.txt"
    proc = scan(*NO_ALLOW, "fork", "--base", base, "--state", str(state), cwd=local.path)
    assert proc.returncode == 0 and not state.exists()
    assert "no state was written" in proc.stdout
    deny = ("--denylist-bundle", str(bundle(tmp_path, DENY_WORD)))
    again = scan(*NO_ALLOW, *deny, "fork", "--base", base, "--state", str(state), cwd=local.path)
    assert again.returncode == 0 and state.exists()


# --- L5. the secret assignment pattern is linear -------------------------------------------


def test_repeated_keywords_scan_in_linear_time(tmp_path: Path) -> None:
    import time

    path = write(tmp_path / "a.txt", "secret" * 20000 + "\n")  # 120 KB on one line
    start = time.monotonic()
    proc = scan(*NO_ALLOW, "text", str(path))
    assert proc.returncode == 0 and time.monotonic() - start < 8, "was quadratic (23 s at 100 KB)"


def test_the_scan_limit_is_checked_between_lines(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "\n".join(["token = x"] * 50000) + "\n")
    proc = run_scan(*NO_ALLOW, "text", str(path), extra={"AE_LEAK_SCAN_SECONDS": "0.001"})
    assert proc.returncode == 2 and "limit" in proc.stderr


# --- L6. addresses the CIDR and version skips used to hide -----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"net {LAN}/24",  # host bits set: a host, not a range
        f"http://{LAN}/80",  # not a prefix length
        f"host=={LAN}",
        f"x~{LAN}",
        f"needs >={LAN}",
        f"needs <={LAN}",
    ],
)
def test_a_cidr_or_version_look_alike_no_longer_hides_a_host_address(
    tmp_path: Path, text: str
) -> None:
    proc = scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text + "\n")))
    assert proc.returncode == 1 and "lan-ip " in proc.stdout, text


def test_a_real_range_and_a_version_still_pass(tmp_path: Path) -> None:
    text = "net 10.0.0.0/8 and 192.168.0.0/16 and v10.1.2.3 and ^10.1.2.3 and 10.1.2.3.4\n"
    assert scan(*NO_ALLOW, "text", str(write(tmp_path / "a.txt", text))).returncode == 0


# --- L8. git@ in an identity ------------------------------------------------------------------


def test_a_git_at_corporate_author_is_a_commit_identity_hit(tmp_path: Path) -> None:
    repo, base = _identity_repo(tmp_path, "ci", "git@" + "acme-corp" + ".io")
    proc = scan(*diff_args(repo, base), cwd=repo.path)
    assert proc.returncode == 1 and "commit-identity email" in proc.stdout
    forge, forge_base = _identity_repo(tmp_path / "ok", "ci", "git@" + "github" + ".com")
    assert scan(*diff_args(forge, forge_base), cwd=forge.path).returncode == 0


def test_a_git_at_clone_url_in_text_is_still_not_an_address(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "git clone git@" + "acme-corp" + ".io:team/repo.git\n")
    assert scan(*NO_ALLOW, "text", str(path)).returncode == 0


# --- L9. fork-push refuses options that push unscanned refs ----------------------------------


@pytest.mark.parametrize("option", ["--tags", "--follow-tags", "--all", "--mirror", "--delete"])
def test_fork_push_refuses_every_option_but_the_force_ones(tmp_path: Path, option: str) -> None:
    script = ROOT / "scripts" / "fork-push.sh"
    repo = _two_remotes(tmp_path)
    repo.commit("Base", a="x\n")
    proc = subprocess.run(
        [str(script), "main", option],
        cwd=repo.path,
        capture_output=True,
        text=True,
        env={**os.environ, "AE_FORK_PUSH_REMOTE": "fork", "AE_LEAK_ALLOW_NO_DENYLIST": "1"},
        check=False,
    )
    assert proc.returncode == 2 and f"'{option}' is refused" in proc.stderr


@pytest.mark.parametrize("option", ["-f", "--force-with-lease", "--force-with-lease=main:abc"])
def test_fork_push_lets_the_force_options_through_to_the_scan(tmp_path: Path, option: str) -> None:
    script = ROOT / "scripts" / "fork-push.sh"
    repo = _two_remotes(tmp_path)
    repo.commit("Base", a="x\n")
    repo.commit("Leak", b=FAKES["tailnet-ip"] + "\n")
    proc = subprocess.run(
        [str(script), "main", option],
        cwd=repo.path,
        capture_output=True,
        text=True,
        env={**os.environ, "AE_FORK_PUSH_REMOTE": "fork", "AE_LEAK_ALLOW_NO_DENYLIST": "1"},
        check=False,
    )
    assert "is refused" not in proc.stderr and "tailnet-ip" in proc.stdout


# --- L10. Bearer tokens of letters and passwords with punctuation ------------------------------


def test_a_letters_only_bearer_token_is_a_secret(tmp_path: Path) -> None:
    token = "abcdefghijklmnop" + "QRSTUVWXYZabcd"
    path = write(tmp_path / "a.txt", f"curl -H 'Authorization: Bearer {token}'\n")
    proc = scan(*NO_ALLOW, "text", str(path))
    assert proc.returncode == 1 and "secret bearer" in proc.stdout
    no_value_echoed(proc, token)
    code = write(tmp_path / "b.txt", "Authorization: Bearer access_token_value_here_long\n")
    assert scan(*NO_ALLOW, "text", str(code)).returncode == 0, "a snake_case name is code"


@pytest.mark.parametrize("char", ["!", "#", "@", "$", "%", "^", "&", "*"])
def test_a_password_with_punctuation_is_caught_whole(tmp_path: Path, char: str) -> None:
    value = "Ab3" + char + "Cd4" + char + "Ef5" + char + "Gh6" + char + "Ij7xyz"
    path = write(tmp_path / "a.txt", f"password = '{value}'\n")
    proc = scan(*NO_ALLOW, "text", str(path))
    assert proc.returncode == 1 and "secret assignment" in proc.stdout
    no_value_echoed(proc, value)


def test_a_shell_variable_is_not_a_password_value(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "password=$DATABASE_PASSWORD_V2_FROM_VAULT\n")
    assert scan(*NO_ALLOW, "text", str(path)).returncode == 0


# --- L11. the hook needs a pinned hash ------------------------------------------------------------


def test_the_hook_refuses_a_bundle_with_no_pin_unless_allowed(tmp_path: Path) -> None:
    bundle_file = bundle(tmp_path, DENY_WORD)
    env = _hook_env(tmp_path, AE_LEAK_DENYLIST_FILE=str(bundle_file))
    blocked = _hook_push(tmp_path / "a", env)
    assert blocked.returncode != 0 and "no pinned sha256" in blocked.stderr
    assert "sha256sum" in blocked.stderr, "the message says how the pin is written"
    allowed = _hook_push(tmp_path / "b", {**env, "AE_LEAK_ALLOW_NO_DENYLIST": "1"})
    assert allowed.returncode == 0, allowed.stderr
    pin = hashlib.sha256(bundle_file.read_bytes()).hexdigest()
    (tmp_path / "bundle.txt.sha256").write_text(pin + "  bundle.txt\n")
    assert _hook_push(tmp_path / "c", env).returncode == 0, "a sibling .sha256 file is the pin"


# ======================================================================================
# Round 3 (2026-10-28): two LOW items from the second fix check.
# ======================================================================================


@pytest.mark.parametrize(
    "line",
    [
        "commit-identity /.+@.+/ # every identity",
        "commit-identity " + "joe.smith" + "@" + "acme-corp" + ".io # a literal identity",
        "commit-identity * # everything",
        "private-repo-link /a.+/ # fewer than 4 fixed characters",
        "internal-host /^[a-z]+$/ # only classes and operators",
    ],
)
def test_identity_entries_are_hash_only_and_regexes_need_four_fixed_characters(
    tmp_path: Path, line: str
) -> None:
    allow = write(tmp_path / "allow.txt", line + "\n")
    proc = scan("--allowlist", str(allow), "text", str(write(tmp_path / "a.txt", "x\n")))
    assert proc.returncode == 2, proc.stderr


def test_a_specific_regex_and_a_hashed_identity_are_accepted(tmp_path: Path) -> None:
    allow = write(
        tmp_path / "allow.txt",
        f"commit-identity {digest('a' + '@' + 'b-corp.io')} # a reviewed identity\n"
        "media-metadata /^Lavc[0-9.]+$/ # an encoder tag\n",
    )
    assert (
        scan("--allowlist", str(allow), "text", str(write(tmp_path / "a.txt", "x\n"))).returncode
        == 0
    )


def test_retargeting_a_pr_voids_the_approval_and_fails_closed() -> None:
    wf = yaml.safe_load(LEAK_PR.read_text())
    step = next(s for s in wf["jobs"]["leaks"]["steps"] if s.get("id") == "approval")
    assert "github.event.changes.base" in step["env"]["RETARGETED"]
    run = step["run"]
    assert '"$ACTION" = "edited" ] && [ "$RETARGETED" = "true"' in run
    assert run.count("-X DELETE") == 1, "a push and a retarget share one removal"
    removal = run[run.index("-X DELETE") :]
    assert "exit 1" in removal.split("approved=$approved")[0], "a label that stays fails the job"


# ======================================================================================
# Fork backstop cost (2026-10-29). Each test fails without its fix.
# ======================================================================================


def _shared_branches_repo(tmp_path: Path, shared_body: str = "fine\n"):
    """A clone of a fake fork whose branches ae/a and ae/b share two commits. Returns the repo,
    the base sha and the shared commits' shas."""
    fork = Repo(tmp_path / "forksrc")
    base = fork.commit("Upstream", a="x\n")
    fork.git("checkout", "-q", "-b", "ae/a")
    first = fork.commit("Shared one", s1=shared_body)
    second = fork.commit("Shared two", s2="fine\n")
    fork.commit("Only on a", a1="fine\n")
    fork.git("checkout", "-q", "-b", "ae/b", second)
    fork.commit("Only on b", b1="fine\n")
    local = Repo(tmp_path / "local")
    local.git("fetch", "-q", str(fork.path), "+refs/heads/*:refs/remotes/fork/*")
    return local, base, [first, second]


def test_the_fork_backstop_scans_a_shared_commit_once(tmp_path: Path, monkeypatch, capsys) -> None:
    local, base, shared = _shared_branches_repo(tmp_path)
    mod = load_scanner()
    calls: list[str] = []
    real = mod.scan_commit

    def spy(git, scanner, sha):
        calls.append(sha)
        return real(git, scanner, sha)

    monkeypatch.setattr(mod, "scan_commit", spy)
    monkeypatch.chdir(local.path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    code = mod.run([*NO_ALLOW, "fork", "--base", base])
    out = capsys.readouterr().out
    assert code == 0, out
    assert len(calls) == len(set(calls)) == 4, "two shared commits plus one per branch, once each"
    for sha in shared:
        assert calls.count(sha) == 1
    assert "4 unique commit(s) scanned" in out


def test_a_hit_in_a_shared_commit_names_every_branch_that_carries_it(tmp_path: Path) -> None:
    local, base, _shared = _shared_branches_repo(tmp_path, shared_body=FAKES["tailnet-ip"] + "\n")
    proc = scan(*NO_ALLOW, "fork", "--base", base, cwd=local.path)
    assert proc.returncode == 1
    assert "public fork branch to delete: ae/a " in proc.stdout
    assert "public fork branch to delete: ae/b " in proc.stdout
    assert len(hit_lines(proc)) == 1, "the commit's hit is reported once, not once per branch"
    no_value_echoed(proc, "100." + "101.2.3")


def test_the_deadline_error_names_the_configured_limit(tmp_path: Path) -> None:
    mod = load_scanner()
    scanner = mod.Scanner(mod.Config())
    assert scanner.limit_seconds == mod.SCAN_SECONDS
    scanner.deadline = 0.0
    scanner.limit_seconds = 900
    with pytest.raises(mod.ScanError, match=r"its 900 s limit"):
        scanner.tick()
    path = write(tmp_path / "a.txt", "\n".join(["token = x"] * 50000) + "\n")
    proc = run_scan(*NO_ALLOW, "text", str(path), extra={"AE_LEAK_SCAN_SECONDS": "0.001"})
    assert proc.returncode == 2 and "its 0.001 s limit" in proc.stderr
    assert "120 s" not in proc.stderr


# --- the fork backstop leaves out upstream's history (2026-10-29) -----------------------------

PERSONAL = "joe.smith" + "@" + "acme-corp" + ".io"
UPSTREAM_REFS = (
    "--exclude-refs",
    "refs/remotes/upstream/",
    "--exclude-refs",
    "refs/remotes/upstream-tags/",
)


def _fork_with_upstream(tmp_path: Path):
    """A clone whose fork branch ae/x carries upstream's commit u1 (which leaks a LAN address
    and has a personal author) plus one commit of its own. Upstream's main is u1."""
    src = Repo(tmp_path / "src")
    base = src.commit("Base", a="x\n")
    src.git("config", "user.email", PERSONAL)
    u1 = src.commit("Upstream change", u=FAKES["lan-ip"] + "\n")
    src.git("config", "user.email", "bot" + "@users.noreply" + ".github.com")
    src.git("checkout", "-q", "-b", "ae/x")
    src.commit("[ae-patch] Ours", ours="fine\n")
    src.git("tag", "v1", u1)
    local = Repo(tmp_path / "local")
    local.git("fetch", "-q", str(src.path), "+refs/heads/ae/*:refs/remotes/fork/ae/*")
    local.git("fetch", "-q", str(src.path), "+refs/heads/main:refs/remotes/upstream/main")
    local.git("fetch", "-q", str(src.path), "+refs/tags/*:refs/remotes/upstream-tags/*")
    return local, base, u1


def test_upstream_refs_are_left_out_of_every_branch_range(tmp_path: Path) -> None:
    local, base, _u1 = _fork_with_upstream(tmp_path)
    plain = scan(*NO_ALLOW, "fork", "--base", base, cwd=local.path)
    assert plain.returncode == 1 and "2 unique commit(s) scanned" in plain.stdout
    left_out = scan(*NO_ALLOW, "fork", "--base", base, *UPSTREAM_REFS, cwd=local.path)
    assert left_out.returncode == 0, left_out.stdout
    assert "1 unique commit(s) scanned" in left_out.stdout


def test_upstream_tags_are_left_out_too(tmp_path: Path) -> None:
    local, base, _u1 = _fork_with_upstream(tmp_path)
    local.git("update-ref", "-d", "refs/remotes/upstream/main")
    proc = scan(
        *NO_ALLOW,
        "fork",
        "--base",
        base,
        "--exclude-refs",
        "refs/remotes/upstream-tags/",
        cwd=local.path,
    )
    assert proc.returncode == 0 and "1 unique commit(s) scanned" in proc.stdout


def test_the_backstop_workflow_fetches_upstream_and_passes_the_exclusions() -> None:
    text = (ROOT / ".github" / "workflows" / "fork-backstop.yml").read_text()
    assert "UPSTREAM_REPO" in text and "refs/remotes/upstream/*" in text
    assert "--exclude-refs refs/remotes/upstream/" in text
    assert "--exclude-refs refs/remotes/upstream-tags/" in text


# --- commit-identity only on AE's own commits in fork mode -------------------------------------


def _identity_fork(tmp_path: Path):
    fork = Repo(tmp_path / "src")
    base = fork.commit("Base", a="x\n")
    fork.git("config", "user.email", PERSONAL)
    mine = fork.commit("[ae-patch] Ours", m="fine\n")
    theirs = fork.commit("Somebody else's change", t=FAKES["lan-ip"] + "\n")
    local = Repo(tmp_path / "local")
    local.git("fetch", "-q", str(fork.path), "+refs/heads/*:refs/remotes/fork/*")
    return local, base, mine, theirs


def test_fork_mode_checks_the_email_only_on_ae_patch_commits(tmp_path: Path) -> None:
    local, base, mine, theirs = _identity_fork(tmp_path)
    proc = scan(*NO_ALLOW, "fork", "--base", base, cwd=local.path)
    assert "notice: 1 commit(s) outside upstream's refs lack the [ae-patch] prefix" in proc.stdout
    shown = hit_lines(proc)
    assert any(f"commit {mine[:10]} author" in h and "commit-identity" in h for h in shown)
    assert not any(f"commit {theirs[:10]}" in h and "commit-identity" in h for h in shown)
    assert any(h.startswith("t@" + theirs[:10]) and "lan-ip" in h for h in shown), (
        "every other class still applies to a commit that is not ours"
    )
    no_value_echoed(proc, "joe.smith", "acme-corp")


def test_the_pr_scan_still_checks_identity_on_every_commit(tmp_path: Path) -> None:
    local, base, _mine, theirs = _identity_fork(tmp_path)
    src = tmp_path / "src"
    proc = scan(*diff_args(local, base, theirs), cwd=src)
    assert f"commit {theirs[:10]} author" in proc.stdout and "commit-identity" in proc.stdout


# --- fork mode skips @outside-patches denylist entries ------------------------------------------

OLD_WORD = "oldfork" + "word"


def _published_value_fork(tmp_path: Path):
    fork = Repo(tmp_path / "src")
    base = fork.commit("Base", a="x\n")
    fork.git("config", "user.name", "Joe " + OLD_WORD)
    fork.commit("Mentions " + OLD_WORD, **{f"{OLD_WORD}.txt": f"see {OLD_WORD}\n"})
    local = Repo(tmp_path / "local")
    local.git("fetch", "-q", str(fork.path), "+refs/heads/*:refs/remotes/fork/*")
    return local, base


def test_outside_patches_entries_are_skipped_in_fork_mode_only(tmp_path: Path) -> None:
    local, base = _published_value_fork(tmp_path)
    deny = ("--denylist-bundle", str(bundle(tmp_path, f"@outside-patches {OLD_WORD}")))
    fork = scan(*NO_ALLOW, *deny, "fork", "--base", base, cwd=local.path)
    assert fork.returncode == 0, fork.stdout
    head = local.git("rev-parse", "refs/remotes/fork/main")
    pr = scan(*NO_ALLOW, *deny, "diff", "--base", base, "--head", head, cwd=local.path)
    assert pr.returncode == 1 and "denylist entry-1" in pr.stdout
    no_value_echoed(pr, OLD_WORD)


def test_a_plain_denylist_entry_still_applies_in_fork_mode(tmp_path: Path) -> None:
    local, base = _published_value_fork(tmp_path)
    deny = ("--denylist-bundle", str(bundle(tmp_path, f"@outside-patches {OLD_WORD}", OLD_WORD)))
    proc = scan(*NO_ALLOW, *deny, "fork", "--base", base, cwd=local.path)
    assert proc.returncode == 1 and "denylist entry-2" in proc.stdout
    assert "denylist entry-1" not in proc.stdout
    assert "public fork branch to delete: main" in proc.stdout


def test_the_docs_describe_the_fork_mode_exemptions() -> None:
    text = (ROOT / "docs" / "LEAK-SCAN.md").read_text()
    assert "--exclude-refs" in text and "[ae-patch]" in text and "outside-patches" in text


# ======================================================================================
# Fork backstop, first review round (2026-10-29). Each test fails without its fix.
# ======================================================================================


def _denylist_identity_fork(tmp_path: Path, subject: str, name: str):
    fork = Repo(tmp_path / "src")
    base = fork.commit("Base", a="x\n")
    fork.git("config", "user.name", name)
    fork.git("config", "user.email", PERSONAL)
    sha = fork.commit(subject, m="fine\n")
    local = Repo(tmp_path / "local")
    local.git("fetch", "-q", str(fork.path), "+refs/heads/*:refs/remotes/fork/*")
    return local, base, sha


@pytest.mark.parametrize("subject", ["[ae-patch] Ours", "fixup! Ours", 'Revert "Ours"', "Merge x"])
def test_the_denylist_runs_on_every_commit_identity_in_fork_mode(
    tmp_path: Path, subject: str
) -> None:
    local, base, sha = _denylist_identity_fork(tmp_path, subject, "Joe " + DENY_WORD)
    deny = ("--denylist-bundle", str(bundle(tmp_path, DENY_WORD)))
    proc = scan(*NO_ALLOW, *deny, "fork", "--base", base, cwd=local.path)
    assert proc.returncode == 1
    assert f"commit {sha[:10]} author:0: denylist entry-1" in proc.stdout
    assert f"commit {sha[:10]} committer:0: denylist entry-1" in proc.stdout
    no_value_echoed(proc, DENY_WORD)


def test_an_outside_patches_name_is_still_skipped_in_a_commit_identity(tmp_path: Path) -> None:
    local, base, _sha = _denylist_identity_fork(tmp_path, "Plain subject", "Joe " + DENY_WORD)
    deny = ("--denylist-bundle", str(bundle(tmp_path, f"@outside-patches {DENY_WORD}")))
    assert scan(*NO_ALLOW, *deny, "fork", "--base", base, cwd=local.path).returncode == 0


def test_the_unprefixed_notice_counts_only_commits_outside_upstream(tmp_path: Path) -> None:
    local, base, _u1 = _fork_with_upstream(tmp_path)
    proc = scan(*NO_ALLOW, "fork", "--base", base, *UPSTREAM_REFS, cwd=local.path)
    assert "lack the [ae-patch] prefix" not in proc.stdout, "u1 is upstream's, ours is prefixed"


# --- the workflow checks upstream.lock before trusting it ---------------------------------------


def _workflow_head() -> str:
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "fork-backstop.yml").read_text())
    run = next(
        s["run"] for s in wf["jobs"]["scan"]["steps"] if "run" in s and "UPSTREAM" in s["run"]
    )
    return run.split("mkdir -p fork-backstop-state")[0]


@pytest.mark.parametrize(
    ("upstream", "ok"),
    [
        ("", False),
        ("https://github.com/" + "analytic" + "endeavors/omnigent.git", False),
        ("git@github.com:" + "Analytic" + "Endeavors/omnigent.git", False),
        ("https://github.com/omnigent-ai/omnigent.git", True),
    ],
)
def test_the_workflow_refuses_a_bad_upstream_repo(tmp_path: Path, upstream: str, ok: bool) -> None:
    fork = "https://github.com/" + "analytic" + "endeavors/omnigent.git"
    (tmp_path / "upstream.lock").write_text(
        f"UPSTREAM_REPO={upstream}\nUPSTREAM_SHA={'a' * 40}\nAE_FORK={fork}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", _workflow_head()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
    )
    assert (proc.returncode == 0) is ok, proc.stdout
    if not ok:
        assert "::error::UPSTREAM_REPO in upstream.lock" in proc.stdout


def test_the_workflow_fails_when_the_upstream_fetch_brings_no_branches() -> None:
    text = (ROOT / ".github" / "workflows" / "fork-backstop.yml").read_text()
    assert "git for-each-ref --count=1 refs/remotes/upstream/" in text
    assert "produced no branches" in text and "exit 1" in text


def test_the_workflow_equals_check_covers_the_fork_itself(tmp_path: Path) -> None:
    same = "https://github.com/omnigent-ai/omnigent.git"
    (tmp_path / "upstream.lock").write_text(
        f"UPSTREAM_REPO={same}\nUPSTREAM_SHA=a\nAE_FORK={same}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", _workflow_head()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
    )
    assert proc.returncode == 1 and "equals AE_FORK" in proc.stdout


# --- --exclude-refs is validated -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "why"),
    [
        ("refs/remotes/upstream", "must end in"),
        ("refs/remotes/fork/", "fork's own refs"),
        ("refs/remotes/", "fork's own refs"),
        ("refs/remotes/fork/ae/", "fork's own refs"),
        ("refs/remotes/nothing-here/", "matches no ref"),
    ],
)
def test_a_bad_exclude_refs_prefix_exits_two(tmp_path: Path, prefix: str, why: str) -> None:
    local, base, _u1 = _fork_with_upstream(tmp_path)
    proc = scan(*NO_ALLOW, "fork", "--base", base, "--exclude-refs", prefix, cwd=local.path)
    assert proc.returncode == 2 and why in proc.stderr, proc.stderr


# --- the shared-commit test that did not fail on the old code ---------------------------------------


def test_a_shared_leaking_commit_is_scanned_exactly_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local, base, shared = _shared_branches_repo(tmp_path, shared_body=FAKES["tailnet-ip"] + "\n")
    mod = load_scanner()
    calls: list[str] = []
    real = mod.scan_commit

    def spy(git, scanner, sha):
        calls.append(sha)
        return real(git, scanner, sha)

    monkeypatch.setattr(mod, "scan_commit", spy)
    monkeypatch.chdir(local.path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert mod.run([*NO_ALLOW, "fork", "--base", base]) == 1
    capsys.readouterr()
    assert calls.count(shared[0]) == 1, "the leaking commit sits on two branches"


# --- the per-branch loop is ticked --------------------------------------------------------------------


def test_the_fork_branch_loop_ticks_the_deadline_for_every_branch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """With the per-branch scans stubbed out, the only tick left is the loop's own call."""
    local, base, _shared = _shared_branches_repo(tmp_path)
    mod = load_scanner()
    ticks: list[int] = []
    monkeypatch.setattr(mod.Scanner, "tick", lambda _self: ticks.append(1))
    monkeypatch.setattr(mod.Scanner, "scan_text", lambda _self, *_a, **_k: [])
    monkeypatch.setattr(mod, "scan_commit", lambda *_a, **_k: [])
    monkeypatch.chdir(local.path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert mod.run([*NO_ALLOW, "fork", "--base", base]) == 0
    capsys.readouterr()
    branches = 2  # ae/a and ae/b
    assert len(ticks) >= branches, "the branch loop must call tick() once per branch"


def test_a_past_deadline_stops_a_fork_scan(tmp_path: Path) -> None:
    local, base, _shared = _shared_branches_repo(tmp_path)
    proc = run_scan(
        *NO_ALLOW, "fork", "--base", base, extra={"AE_LEAK_SCAN_SECONDS": "-1"}, cwd=local.path
    )
    assert proc.returncode == 2 and "limit" in proc.stderr


# --- the unprefixed-commit notice says what was actually checked ---------------------------------------


def test_the_notice_says_the_denylist_was_checked_when_one_is_loaded(tmp_path: Path) -> None:
    local, base, _mine, _theirs = _identity_fork(tmp_path)
    deny = ("--denylist-bundle", str(bundle(tmp_path, DENY_WORD)))
    proc = scan(*NO_ALLOW, *deny, "fork", "--base", base, cwd=local.path)
    assert (
        "the e-mail check was skipped and the denylist was checked on their author and committer"
        in proc.stdout
    )
    assert "no denylist is loaded" not in proc.stdout


def test_the_notice_says_no_denylist_is_loaded_when_none_is(tmp_path: Path) -> None:
    local, base, _mine, _theirs = _identity_fork(tmp_path)
    proc = scan(*NO_ALLOW, "fork", "--base", base, cwd=local.path)
    assert "the e-mail check was skipped and no denylist is loaded" in proc.stdout
    assert "the denylist was checked" not in proc.stdout


# ======================================================================================
# The fork baseline (2026-10-29): the backstop fails only on values the fork did not carry
# ======================================================================================

BASELINE_FILE = ROOT / "scripts" / "leak-fork-baseline.txt"
MSG_EMAIL = "ann.lee" + "@" + "acme-corp" + ".io"
EMAIL_ADDR = PERSONAL


def _baseline_fork(tmp_path: Path):
    """A fork whose branch ae/orig carries an e-mail in a file, one in its message and the
    committer identity: three soft values of two classes. Returns (src, local, base)."""
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    src.git("config", "user.email", EMAIL_ADDR)
    src.git("checkout", "-q", "-b", "ae/orig")
    src.commit(f"[ae-patch] Add the fixture, ask {MSG_EMAIL}", f__txt=f"contact {EMAIL_ADDR}\n")
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    return src, local, base


def _refetch(src: Repo, local: Repo) -> None:
    local.git("fetch", "-q", "--force", str(src.path), "+refs/heads/*:refs/remotes/fork/*")


def _rebase_like(src: Repo, local: Repo) -> str:
    """Same change, new sha (the author date moves), as a patch-rebase.yml push does."""
    before = src.git("rev-parse", "HEAD")
    src.git("commit", "-q", "--amend", "--no-edit", "--date", "2001-01-01T00:00:00")
    after = src.git("rev-parse", "HEAD")
    assert before != after
    _refetch(src, local)
    return after


def _deny(tmp_path: Path) -> tuple[str, str]:
    return ("--denylist-bundle", str(bundle(tmp_path, DENY_WORD)))


def _write_baseline(tmp_path: Path, local: Repo, base: str, *extra: str):
    out = tmp_path / "baseline.txt"
    proc = scan(
        *NO_ALLOW, *_deny(tmp_path), *extra,
        "fork", "--base", base, "--write-baseline", str(out), cwd=local.path,
    )  # fmt: skip
    return proc, out


def test_a_baselined_value_is_accepted_after_a_rebase_with_and_without_state(
    tmp_path: Path,
) -> None:
    src, local, base = _baseline_fork(tmp_path)
    made, out = _write_baseline(tmp_path, local, base)
    assert made.returncode == 0, made.stdout + made.stderr
    _rebase_like(src, local)
    state = tmp_path / "state.txt"
    args = (*NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--baseline", str(out))
    cold = scan(*args, cwd=local.path)
    assert cold.returncode == 0, cold.stdout + cold.stderr
    assert "notice: fork baseline accepted 4 hit(s) in 1 commit(s): " in cold.stdout
    assert "commit-identity=" in cold.stdout and "email=" in cold.stdout
    no_value_echoed(cold, EMAIL_ADDR, MSG_EMAIL, "acme-corp")
    warm = scan(*args, "--state", str(state), cwd=local.path)
    assert warm.returncode == 0 and state.read_text().startswith("ae/orig ")


def test_a_new_value_on_a_baselined_branch_fails_and_known_values_stay_quiet(
    tmp_path: Path,
) -> None:
    src, local, base = _baseline_fork(tmp_path)
    _made, out = _write_baseline(tmp_path, local, base)
    src.commit("[ae-patch] More", g=FAKES["lan-ip"] + "\n")
    _refetch(src, local)
    proc = scan(
        *NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--baseline", str(out),
        cwd=local.path,
    )  # fmt: skip
    assert proc.returncode == 1
    assert "public fork branch to delete: ae/orig" in proc.stdout
    assert "with new hits" in proc.stdout and "not a way to clear" in proc.stderr
    shown = hit_lines(proc)
    assert any("lan-ip" in h for h in shown)
    assert not any(" email " in h or "commit-identity" in h for h in shown), shown
    no_value_echoed(proc, EMAIL_ADDR, MSG_EMAIL, LAN)


@pytest.mark.parametrize(
    "body",
    [
        FAKES["secret"] + "\n",
        FAKES["private-key"] + "\n",
        f"see the {DENY_WORD} plan\n",
        "x = 1  # leak-scan: allow=" + "email\n",
    ],
    ids=["secret", "private-key", "denylist", "marker-without-reason"],
)
def test_hard_classes_and_markers_fail_with_a_baseline_loaded(tmp_path: Path, body: str) -> None:
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    src.git("checkout", "-q", "-b", "ae/bad")
    src.commit("Add", f=body)
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    empty = write(tmp_path / "empty-baseline.txt", "# nothing accepted\n")
    proc = scan(
        *NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--baseline", str(empty),
        cwd=local.path,
    )  # fmt: skip
    assert proc.returncode == 1 and "public fork branch to delete: ae/bad" in proc.stdout


def test_a_baseline_never_clears_a_hard_hit_even_by_hash(tmp_path: Path) -> None:
    """A hand-made line for the secret's hash is refused, not honoured."""
    secret = FAKES["secret"].split(" ", 1)[1]
    forged = write(tmp_path / "b.txt", f"secret {digest(secret)} # forged\n")
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    proc = scan(
        *NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--baseline", str(forged),
        cwd=local.path,
    )  # fmt: skip
    assert proc.returncode == 2 and "never accepts" in proc.stderr


HEX = "ab" * 32
BAD_BASELINES = [
    ("secret", f"secret sha256:{HEX} # why\n", "never accepts"),
    ("denylist", f"denylist sha256:{HEX} # why\n", "never accepts"),
    ("private-key", f"private-key sha256:{HEX} # why\n", "never accepts"),
    ("marker", f"marker sha256:{HEX} # why\n", "never accepts"),
    ("unknown-class", f"nothing sha256:{HEX} # why\n", "unknown class"),
    ("short-hex", f"email sha256:{HEX[:-2]} # why\n", "malformed digest"),
    ("upper-hex", f"email sha256:{HEX.upper()} # why\n", "malformed digest"),
    ("no-prefix", f"email {HEX} # why\n", "malformed digest"),
    ("no-reason", f"email sha256:{HEX}\n", "no '# reason'"),
    ("blank-reason", f"email sha256:{HEX} #  \n", "no '# reason'"),
    ("extra-field", f"email sha256:{HEX} extra # why\n", "malformed"),
    ("duplicate", f"email sha256:{HEX} # a\nemail sha256:{HEX} # b\n", "repeats"),
]


@pytest.mark.parametrize(
    ("text", "why"), [(t, w) for _n, t, w in BAD_BASELINES], ids=[n for n, *_ in BAD_BASELINES]
)
def test_a_malformed_baseline_exits_two(tmp_path: Path, text: str, why: str) -> None:
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    path = write(tmp_path / "b.txt", text)
    proc = scan(*NO_ALLOW, "fork", "--base", base, "--baseline", str(path), cwd=local.path)
    assert proc.returncode == 2 and why in proc.stderr, proc.stderr


def test_an_unreadable_baseline_exits_two(tmp_path: Path) -> None:
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    missing = tmp_path / "missing.txt"
    proc = scan(*NO_ALLOW, "fork", "--base", base, "--baseline", str(missing), cwd=local.path)
    assert proc.returncode == 2 and "Cannot read the fork baseline" in proc.stderr


def test_a_baseline_that_carries_a_denylist_value_or_its_hash_exits_two(tmp_path: Path) -> None:
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    for text, why in (
        (f"email sha256:{HEX} # seen in {DENY_WORD}.txt\n", "contains denylist entry 1"),
        (f"email {digest(DENY_WORD)} # why\n", "hash of denylist entry 1"),
    ):
        path = write(tmp_path / "b.txt", text)
        proc = scan(
            *NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--baseline", str(path),
            cwd=local.path,
        )  # fmt: skip
        assert proc.returncode == 2 and why in proc.stderr, proc.stderr
        no_value_echoed(proc, DENY_WORD)


def test_write_baseline_is_sorted_deduplicated_and_byte_identical(tmp_path: Path) -> None:
    src, local, base = _baseline_fork(tmp_path)
    # A second branch carries the same file again: one value, one line.
    src.git("checkout", "-q", "-b", "ae/dup", base)
    src.commit("[ae-patch] Again", g=f"contact {EMAIL_ADDR}\n")
    _refetch(src, local)
    first, out = _write_baseline(tmp_path, local, base)
    assert first.returncode == 0, first.stdout + first.stderr
    text = out.read_text()
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines == sorted(lines) and len(lines) == len(set(lines))
    keys = [ln.split(" # ")[0] for ln in lines]
    assert keys == [
        f"commit-identity {digest(EMAIL_ADDR)}",
        *sorted([f"email {digest(EMAIL_ADDR)}", f"email {digest(MSG_EMAIL)}"]),
    ]
    assert "baseline written: 3 entr(ies): commit-identity=1, email=2" in first.stdout
    second, again = _write_baseline(tmp_path, local, base)
    assert second.returncode == 0 and again.read_bytes() == out.read_bytes()
    for proc in (first, second):
        no_value_echoed(proc, EMAIL_ADDR, MSG_EMAIL, "acme-corp")
        assert digest(EMAIL_ADDR)[7:] not in proc.stdout + proc.stderr
        assert digest(MSG_EMAIL)[7:] not in proc.stdout + proc.stderr
    assert re.search(r"[0-9a-f]{10}", "".join(ln.split(" # ")[1] for ln in lines)) is None, (
        "no commit sha in a comment: it would change on a rebase"
    )


def test_write_baseline_ignores_the_allowlist_and_markers(tmp_path: Path) -> None:
    src = Repo(tmp_path / "src")
    base = src.commit("Upstream", a="x\n")
    src.git("checkout", "-q", "-b", "ae/m")
    src.commit("Add", f=f'contact {EMAIL_ADDR}  # leak-scan: allow=email reason="fixture"\n')
    local = Repo(tmp_path / "local")
    _refetch(src, local)
    allow = write(tmp_path / "allow.txt", f"email {digest(MSG_EMAIL)} # fixture\n")
    out = tmp_path / "baseline.txt"
    proc = scan(
        "--allowlist", str(allow), *_deny(tmp_path), "fork", "--base", base,
        "--write-baseline", str(out), cwd=local.path,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"email {digest(EMAIL_ADDR)}" in out.read_text(), "the marker did not hide it"
    # The allowlist does not hide a value either: allow the value that is present.
    allow2 = write(tmp_path / "allow2.txt", f"email {digest(EMAIL_ADDR)} # fixture\n")
    out2 = tmp_path / "baseline2.txt"
    proc2 = scan(
        "--allowlist", str(allow2), *_deny(tmp_path), "fork", "--base", base,
        "--write-baseline", str(out2), cwd=local.path,
    )  # fmt: skip
    assert proc2.returncode == 0 and out2.read_bytes() == out.read_bytes()


def test_write_baseline_exits_two_without_a_denylist_or_with_state_or_baseline(
    tmp_path: Path,
) -> None:
    _src, local, base = _baseline_fork(tmp_path)
    out = tmp_path / "baseline.txt"
    no_deny = scan(*NO_ALLOW, "fork", "--base", base, "--write-baseline", str(out), cwd=local.path)
    assert no_deny.returncode == 2 and "needs the private denylist" in no_deny.stderr
    with_state = scan(
        *NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--state", str(tmp_path / "s.txt"),
        "--write-baseline", str(out), cwd=local.path,
    )  # fmt: skip
    assert with_state.returncode == 2 and "cannot take --state" in with_state.stderr
    other = write(tmp_path / "other.txt", "# none\n")
    with_baseline = scan(
        *NO_ALLOW, *_deny(tmp_path), "fork", "--base", base, "--baseline", str(other),
        "--write-baseline", str(out), cwd=local.path,
    )  # fmt: skip
    assert with_baseline.returncode == 2 and not out.exists()


def test_write_baseline_refuses_while_a_hard_hit_exists(tmp_path: Path) -> None:
    src, local, base = _baseline_fork(tmp_path)
    src.git("checkout", "-q", "-b", "ae/secret", base)
    src.commit("Add", s=FAKES["secret"] + "\n")
    _refetch(src, local)
    proc, out = _write_baseline(tmp_path, local, base)
    assert proc.returncode == 1 and not out.exists()
    assert "baseline not written: 1 hit(s) cannot be baselined (secret=1)" in proc.stdout
    assert "public fork branch to delete: ae/secret" in proc.stdout
    no_value_echoed(proc, FAKES["secret"].split(" ", 1)[1], EMAIL_ADDR)


def test_the_committed_fork_baseline_parses_and_passes_the_denylist_check() -> None:
    if not BASELINE_FILE.exists():
        pytest.skip("no fork baseline: the public sync leaves it out on purpose")
    mod = load_scanner()
    text = BASELINE_FILE.read_text()
    entries = mod.parse_baseline(text)
    assert entries, "the committed baseline holds the values the fork carried on 2026-10-29"
    deny = mod.parse_bundle("# fake bundle\n" + DENY_WORD + "\n")
    mod.check_baseline_against_denylist(entries, deny, text)
    assert {cls for cls, _digest in entries} <= mod.SOFT


def _backstop_wf() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "fork-backstop.yml").read_text())


def test_the_backstop_passes_the_baseline_and_keys_its_cache_on_it() -> None:
    steps = _backstop_wf()["jobs"]["scan"]["steps"]
    run = next(s["run"] for s in steps if "UPSTREAM" in s.get("run", ""))
    assert '--baseline "$workspace/scripts/leak-fork-baseline.txt"' in run
    cache = next(s for s in steps if "actions/cache" in s.get("uses", ""))["with"]
    key = "fork-backstop-${{ hashFiles('scripts/leak-fork-baseline.txt') }}-"
    assert cache["key"] == key + "${{ github.run_id }}" and cache["restore-keys"] == key


def _leak_pr_step_list() -> list[dict]:
    return yaml.safe_load(LEAK_PR.read_text())["jobs"]["leaks"]["steps"]


def test_the_pr_workflow_guards_the_baseline_and_voids_the_approval_on_a_push() -> None:
    steps = _leak_pr_step_list()
    names = [s.get("name", "") for s in steps]
    guard = next(s for s in steps if "fork baseline" in s.get("name", "") and "run" in s)
    assert names.index(guard["name"]) > names.index(
        "Decide whether the allowlist change is approved"
    )
    assert names.index(guard["name"]) < names.index("Scan the commits, paths, title and body")
    assert (
        '"$BASE_SHA...$HEAD_SHA"' in guard["run"]
        and "scripts/leak-fork-baseline.txt" in guard["run"]
    )
    assert guard["env"]["APPROVED"] == "${{ steps.approval.outputs.approved }}"
    approval = next(s for s in steps if s.get("id") == "approval")["run"]
    assert (
        "scripts/leak-fork-baseline.txt" in approval and "scripts/leak-allowlist.txt" in approval
    )


@pytest.mark.parametrize(
    ("changed", "approved", "code"),
    [(False, "false", 0), (True, "false", 1), (True, "true", 0), (False, "true", 0)],
)
def test_the_baseline_guard_step_runs_as_the_workflow_runs_it(
    tmp_path: Path, changed: bool, approved: str, code: int
) -> None:
    guard = next(
        s for s in _leak_pr_step_list() if "fork baseline" in s.get("name", "") and "run" in s
    )
    repo = Repo(tmp_path / "r")
    base = repo.commit("Base", **{"scripts__leak-fork-baseline.txt": "# none\n"})
    head = (
        repo.commit("Head", **{"scripts__leak-fork-baseline.txt": "# none\n# more\n"})
        if changed
        else repo.commit("Head", other="y\n")
    )
    proc = subprocess.run(
        ["bash", "-c", guard["run"]],
        cwd=repo.path, capture_output=True, text=True, check=False,
        env={**os.environ, "BASE_SHA": base, "HEAD_SHA": head, "APPROVED": approved},
    )  # fmt: skip
    assert proc.returncode == code, proc.stdout + proc.stderr
    if code:
        assert "::error::" in proc.stdout


def test_the_baseline_guard_ignores_what_the_base_gained_since_the_branch_left_it(
    tmp_path: Path,
) -> None:
    guard = next(
        s for s in _leak_pr_step_list() if "fork baseline" in s.get("name", "") and "run" in s
    )
    repo = Repo(tmp_path / "r")
    fork_point = repo.commit("Base", a="x\n")
    repo.git("checkout", "-q", "-b", "topic")
    head = repo.commit("Topic", b="y\n")
    repo.git("checkout", "-q", "main")
    base = repo.commit("Base gains the file", **{"scripts__leak-fork-baseline.txt": "# new\n"})
    assert fork_point != base
    proc = subprocess.run(
        ["bash", "-c", guard["run"]],
        cwd=repo.path, capture_output=True, text=True, check=False,
        env={**os.environ, "BASE_SHA": base, "HEAD_SHA": head, "APPROVED": "false"},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_docs_describe_the_fork_baseline() -> None:
    text = (ROOT / "docs" / "LEAK-SCAN.md").read_text()
    section = text.split("The fork baseline (2026-10-29)")[1].split("\n## ")[0]
    for needle in (
        "leak-fork-baseline.txt",
        "--baseline",
        "--write-baseline",
        "class and the hash",
        "never accepts",
        "leak-allowlist-approved",
        "public release",
    ):
        assert needle in section, needle
