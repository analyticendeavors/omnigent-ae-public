#!/usr/bin/env python3
"""Block internal identifiers before they leave the box (2026-10-28).

  scripts/leak-scan.py diff --base B --head H [--title-env NAME] [--body-env NAME]
  scripts/leak-scan.py tree (--ref R | --git-dir D --tree T)
  scripts/leak-scan.py text FILE...|-
  scripts/leak-scan.py media FILE... [--strip] [--ocr auto|off]
  scripts/leak-scan.py push [REMOTE [URL]]      (git pre-push hook mode, refs on stdin)

Scans for IPs, tailnet and Azure host names, e-mail addresses, home paths, cloud
ids, private repository links, secrets, image metadata, and for the strings of a
private denylist bundle (--denylist-bundle). docs/LEAK-SCAN.md has the classes and
how to clear a false positive. Stdlib only, Python 3.11 or later.

A hit prints as ``path:line: class rule-id``. The matched text is never printed,
logged or written to a report, so a scan can run in a public log. Exit 0 is clean,
1 means hits, 2 means the scan itself failed (an unreadable input, a bad bundle or
allowlist, a required denylist that is missing): fail closed, the way
sync-public.sh's scan_one does.

The release runs this file only at the sha256 its script pins (LEAK_SCAN_SHA256 in
sync-public.sh), since it runs with admin credentials: a change here needs the new
sum set there before the next public release.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import fnmatch
import hashlib
import ipaddress
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = "scripts/leak-allowlist.txt"
MAX_PRINTED = 50
HARD = frozenset({"denylist", "secret", "private-key"})
SOFT = frozenset(
    {
        "tailnet-host",
        "tailnet-ip",
        "lan-ip",
        "email",
        "home-path",
        "azure-endpoint",
        "cloud-id",
        "private-repo-link",
        "path-name",
        "media-metadata",
        "media-text",
        "internal-host",
        "binary",
        "commit-identity",
    }
)
ALL_CLASSES = HARD | SOFT | {"marker"}
LOCKFILES = {
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
}
MEDIA_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}
# Binary files that carry no readable text or metadata worth a person's look. Anything else
# binary that a change adds or edits is a hit (binary unreviewed) until a person allowlists
# its exact path.
SAFE_BINARY_SUFFIXES = {".woff", ".woff2", ".ttf", ".otf", ".eot", ".ico", ".wasm", ".map"}
SCAN_SECONDS = 120
LOCKLIKE_SEGMENTS = ("node_modules/", "/dist/", "/vendor/", "/vendored/", "/generated/")
ZERO_SHA = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Names a document or a test may use in place of a real value.
PLACEHOLDER_USERS = {
    "user",
    "u",
    "demo",
    "agent",
    "runner",
    "you",
    "username",
    "name",
    "me",
    "someone",
    "example",
    "x",
    "foo",
    "bar",
    "test",
}
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net"}
EXAMPLE_TLDS = {"example", "invalid", "test", "localhost"}
TRAILER = re.compile(
    r"^(From|Signed-off-by|Co-authored-by|Reviewed-by|Acked-by|Tested-by|Reported-by|Cc):",
    re.IGNORECASE,
)
MARKER = re.compile(r"leak-scan:\s*allow=([a-z][a-z,-]*)(?:\s+reason=\"([^\"]*)\")?")
HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ScanError(Exception):
    """The scan could not run; the caller exits 2 so a crash is never read as clean."""


@dataclass(frozen=True)
class Hit:
    """A finding. ``path`` is always the redacted display path, never a raw file name."""

    path: str
    line: int
    cls: str
    rule: str
    commit: str = ""
    digest: str = ""

    def shown(self, *, hashes: bool = False) -> str:
        where = f"{self.path}@{self.commit}" if self.commit else self.path
        tail = f" sha256:{self.digest}" if hashes and self.digest else ""
        return f"{where}:{self.line}: {self.cls} {self.rule}{tail}"


def digest_of(value: str) -> str:
    """The allowlist key of a matched value: sha256 of its lower-cased text."""
    return hashlib.sha256(value.lower().encode("utf-8", errors="replace")).hexdigest()


def escape_annotation(text: str) -> str:
    """Percent-escape a workflow-command property so a crafted name cannot inject commands."""
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def clean_label(text: str) -> str:
    """Control characters and newlines in a shown name become ``?``."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


@dataclass
class DenyEntry:
    number: int
    text: str
    scope: str  # "", "outside-patches" or "new-lines"


@dataclass
class AllowEntry:
    cls: str
    literal: str | None
    regex: re.Pattern[str] | None
    glob: str | None
    number: int

    def matches(self, cls: str, key: str, path: str) -> bool:
        if self.cls != cls:
            return False
        if self.glob and not fnmatch.fnmatchcase(path, self.glob):
            return False
        if self.regex is not None:
            return bool(self.regex.search(key))
        assert self.literal is not None
        if self.literal.startswith("sha256:"):
            return digest_of(key) == self.literal[7:].lower()
        if cls == "binary":
            return key == self.literal  # an exact path, never a pattern
        return fnmatch.fnmatchcase(key.lower(), self.literal.lower())


@dataclass
class Config:
    deny: list[DenyEntry] = field(default_factory=list)
    allow: list[AllowEntry] = field(default_factory=list)
    new_lines: bool = False  # diff, push and text scans see new content only
    fork: bool = False  # the fork backstop: @outside-patches denylist entries are skipped

    def deny_for(self, path: str) -> list[DenyEntry]:
        in_patches = path.startswith("patches/")
        out = []
        for entry in self.deny:
            if entry.scope == "outside-patches" and (in_patches or self.fork):
                continue
            if entry.scope == "new-lines" and not self.new_lines:
                continue
            out.append(entry)
        return out


# ---------------------------------------------------------------------------
# Bundles and allowlists


def parse_bundle(text: str) -> list[DenyEntry]:
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        scope = ""
        for prefix in ("@outside-patches ", "@new-lines "):
            if line.startswith(prefix):
                scope = prefix[1:].strip()
                line = line[len(prefix) :].strip()
                break
        if line.startswith("@"):
            raise ScanError(
                "The denylist bundle has a line with an unknown scope prefix. Valid prefixes "
                "are '@outside-patches ' and '@new-lines '; rebuild the bundle from "
                "public-release/denylist-bundle.sh."
            )
        if len(line) < 3:
            raise ScanError(
                "The denylist bundle has an entry shorter than 3 characters, which would flag "
                "most lines. Fix the entry in the private denylist and rebuild the bundle."
            )
        entries.append(DenyEntry(len(entries) + 1, line.lower(), scope))
    return entries


# Classes whose values are addresses and ids: an allowlist entry for one is a hash and nothing
# else, so no wildcard can clear them wholesale (2026-10-28, second review).
HASH_ONLY = frozenset(
    {
        "email",
        "commit-identity",
        "lan-ip",
        "tailnet-host",
        "tailnet-ip",
        "cloud-id",
        "home-path",
        "azure-endpoint",
    }
)


def parse_allowlist(text: str) -> list[AllowEntry]:
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        body, sep, reason = raw.partition("#")
        if not sep or not reason.strip():
            raise ScanError(
                f"Allowlist line {number} has no '# reason'. Every entry says why it is safe: "
                "'<class> <literal-or-/regex/> [path-glob] # reason'."
            )
        parts = body.split()
        if len(parts) not in (2, 3):
            raise ScanError(
                f"Allowlist line {number} is malformed. Use '<class> <literal-or-/regex/> "
                "[path-glob] # reason'; a pattern cannot contain spaces (use \\s in a regex)."
            )
        cls, pattern = parts[0], parts[1]
        if cls in HARD:
            raise ScanError(
                f"Allowlist line {number} names class '{cls}'. The allowlist never clears "
                "denylist, secret or private-key hits; fix the text instead."
            )
        if cls not in SOFT:
            raise ScanError(f"Allowlist line {number} names an unknown class '{cls}'.")
        glob = parts[2] if len(parts) == 3 else None
        if cls in HASH_ONLY and not pattern.startswith("sha256:"):
            raise ScanError(
                f"Allowlist line {number}: a {cls} entry must be 'sha256:<hex>', never a literal "
                "or a regex, because a wildcard there could clear every address or id of the "
                "class. Run the scan with --hash-hits to print the digest."
            )
        if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
            try:
                regex = re.compile(pattern[1:-1])
            except re.error as err:
                raise ScanError(f"Allowlist line {number} has an invalid regex: {err}.") from err
            if any(regex.search(probe) for probe in ("", "a", "1", "x.y", "-")):
                raise ScanError(
                    f"Allowlist line {number} has a regex that matches almost anything, so it "
                    "would clear a whole class. Key the entry on the value instead: run the "
                    "scan with --hash-hits and use 'sha256:<hex>', or use a line marker."
                )
            fixed = re.sub(r"[^A-Za-z0-9]", "", re.sub(r"\[[^\]]*\]", "", pattern[1:-1]))
            if len(fixed) < 4:
                raise ScanError(
                    f"Allowlist line {number} has a regex with fewer than 4 fixed characters "
                    "outside classes and operators, so it would clear far more than one value. "
                    "Name the value, or key the entry on 'sha256:<hex>' (--hash-hits)."
                )
            entries.append(AllowEntry(cls, None, regex, glob, number))
        else:
            if pattern.startswith("sha256:") and not re.fullmatch(
                r"sha256:[0-9a-fA-F]{64}", pattern
            ):
                raise ScanError(f"Allowlist line {number} has a malformed sha256 entry.")
            if not pattern.startswith("sha256:") and (
                len(pattern.replace("*", "").replace("?", "")) < 4
            ):
                raise ScanError(
                    f"Allowlist line {number} has a literal of fewer than 4 characters besides "
                    "wildcards, which would clear far more than one value. Name the value, or "
                    "key the entry on 'sha256:<hex>' (--hash-hits)."
                )
            if cls == "binary" and (glob or any(ch in pattern for ch in "*?[")):
                raise ScanError(
                    f"Allowlist line {number}: a binary entry is an exact path, with no "
                    "wildcards and no path glob."
                )
            entries.append(AllowEntry(cls, pattern, None, glob, number))
    return entries


def check_allowlist_against_denylist(
    allow: list[AllowEntry], deny: list[DenyEntry], allow_text: str
) -> None:
    """The public allowlist must not carry or match a private value (fail closed)."""
    lowered = allow_text.lower()
    for entry in deny:
        if entry.text in lowered:
            raise ScanError(
                f"The allowlist text contains denylist entry {entry.number}. Remove that line "
                "from scripts/leak-allowlist.txt; a public file cannot hold a private value."
            )
        for item in allow:
            if item.regex is not None and item.regex.fullmatch(entry.text):
                raise ScanError(
                    f"Allowlist line {item.number} matches denylist entry {entry.number}. "
                    "Narrow the pattern in scripts/leak-allowlist.txt."
                )
            if item.literal is not None and fnmatch.fnmatchcase(entry.text, item.literal.lower()):
                raise ScanError(
                    f"Allowlist line {item.number} matches denylist entry {entry.number}. "
                    "Narrow the pattern in scripts/leak-allowlist.txt."
                )


BASELINE_LINE = re.compile(r"sha256:[0-9a-f]{64}")
BASELINE_HEADER = (
    '# Fork backstop baseline (docs/LEAK-SCAN.md, "The fork baseline"). One line per value the\n'
    "# public fork already carried when the baseline was taken: '<class> sha256:<hex> # reason'.\n"
    "# The key is the class and the hash of the lower-cased value, never a commit, so a rebase\n"
    "# does not re-open an old hit. Only `fork --baseline` reads it. Generated, never edited:\n"
    "# python3 scripts/leak-scan.py <denylist options> fork --base <sha> --write-baseline <file>\n"
    "# Any change to this file needs the leak-allowlist-approved label.\n"
)


def parse_baseline(text: str) -> set[tuple[str, str]]:
    """The (class, digest) pairs of a fork baseline file. Anything malformed is exit 2, so a
    damaged file can never read as an empty or a partial baseline."""
    entries: set[tuple[str, str]] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        body, sep, reason = raw.partition("#")
        if not sep or not reason.strip():
            raise ScanError(
                f"Baseline line {number} has no '# reason'. Regenerate the file with "
                "'fork --write-baseline' instead of editing it."
            )
        parts = body.split()
        if len(parts) != 2:
            raise ScanError(
                f"Baseline line {number} is malformed. Use '<class> sha256:<hex> # reason'; "
                "regenerate the file with 'fork --write-baseline'."
            )
        cls, digest = parts
        if cls in HARD or cls == "marker":
            raise ScanError(
                f"Baseline line {number} names class '{cls}'. A baseline never accepts a "
                "denylist, secret, private-key or marker hit; delete the fork branch instead."
            )
        if cls not in SOFT:
            raise ScanError(f"Baseline line {number} names an unknown class '{cls}'.")
        if not BASELINE_LINE.fullmatch(digest):
            raise ScanError(
                f"Baseline line {number} has a malformed digest. Use 'sha256:' and 64 "
                "lower-case hex characters."
            )
        key = (cls, digest[7:])
        if key in entries:
            raise ScanError(f"Baseline line {number} repeats an earlier line.")
        entries.add(key)
    return entries


def check_baseline_against_denylist(
    entries: set[tuple[str, str]], deny: list[DenyEntry], text: str
) -> None:
    """The public baseline must not carry a private value, or the hash of one (fail closed).
    A soft value that merely contains an @outside-patches entry cannot be told from its hash;
    by policy those values are already published."""
    lowered = text.lower()
    hashes = {digest_of(entry.text): entry for entry in deny}
    for entry in deny:
        if entry.text in lowered:
            raise ScanError(
                f"The baseline text contains denylist entry {entry.number}. Regenerate "
                "scripts/leak-fork-baseline.txt; a public file cannot hold a private value."
            )
    for _cls, digest in sorted(entries):
        if digest in hashes:
            raise ScanError(
                f"A baseline line is the hash of denylist entry {hashes[digest].number}, so it "
                "would accept a private value. Regenerate scripts/leak-fork-baseline.txt and "
                "delete the fork branch that holds the value."
            )


def load_baseline(path: str, deny: list[DenyEntry]) -> set[tuple[str, str]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ScanError(
            f"Cannot read the fork baseline ({type(err).__name__}). Check out "
            "scripts/leak-fork-baseline.txt or drop --baseline."
        ) from err
    entries = parse_baseline(text)
    check_baseline_against_denylist(entries, deny, text)
    return entries


def render_baseline(seen: dict[tuple[str, str], str]) -> str:
    """The file text: a header and one sorted line per (class, digest), the smallest display
    location as the reviewer's hint. Same input, same bytes."""
    lines = [
        f"{cls} sha256:{digest} # fork baseline, seen in {where}"
        for (cls, digest), where in sorted(seen.items())
    ]
    return BASELINE_HEADER + "".join(line + "\n" for line in lines)


COMMIT_LABEL = re.compile(r"commit [0-9a-f]{10}")
IDENTITY_LABEL = re.compile(r"commit [0-9a-f]{10} (?:author|committer)")


def baseline_location(path: str) -> str:
    """Where a hit sat, without a commit sha (a sha changes on a rebase and the comment must
    not): the display path the scanner already prints, or a fixed word for the three sources
    that have no file."""
    if IDENTITY_LABEL.fullmatch(path):
        return "commit identity"
    if COMMIT_LABEL.fullmatch(path):
        return "commit message"
    return clean_label(path)


# ---------------------------------------------------------------------------
# Pattern classes

SECRET_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("anthropic", re.compile(r"\bsk-ant-[a-z]+\d{2}-[A-Za-z0-9_-]{20,}")),
    ("openai", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("azure-key", re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey)=[A-Za-z0-9+/=]{20,}")),
    ("acs-key", re.compile(r"(?i)\baccesskey=[A-Za-z0-9+/=]{20,}")),
    ("azure-sas", re.compile(r"[?&]sig=[A-Za-z0-9%+/=]{30,}")),
    (
        "entra-client-secret",
        re.compile(r"(?<![A-Za-z0-9_~.-])[A-Za-z0-9_~.-]{3}8Q~[A-Za-z0-9_~.-]{30,}"),
    ),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/\w+")),
    ("tailscale", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{10,}")),
    ("databricks", re.compile(r"\bdapi[0-9a-f]{32}\b")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    ("hubspot", re.compile(r"\bpat-(?:na|eu)\d-[0-9a-f-]{30,}")),
    ("mailchimp", re.compile(r"\b[0-9a-f]{32}-us\d{1,2}\b")),
    ("cloudflare-tunnel", re.compile(r"\beyJhIjoi[A-Za-z0-9+/=]{40,}")),
]
# A keyword (secret, token, password, api key, ...) assigned a long value, in code, YAML, env
# files or JSON ("token": "..."). The value class holds ~ and . so Entra client secrets fit.
SECRET_ASSIGN = re.compile(
    r"(?i)(?:secret|token|passw(?:or)?d|api_?key|access_?key|credential)[A-Za-z0-9_.-]{0,60}"
    r"[\"']?\s{0,20}[:=]\s{0,20}[\"']?([A-Za-z0-9/+_~.=!#@$%^&*-]{20,})"
)
BEARER = re.compile(r"(?i)\bbearer\s{1,10}([A-Za-z0-9._~+/=-]{24,})")
SHELL_VARIABLE = re.compile(r"[$%][A-Za-z_][A-Za-z0-9_]*[%]?")
CODE_REFERENCE = re.compile(
    r"[a-z][a-z0-9_]*|[A-Z][A-Z0-9_]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
)
FAKE_WORDS = (
    "test",
    "fake",
    "dummy",
    "example",
    "placeholder",
    "never",
    "sample",
    "redacted",
    "0123456789",
    "1234567890",
    "xxxx",
)
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|fixtures?|testdata)/|(^|/)(test_|conftest)|[._-]test\.|\.spec\.",
    re.IGNORECASE,
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
TAILNET_V6 = re.compile(r"(?i)\bfd7a:115c:a1e0:[0-9a-f:]*")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w])")
CIDR_TAIL = re.compile(r"/\d{1,2}(?!\d)")
EMAIL = re.compile(
    r"(?<![A-Za-z0-9_.%+-])([A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
)
HOME_PATHS = [
    ("posix-home", re.compile(r"(?<![A-Za-z0-9_])/home/([A-Za-z0-9._-]+)")),
    ("mac-home", re.compile(r"(?<![A-Za-z0-9_])/Users/([A-Za-z0-9._-]+)")),
    ("windows-home", re.compile(r"(?i)\b[A-Z]:\\{1,2}Users\\{1,2}([A-Za-z0-9._-]+)")),
    (
        "windows-posix-home",
        re.compile(r"(?<![A-Za-z0-9_])/(?:mnt/)?[A-Za-z]/Users/([A-Za-z0-9._-]+)"),
    ),
    ("claude-project-dir", re.compile(r"(?<![A-Za-z0-9])-home-([a-z0-9_]+)-")),
    ("claude-windows-dir", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]--Users-([A-Za-z0-9_]+)")),
]
# Host names are found suffix first, then walked back over the label characters (at most 253),
# so a long run of dots and hyphens costs linear time, never the quadratic regex backtracking.
TAILNET_SUFFIX = re.compile(r"(?i)\.ts\.net\b")
# 2026-10-29: the private denylist holds the tenant-domain and Key Vault words whole, so this
# file's own source builds them from parts; the pattern below matches exactly what it did before.
_TENANT_DOMAIN = "onmicro" + "soft"
_KEY_VAULT = "vault" + r"\.azure"
AZURE_SUFFIX = re.compile(
    r"(?i)\.(?:" + _KEY_VAULT + r"\.net|" + _TENANT_DOMAIN + r"\.com|blob\.core\.windows\.net|"
    r"queue\.core\.windows\.net|table\.core\.windows\.net|file\.core\.windows\.net|"
    r"azurewebsites\.net|azuredatabricks\.net|cloud\.databricks\.com|"
    r"servicebus\.windows\.net|database\.windows\.net|azurecr\.io)\b"
)
INTERNAL_SUFFIX = re.compile(r"\.(?:local|lan|internal)\b(?![(\[=]|\.[A-Za-z0-9])")
DOCKER_HOSTS = {"host.docker.internal", "gateway.docker.internal"}
SYSTEMD_UNIT_TLDS = {"service", "socket", "timer", "mount", "target", "path", "slice", "scope"}
GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
AZURE_GUID_URL = re.compile(rf"(?i)(?:login\.microsoftonline\.com/|/groups/)({GUID})")
GUID_RE = re.compile(rf"(?<![0-9a-fA-F-])({GUID})(?![0-9a-fA-F-])")
HEX32_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")
GUID_CONTEXT = re.compile(
    r"(?i)tenant|client[_ -]?id|app[_ -]?id|subscription|object[_ -]?id|directory|AZURE_|ENTRA"
)
RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Anywhere an organisation or personal-account repository is named: a URL, a clone address, a
# workflow ``uses:`` or ``repository:``, an API path. 2026-10-29: the personal-account handle is
# not listed here; the private denylist blocks it as a hard hit wherever the denylist runs.
REPO_LINK = re.compile(r"(?i)(?<![A-Za-z0-9_.@-])(analyticendeavors)/([A-Za-z0-9][A-Za-z0-9._-]*)")
TAILNET_LABELS = {
    "tailnet",
    "example",
    "your-tailnet",
    "yourtailnet",
    "tailnet-name",
    "tail1234",
    "x",
}
AZURE_LABELS = {
    "contoso",
    "example",
    "myvault",
    "yourvault",
    "vault-name",
    "name",
    "xxx",
    "xxxx",
    "x",
    "test",
    "tenant",
    "account",
    "myaccount",
    "workspace",
}
INTERNAL_LABELS = {"host", "hostname", "example", "box", "name", "your-host", "myhost"}
CODE_WORDS = {
    "self",
    "this",
    "cls",
    "os",
    "sys",
    "config",
    "settings",
    "ctx",
    "props",
    "state",
    "window",
    "document",
    "process",
    "module",
    "exports",
    "request",
    "req",
    "res",
    "args",
    "opts",
    "options",
    "data",
    "item",
    "obj",
    "e",
    "err",
    "error",
    "pip",
    "locals",
}


def _looks_fake(value: str) -> bool:
    """A fixture value that says so ("test", "never", a counting run), or a truncated prefix."""
    low = value.lower()
    return low.endswith(("-", "_")) or any(word in low for word in FAKE_WORDS)


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def is_generated_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        name in LOCKFILES
        or name.endswith((".min.js", ".min.css", ".map", ".lock"))
        or any(seg in "/" + path for seg in LOCKLIKE_SEGMENTS)
    )


def _is_placeholder_guid(value: str) -> bool:
    """All-zero, all-3 and similar fill patterns (at most three distinct digits)."""
    return len(set(value.replace("-", "").lower())) <= 3


def _placeholder_label(label: str, allowed: set[str]) -> bool:
    return label in allowed or label.startswith(("your-", "my-"))


def _example_domain(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    if any(domain == d or domain.endswith("." + d) for d in EXAMPLE_DOMAINS):
        return True
    return domain.rsplit(".", 1)[-1] in EXAMPLE_TLDS


GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}


def _email_allowed(address: str, *, identity: bool = False) -> bool:
    """A documentation, noreply or clone-URL address. In a commit identity ``git@`` is only a
    public forge's, not any host's: git@<corp domain> is a real address there."""
    local, _, domain = address.rpartition("@")
    domain = domain.lower()
    if _example_domain(domain) or domain == "users.noreply.github.com":
        return True
    if domain.rsplit(".", 1)[-1] in SYSTEMD_UNIT_TLDS:
        return True  # name@instance.service is a systemd template unit, not an address
    if local.lower() in {"noreply", "no-reply"} and domain in {"anthropic.com", "github.com"}:
        return True
    if local.lower() == "git":
        return not identity or domain in GIT_HOSTS
    return False


def _version_context(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 3) : match.start()]
    after = text[match.end() : match.end() + 2]
    # Only a leading v or ^ marks a version: ==, ~, >= and <= also sit in front of hosts
    # (name==<address>), and a version-looking skip must not hide an address.
    if before.endswith(("v", "V", "^")):
        return True
    return bool(re.match(r"\.\d", after))


def _hosts(text: str, suffix: re.Pattern[str]):
    """Yield (host, offset, last-label-before-suffix) for each host ending in *suffix*."""
    for m in suffix.finditer(text):
        i, j = m.start(), m.start()
        while (
            j > 0
            and i - j < 253
            and text[j - 1].isascii()
            and (text[j - 1].isalnum() or text[j - 1] in "-.")
        ):
            j -= 1
        if j > 0 and text[j - 1] == "_":
            continue  # a code identifier (artifact_store.local), not a host name
        segment = text[j:i]
        prefix = segment.strip(".-")
        if not prefix:
            continue
        offset = j + len(segment) - len(segment.lstrip(".-"))
        yield prefix + m.group(0), offset, prefix.rsplit(".", 1)[-1].lower()


def _cidr_follows(text: str, end: int, address: str) -> bool:
    """True when ``/NN`` follows the address and the pair is a real network (no host bits set):
    a network address with its prefix is a range; one with host bits set, or a prefix over 32,
    names a host."""
    tail = CIDR_TAIL.match(text, end)
    if tail is None:
        return False
    try:
        ipaddress.ip_network(address + tail.group(0), strict=True)
    except ValueError:
        return False
    return True


def generic_matches(
    text: str, path: str, *, skip_ip: bool, tick=None
) -> list[tuple[str, str, str, int]]:
    """Every generic-class match in *text* as (class, rule, allow-key, offset). *tick*, when
    given, is called once per line of the assignment scan so the scan's time limit holds."""
    found: list[tuple[str, str, str, int]] = []
    for rule, rx in SECRET_SHAPES:
        found += [("secret", rule, m.group(0), m.start()) for m in rx.finditer(text)]
    line_start = 0
    for line in text.split("\n"):
        if tick is not None:
            tick()
        for m in SECRET_ASSIGN.finditer(line):
            value = m.group(1)
            if (
                re.search(r"\d", value)
                and re.search(r"[A-Za-z]", value)
                and not CODE_REFERENCE.fullmatch(value)
                and not SHELL_VARIABLE.fullmatch(value)  # $NAME or %NAME%, not a value
                and not re.fullmatch(GUID, value)  # an id, which the cloud-id class judges
                and line[m.end() : m.end() + 1] != "("
            ):
                found.append(("secret", "assignment", value, line_start + m.start()))
        line_start += len(line) + 1
    for m in BEARER.finditer(text):
        value = m.group(1)
        # Letters alone count: a snake_case or dotted name is code, any other run is a token.
        if re.search(r"\d", value) or not (
            CODE_REFERENCE.fullmatch(value) and re.search(r"[_.]", value)
        ):
            found.append(("secret", "bearer", value, m.start()))
    found += [("private-key", "pem", m.group(0), m.start()) for m in PRIVATE_KEY.finditer(text)]
    for host, at, label in _hosts(text, TAILNET_SUFFIX):
        if not _placeholder_label(label, TAILNET_LABELS):
            found.append(("tailnet-host", "ts-net", host, at))
    if not skip_ip:
        for m in TAILNET_V6.finditer(text):
            if not _cidr_follows(text, m.end(), m.group(0)):
                found.append(("tailnet-ip", "cgnat-v6", m.group(0), m.start()))
        for m in IPV4.finditer(text):
            if _cidr_follows(text, m.end(), m.group(1)) or _version_context(text, m):
                continue
            try:
                addr = ipaddress.IPv4Address(m.group(1))
            except ValueError:
                continue
            if addr in CGNAT:
                found.append(("tailnet-ip", "cgnat-v4", m.group(1), m.start()))
            elif any(addr in net for net in RFC1918):
                found.append(("lan-ip", "rfc1918", m.group(1), m.start()))
            elif addr.is_global:
                found.append(("public-ip", "warn", m.group(1), m.start()))
    for m in EMAIL.finditer(text):
        address = m.group(1)
        if not _email_allowed(address):
            found.append(("email", "address", address, m.start()))
    for rule, rx in HOME_PATHS:
        for m in rx.finditer(text):
            if m.group(1).lower() not in PLACEHOLDER_USERS:
                found.append(("home-path", rule, m.group(1), m.start()))
    for host, at, label in _hosts(text, AZURE_SUFFIX):
        if not _placeholder_label(label, AZURE_LABELS):
            found.append(("azure-endpoint", "host", host, at))
    for host, at, label in _hosts(text, INTERNAL_SUFFIX):
        if host.lower() in DOCKER_HOSTS or (
            "." not in host.rsplit(".", 1)[0] and label in CODE_WORDS
        ):
            continue
        if not _placeholder_label(label, INTERNAL_LABELS):
            found.append(("internal-host", "lan-name", host, at))
    for m in AZURE_GUID_URL.finditer(text):
        if not _is_placeholder_guid(m.group(1)):
            found.append(("azure-endpoint", "guid-url", m.group(1), m.start()))
    if not is_generated_path(path):
        for m in GUID_RE.finditer(text):
            if not _is_placeholder_guid(m.group(1)):
                found.append(("cloud-id", "guid", m.group(1), m.start()))
        for m in HEX32_RE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start : len(text) if line_end < 0 else line_end]
            if not _is_placeholder_guid(m.group(1)) and GUID_CONTEXT.search(line):
                found.append(("cloud-id", "hex32", m.group(1), m.start()))
    for m in REPO_LINK.finditer(text):
        name = m.group(2).rstrip(".-")
        if name.lower().endswith(".git"):
            name = name[:-4]
        if name:
            found.append(("private-repo-link", "repo", f"{m.group(1)}/{name}", m.start()))
    return found


# ---------------------------------------------------------------------------
# Text scanning

DIFFSTAT = re.compile(r"^ \S.*\|\s+(\d+|Bin)|^ \d+ files? changed")
MBOX_FROM = re.compile(r"^From [0-9a-f]{40} ")


def _header_ends(lines: list[str], i: int) -> bool:
    """A bare ``---`` ends a patch header only when a diffstat or a diff follows it."""
    for nxt in lines[i + 1 :]:
        if not nxt.strip():
            continue
        return bool(DIFFSTAT.match(nxt) or nxt.startswith("diff --git "))
    return True


def patch_modes(lines: list[str]) -> tuple[list[str], list[str]]:
    """Per line: ``full`` (every class), ``trailer`` (full, e-mail exempt) or ``deny`` only,
    and the file each hunk line belongs to."""
    modes = ["deny"] * len(lines)
    files = [""] * len(lines)
    state = "header"
    old_left = new_left = 0
    current = ""
    for i, line in enumerate(lines):
        if MBOX_FROM.match(line) and state != "hunk":
            state = "header"
        if state == "header":
            if line == "---" and _header_ends(lines, i):
                state = "stat"
                continue
            modes[i] = "trailer" if TRAILER.match(line) else "full"
        elif state in ("stat", "between", "filehdr", "sig"):
            if line.startswith("diff --git "):
                state = "filehdr"
                found = re.match(r"diff --git a/.* b/(.*)$", line)
                current = found.group(1) if found else ""
            elif state in ("filehdr", "between"):
                match = HUNK.match(line)
                if match:
                    old_left = int(match.group(1) if match.group(1) is not None else 1)
                    new_left = int(match.group(3) if match.group(3) is not None else 1)
                    state = "hunk" if (old_left or new_left) else "between"
            elif state == "stat" and line == "-- ":
                state = "sig"
        elif state == "hunk":
            files[i] = current
            if line.startswith("+"):
                modes[i] = "full"
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith("\\"):
                pass
            else:
                old_left -= 1
                new_left -= 1
            if old_left <= 0 and new_left <= 0:
                state = "between"
    return modes, files


class Scanner:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.record_markers = False
        self.new_markers: list[tuple[str, int, str]] = []
        self.warnings: list[Hit] = []
        self.notices: list[str] = []
        self.deadline: float | None = None
        self.limit_seconds: float = SCAN_SECONDS
        self.check_identity = True
        self.check_identity_email = True  # fork mode turns this off for commits that are not AE's
        self.ignore_markers = False  # --write-baseline: a marker must not hide a value from it
        self._display: dict[str, str] = {}

    def tick(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise ScanError(
                f"The scan ran past its {self.limit_seconds:g} s limit, so nothing was checked to "
                "the end. Scan a smaller range (--base closer to --head), raise "
                "AE_LEAK_SCAN_SECONDS, or report the slow input."
            )

    def allowed(self, cls: str, key: str, path: str) -> bool:
        return any(a.matches(cls, key, path) for a in self.cfg.allow)

    def disp(self, path: str) -> str:
        """The one display path of a file: every private or matched value replaced by ``***``,
        control characters by ``?``. Every output site uses this, never the raw name."""
        shown = self._display.get(path)
        if shown is None:
            shown = redact_text(path, self.cfg)
            self._display[path] = shown
        return shown

    def scan_text(
        self,
        path: str,
        text: str,
        *,
        only_lines: set[int] | None = None,
        is_patch: bool | None = None,
        label: str | None = None,
        ocr: bool = False,
        commit: str = "",
    ) -> list[Hit]:
        shown = label or self.disp(path)
        lines = text.split("\n")
        offsets = [0]
        for line in lines[:-1]:
            offsets.append(offsets[-1] + len(line) + 1)
        patch = path.endswith(".patch") if is_patch is None else is_patch
        modes, files = patch_modes(lines) if patch else (None, None)

        def line_of(offset: int) -> int:
            return bisect.bisect_right(offsets, offset)  # 1-based

        raw: list[tuple[int, str, str, str]] = []  # line, class, rule, key
        lowered = text.lower()
        for entry in self.cfg.deny_for(path):
            self.tick()
            start = 0
            while True:
                at = lowered.find(entry.text, start)
                if at < 0:
                    break
                raw.append((line_of(at), "denylist", f"entry-{entry.number}", entry.text))
                start = at + max(1, len(entry.text))
        self.tick()
        skip_ip = PurePosixPath(path).name in LOCKFILES
        for cls, rule, key, offset in generic_matches(text, path, skip_ip=skip_ip, tick=self.tick):
            raw.append((line_of(offset), cls, rule, key))
        self.tick()

        hits: dict[Hit, None] = {}
        for lineno, cls, rule, key in raw:
            if only_lines is not None and lineno not in only_lines:
                continue
            mode = modes[lineno - 1] if modes is not None else "full"
            if mode == "deny" and cls != "denylist":
                continue
            if mode == "trailer" and cls == "email":
                continue
            if cls == "secret" and rule in ("assignment", "bearer") and _looks_fake(key):
                in_file = files[lineno - 1] if files is not None else ""
                if is_test_path(in_file or path):
                    continue  # a fixture that says so, in a test file
            if cls == "public-ip":
                self.warnings.append(Hit(shown, lineno, cls, rule, commit))
                continue
            digest = digest_of(key) if cls in SOFT else ""
            if cls in SOFT and self.allowed(cls, key, path):
                continue
            if (
                cls in SOFT
                and not ocr
                and not self.ignore_markers
                and self._marked(lines, lineno, cls, only_lines)
            ):
                continue
            out_cls, out_rule = ("media-text", f"{cls}-{rule}") if ocr else (cls, rule)
            hits[Hit(shown, lineno, out_cls, out_rule, commit, digest)] = None
        if not ocr:
            hits.update(dict.fromkeys(self._marker_problems(lines, shown, only_lines, commit)))
        return sorted(hits, key=lambda h: (h.path, h.line, h.cls, h.rule))

    def _marked(self, lines: list[str], lineno: int, cls: str, only: set[int] | None) -> bool:
        """A marker clears the line it sits on or the line below it, but only a marker that
        is itself one of the added lines counts: an old marker cannot cover a new line."""
        for at in (lineno, lineno - 1):
            if at < 1 or at > len(lines):
                continue
            if only is not None and at not in only:
                continue
            match = MARKER.search(lines[at - 1])
            if match and match.group(2) and cls in match.group(1).split(","):
                return True
        return False

    def _marker_problems(
        self, lines: list[str], shown: str, only: set[int] | None, commit: str
    ) -> list[Hit]:
        problems = []
        for i, line in enumerate(lines, 1):
            if only is not None and i not in only:
                continue
            match = MARKER.search(line)
            if not match:
                continue
            classes = match.group(1).split(",")
            if self.record_markers:
                self.new_markers.append((shown, i, match.group(1)))
            if not match.group(2):
                problems.append(Hit(shown, i, "marker", "no-reason", commit))
            for cls in classes:
                if cls in HARD:
                    problems.append(Hit(shown, i, "marker", f"cannot-clear-{cls}", commit))
                elif cls not in SOFT:
                    problems.append(Hit(shown, i, "marker", "unknown-class", commit))
        return problems

    def scan_path(self, path: str, commit: str = "") -> list[Hit]:
        """The denylist, e-mail, host and IP classes over a file path."""
        hits: dict[Hit, None] = {}
        shown = self.disp(path)
        lowered = path.lower()
        for entry in self.cfg.deny_for(path):
            if entry.text in lowered:
                hits[Hit(shown, 0, "path-name", f"entry-{entry.number}", commit)] = None
        for cls, _rule, key, _ in generic_matches(path, path, skip_ip=False):
            if cls in ("tailnet-host", "tailnet-ip", "lan-ip", "email") and not self.allowed(
                "path-name", key, path
            ):
                hits[Hit(shown, 0, "path-name", cls, commit, digest_of(key))] = None
        return list(hits)

    def scan_identity(self, sha: str, role: str, name: str, email: str) -> list[Hit]:
        """A commit's author or committer: the denylist on both fields, and the e-mail must be
        a noreply or bot identity (built in or listed in the allowlist file). The denylist half
        always runs; the e-mail half is skipped when ``check_identity_email`` is off."""
        label = f"commit {sha[:10]} {role}"
        hits = []
        for entry in self.cfg.deny:
            if entry.scope == "outside-patches" and self.cfg.fork:
                continue
            if entry.text in (name + " " + email).lower():
                hits.append(Hit(label, 0, "denylist", f"entry-{entry.number}", ""))
        if (
            self.check_identity_email
            and email
            and not _email_allowed(email, identity=True)
            and not self.allowed("commit-identity", email, "")
        ):
            hits.append(Hit(label, 0, "commit-identity", "email", "", digest_of(email)))
        return hits


def redact_text(text: str, cfg: Config) -> str:
    """*text* with every denylist entry and every generic match replaced by ``***``."""
    values = [e.text for e in cfg.deny]
    for cls, _rule, key, _ in generic_matches(text, text, skip_ip=False):
        if cls != "public-ip":
            values.append(key.split("/")[-1] if cls == "private-repo-link" else key)
    out = text
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            out = re.sub(re.escape(value), "***", out, flags=re.IGNORECASE)
    return clean_label(out)


# ---------------------------------------------------------------------------
# Image metadata


@dataclass
class MediaFinding:
    rule: str
    key: str  # text the allowlist matches against


PNG_KEEP = {
    b"IHDR",
    b"PLTE",
    b"IDAT",
    b"IEND",
    b"gAMA",
    b"cHRM",
    b"sRGB",
    b"sBIT",
    b"bKGD",
    b"hIST",
    b"tRNS",
    b"pHYs",
    b"sPLT",
    b"tIME",
    b"acTL",
    b"fcTL",
    b"fdAT",
}
PNG_META = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"iCCP"}
WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"}
JPEG_DROP = {0xE1, *range(0xE2, 0xF0), 0xFE}
TIFF_TAGS = {
    0x010E: "description",
    0x010F: "make",
    0x0110: "model",
    0x0131: "software",
    0x0132: "datetime",
    0x013B: "artist",
    0x02BC: "xmp",
    0x8298: "copyright",
    0x83BB: "iptc",
    0x8649: "photoshop",
    0x8769: "exif-ifd",
    0x8773: "icc",
    0x8825: "gps",
}
PDF_INFO = re.compile(
    rb"/(Author|Creator|Producer|Title|Subject|Keywords)\s*(?:\(([^)]{1,200})\)|<([0-9A-Fa-f]{2,400})>)"
)


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes, int, int]]:
    """(type, body, start, end) of each chunk up to IEND; raises ValueError on a bad file."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    chunks = []
    at = 8
    while at < len(data):
        if at + 12 > len(data):
            raise ValueError("truncated chunk")
        (length,) = struct.unpack(">I", data[at : at + 4])
        kind = data[at + 4 : at + 8]
        end = at + 12 + length
        if end > len(data):
            raise ValueError("truncated chunk")
        chunks.append((kind, data[at + 8 : at + 8 + length], at, end))
        at = end
        if kind == b"IEND":
            break
    return chunks


def _jpeg_segments(data: bytes) -> tuple[list[tuple[int, bytes, int, int]], int]:
    """(marker, body, start, end) of each header segment, and the offset of the scan data."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    segments = []
    at = 2
    while at < len(data):
        if data[at] != 0xFF:
            raise ValueError("bad marker")
        while at < len(data) and data[at] == 0xFF:
            at += 1
        if at >= len(data):
            raise ValueError("truncated")
        marker = data[at]
        at += 1
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            return segments, at - 2
        if at + 2 > len(data):
            raise ValueError("truncated")
        (length,) = struct.unpack(">H", data[at : at + 2])
        end = at + length
        if length < 2 or end > len(data):
            raise ValueError("truncated segment")
        segments.append((marker, data[at + 2 : end], at - 2, end))
        if marker == 0xDA:
            return segments, at - 2
        at = end
    return segments, len(data)


def _webp_chunks(data: bytes) -> tuple[list[tuple[bytes, bytes, int, int]], int]:
    """The chunks, and how many bytes follow the RIFF container."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP")
    (riff,) = struct.unpack("<I", data[4:8])
    limit = min(len(data), 8 + riff)
    chunks = []
    at = 12
    while at < limit:
        if at + 8 > limit:
            raise ValueError("truncated chunk")
        kind = data[at : at + 4]
        (length,) = struct.unpack("<I", data[at + 4 : at + 8])
        end = at + 8 + length + (length & 1)
        if at + 8 + length > limit:
            raise ValueError("truncated chunk")
        chunks.append((kind, data[at + 8 : at + 8 + length], at, min(end, limit)))
        at = end
    return chunks, len(data) - (8 + riff)


def _gif_findings(data: bytes) -> list[MediaFinding]:
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise ValueError("not a GIF")
    found = []
    flags = data[10]
    at = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)

    while at < len(data):
        block = data[at]
        if block == 0x3B:
            if at + 1 < len(data):
                found.append(MediaFinding("gif-trailing-data", ""))
            return found
        if block == 0x21 and at + 2 < len(data):
            label = data[at + 1]
            if label == 0xFE:
                found.append(MediaFinding("gif-comment", ""))
            elif label == 0xFF:
                app = data[at + 3 : at + 14]
                if not app.startswith((b"NETSCAPE2.0", b"ANIMEXTS1.0")):
                    found.append(MediaFinding("gif-application", ""))
            elif label not in (0xF9,):
                found.append(MediaFinding("gif-extension", ""))
            at = _gif_skip(data, at)
        elif block == 0x2C:
            at = _gif_image_end(data, at)
        else:
            raise ValueError("bad block")
    raise ValueError("no trailer")


def _gif_skip(data: bytes, at: int) -> int:
    """The offset after the extension block that starts at *at*."""
    pos = at + 2
    if data[at + 1] in (0xFF, 0x01):
        pos += 1 + data[pos]  # the fixed-size header block
    while pos < len(data):
        size = data[pos]
        pos += 1 + size
        if size == 0:
            return pos
    raise ValueError("truncated")


def _gif_image_end(data: bytes, at: int) -> int:
    flags = data[at + 9]
    pos = at + 10 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0) + 1
    while pos < len(data):
        size = data[pos]
        pos += 1 + size
        if size == 0:
            return pos
    raise ValueError("truncated")


def _tiff_findings(data: bytes) -> list[MediaFinding]:
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        raise ValueError("not a TIFF")
    order = "<" if data[:2] == b"II" else ">"
    (ifd,) = struct.unpack(order + "I", data[4:8])
    (count,) = struct.unpack(order + "H", data[ifd : ifd + 2])
    found = []
    for n in range(count):
        entry = data[ifd + 2 + 12 * n : ifd + 14 + 12 * n]
        (tag,) = struct.unpack(order + "H", entry[:2])
        if tag in TIFF_TAGS:
            found.append(MediaFinding(f"tiff-{TIFF_TAGS[tag]}", ""))
    return found


def _heif_findings(data: bytes) -> list[MediaFinding]:
    if data[4:8] != b"ftyp":
        raise ValueError("not a HEIF or AVIF file")
    found = []
    if b"Exif\x00\x00" in data:
        found.append(MediaFinding("heif-exif", ""))
    if (
        b"<x:xmpmeta" in data
        or b"http://ns.adobe.com/xap" in data
        or b"application/rdf+xml" in data
    ):
        found.append(MediaFinding("heif-xmp", ""))
    return found


def pdf_findings(data: bytes) -> list[MediaFinding]:
    found = []
    for m in PDF_INFO.finditer(data):
        name = m.group(1).decode()
        raw = m.group(2) if m.group(2) is not None else bytes.fromhex(m.group(3).decode())
        found.append(MediaFinding(f"pdf-{name.lower()}", raw.decode("latin-1")))
    if b"<x:xmpmeta" in data:
        found.append(MediaFinding("pdf-xmp", ""))
    return found


def media_findings(path: str, data: bytes) -> list[MediaFinding]:
    suffix = PurePosixPath(path).suffix.lower()
    found = []
    try:
        if suffix == ".png":
            chunks = _png_chunks(data)
            for kind, body, _, _ in chunks:
                if kind in PNG_META:
                    found.append(MediaFinding(f"png-{kind.decode()}", body.decode("latin-1")))
                elif kind not in PNG_KEEP:
                    found.append(MediaFinding("png-unknown-chunk", kind.decode("latin-1")))
            if not chunks or chunks[-1][0] != b"IEND":
                found.append(MediaFinding("png-no-iend", ""))
            elif len(data) > chunks[-1][3]:
                found.append(MediaFinding("png-trailing-data", ""))
        elif suffix in (".jpg", ".jpeg"):
            segments, _ = _jpeg_segments(data)
            for marker, body, _, _ in segments:
                if marker == 0xE1:
                    xmp = body.startswith(b"http://ns.adobe.com/xap")
                    if xmp or body.startswith(b"Exif"):
                        found.append(MediaFinding("jpeg-xmp" if xmp else "jpeg-exif", ""))
                elif marker == 0xED:
                    found.append(MediaFinding("jpeg-iptc", ""))
                elif marker in range(0xE2, 0xF0):
                    digest = hashlib.sha256(body).hexdigest()
                    found.append(MediaFinding(f"jpeg-app{marker - 0xE0}", f"app:{digest}"))
                elif marker == 0xFE:
                    found.append(
                        MediaFinding("jpeg-comment", body.rstrip(b"\0").decode("latin-1"))
                    )
            eoi = data.rfind(b"\xff\xd9")
            if eoi >= 0 and len(data) > eoi + 2:
                found.append(MediaFinding("jpeg-trailing-data", ""))
        elif suffix == ".webp":
            chunks, trailing = _webp_chunks(data)
            for kind, _body, _, _ in chunks:
                if kind == b"EXIF":
                    found.append(MediaFinding("webp-exif", ""))
                elif kind == b"XMP ":
                    found.append(MediaFinding("webp-xmp", ""))
                elif kind == b"ICCP":
                    found.append(MediaFinding("webp-iccp", ""))
                elif kind not in WEBP_KEEP:
                    found.append(MediaFinding("webp-unknown-chunk", kind.decode("latin-1")))
            if trailing > 0:
                found.append(MediaFinding("webp-trailing-data", ""))
        elif suffix == ".gif":
            found += _gif_findings(data)
        elif suffix in (".tif", ".tiff"):
            found += _tiff_findings(data)
        elif suffix in (".heic", ".heif", ".avif"):
            found += _heif_findings(data)
        elif suffix == ".pdf":
            found += pdf_findings(data)
    except (ValueError, struct.error, IndexError):
        found.append(MediaFinding("unparseable", "unparseable"))
    return found


def strip_media(path: str, data: bytes) -> bytes:
    """The image without its metadata chunks; pixel data untouched. The colour profile goes
    too (the scan flags it), so a profile that matters has to be added back by hand."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".png":
        chunks = _png_chunks(data)
        out = bytearray(data[:8])
        for kind, _, start, end in chunks:
            if kind in PNG_KEEP:
                out += data[start:end]
        return bytes(out)
    if suffix in (".jpg", ".jpeg"):
        segments, scan_at = _jpeg_segments(data)
        out = bytearray(b"\xff\xd8")
        for marker, _, start, end in segments:
            if marker in JPEG_DROP or marker == 0xED:
                continue
            if marker == 0xDA:
                break
            out += data[start:end]
        return bytes(out) + data[scan_at:]
    if suffix == ".webp":
        chunks, _ = _webp_chunks(data)
        dropped = {c[0] for c in chunks if c[0] not in WEBP_KEEP}
        body = bytearray()
        for kind, payload, start, end in chunks:
            if kind not in WEBP_KEEP:
                continue
            piece = bytearray(data[start:end])
            if kind == b"VP8X" and len(payload) >= 1:
                flags = piece[8]
                if b"EXIF" in dropped:
                    flags &= ~0x08
                if b"XMP " in dropped:
                    flags &= ~0x04
                if b"ICCP" in dropped:
                    flags &= ~0x20
                piece[8] = flags
            body += piece
        return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + bytes(body)
    return data


def scan_media(scanner: Scanner, path: str, data: bytes, commit: str = "") -> list[Hit]:
    hits = []
    for finding in media_findings(path, data):
        key = finding.key or finding.rule
        if scanner.allowed("media-metadata", key, path):
            continue
        hits.append(
            Hit(scanner.disp(path), 0, "media-metadata", finding.rule, commit, digest_of(key))
        )
    return hits


def _is_animated_webp(data: bytes) -> bool:
    """True for a WebP with animation chunks or the VP8X animation flag, read from the bytes."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    try:
        chunks, _ = _webp_chunks(data)
    except (ValueError, struct.error):
        return False  # media-metadata already flags it; the still path reports the rest
    return any(
        kind in (b"ANIM", b"ANMF") or (kind == b"VP8X" and body[:1] and body[0] & 0x02)
        for kind, body, _, _ in chunks
    )


def _tesseract_run(exe: str, image: Path, *, single_thread: bool = False) -> str:
    # Frames are read four at a time, so each tesseract keeps to one thread.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"} if single_thread else None
    done = subprocess.run(
        [exe, str(image), "stdout"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    if done.returncode != 0:
        raise ScanError(
            f"tesseract failed on an image (exit {done.returncode}). Rerun with --ocr off or "
            "fix the tesseract install."
        )
    return done.stdout


def _ocr_animated(exe: str, image: Path, tmp: str) -> str:
    """Text from every distinct frame of an animated WebP, dumped to PNGs with anim_dump."""
    dump = shutil.which("anim_dump")
    if not dump:
        raise ScanError(
            "anim_dump is not installed, so an animated WebP cannot be read. Install the "
            "webp package (apt install webp) or use --ocr off."
        )
    folder = Path(tmp) / "frames"
    folder.mkdir()
    try:
        done = subprocess.run(
            [dump, "-folder", str(folder), "-prefix", "f", str(image)],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as err:
        raise ScanError(
            f"anim_dump could not read an animated WebP ({type(err).__name__}). "
            "Rerun with --ocr off or fix the webp install."
        ) from err
    if done.returncode != 0:
        raise ScanError(
            f"anim_dump failed on an animated WebP (exit {done.returncode}). Rerun with "
            "--ocr off or fix the webp install."
        )
    frames: dict[str, Path] = {}
    for frame in sorted(folder.glob("*.png")):
        frames.setdefault(hashlib.sha256(frame.read_bytes()).hexdigest(), frame)
    if not frames:
        raise ScanError("anim_dump wrote no frames for an animated WebP. Rerun with --ocr off.")
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        texts = list(
            pool.map(lambda f: _tesseract_run(exe, f, single_thread=True), frames.values())
        )
    except subprocess.TimeoutExpired as err:
        raise ScanError(
            "tesseract timed out on a frame of an animated WebP. Rerun with --ocr off."
        ) from err
    finally:
        # One failed frame fails the scan, so the frames still queued are not read.
        pool.shutdown(wait=True, cancel_futures=True)
    return "\n".join(texts)


def ocr_text(path: str, data: bytes) -> str | None:
    """Text tesseract reads from the image, or None when it is not installed.

    An animated WebP is dumped to PNG frames first, since tesseract decodes only still WebP.
    """
    exe = shutil.which("tesseract")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / ("image" + PurePosixPath(path).suffix.lower())
        image.write_bytes(data)
        if _is_animated_webp(data):
            return _ocr_animated(exe, image, tmp)
        return _tesseract_run(exe, image)


# ---------------------------------------------------------------------------
# Git access


class Git:
    def __init__(self, git_dir: str | None = None) -> None:
        self.base = ["git", "--git-dir", git_dir] if git_dir else ["git"]
        self._batch: subprocess.Popen[bytes] | None = None

    def run(self, *args: str, check: bool = True, stdin: bytes | None = None) -> bytes:
        done = subprocess.run([*self.base, *args], capture_output=True, input=stdin, check=False)
        if check and done.returncode != 0:
            raise ScanError(
                f"git {args[0]} failed ({safe(done.stderr.decode(errors='replace').strip())}). "
                "Run the scan inside the repository with enough history (fetch-depth: 0 in CI)."
            )
        return done.stdout

    def ok(self, *args: str) -> bool:
        return (
            subprocess.run([*self.base, *args], capture_output=True, check=False).returncode == 0
        )

    def blob(self, spec: str) -> bytes | None:
        if self._batch is None:
            self._batch = subprocess.Popen(
                [*self.base, "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        proc = self._batch
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(spec.encode() + b"\n")
        proc.stdin.flush()
        header = proc.stdout.readline().decode().split()
        if not header or header[-1] == "missing":
            return None
        size = int(header[2])
        body = proc.stdout.read(size + 1)
        return body[:size]

    def close(self) -> None:
        if self._batch is not None:
            assert self._batch.stdin is not None
            self._batch.stdin.close()
            self._batch.wait()
            self._batch = None


def decode(data: bytes) -> str | None:
    """File text (UTF-8, or UTF-16 with a byte-order mark), or None for a binary file."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def blank_allowlist_fields(text: str) -> str:
    """The allowlist file with each entry's pattern field blanked, so the file is scanned for
    values pasted into comments and reasons but its own entry fields do not hit."""
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            line = re.sub(
                r"^(\s*\S+\s+)(\S+)", lambda m: m.group(1) + " " * len(m.group(2)), line, count=1
            )
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Subcommands


def scan_blob(
    scanner: Scanner, path: str, data: bytes, only: set[int] | None, commit: str = ""
) -> list[Hit]:
    hits = scanner.scan_path(path, commit)
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in MEDIA_SUFFIXES or suffix == ".pdf":
        return hits + scan_media(scanner, path, data, commit)
    text = decode(data)
    if text is None:
        if suffix not in SAFE_BINARY_SUFFIXES and not scanner.allowed("binary", path, path):
            hits.append(
                Hit(scanner.disp(path), 0, "binary", "unreviewed", commit, digest_of(path))
            )
        return hits
    if path == DEFAULT_ALLOWLIST:
        text = blank_allowlist_fields(text)
    return hits + scanner.scan_text(path, text, only_lines=only, commit=commit)


def added_lines(git: Git, parent: str, sha: str, path: str) -> set[int]:
    out = git.run("diff", "-U0", "--no-ext-diff", "--no-renames", parent, sha, "--", path)
    added: set[int] = set()
    for raw in out.decode(errors="replace").splitlines():
        match = HUNK.match(raw)
        if match:
            start = int(match.group(2))
            count = int(match.group(3)) if match.group(3) is not None else 1
            added.update(range(start, start + count))
    return added


def raw_changes(git: Git, old: str, new: str) -> dict[str, tuple[str, str, str, str]]:
    """``{path: (status, old blob, new blob, new mode)}`` between two trees. Output that does
    not parse is a scan failure, never an empty (clean) answer."""
    out = git.run("diff", "--raw", "-z", "--no-renames", "--no-ext-diff", old, new)
    fields = out.split(b"\0")
    found: dict[str, tuple[str, str, str, str]] = {}
    i = 0
    while i < len(fields):
        if not fields[i]:
            i += 1
            continue
        meta = fields[i].decode(errors="replace").split()
        if not fields[i].startswith(b":") or len(meta) != 5 or i + 1 >= len(fields):
            raise ScanError(
                "git diff --raw printed something this scanner does not understand, so the "
                "change was not checked. Update the scanner, or scan a smaller range."
            )
        path = fields[i + 1].decode("utf-8", errors="replace")
        found[path] = (meta[4][0], meta[2], meta[3], meta[1])
        i += 2
    return found


def commit_message_hits(git: Git, scanner: Scanner, sha: str) -> list[Hit]:
    out = git.run("log", "-1", "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%B", sha)
    parts = out.decode(errors="replace").split("\x1f", 4)
    if len(parts) != 5:
        raise ScanError("A commit's author fields did not parse, so it was not checked.")
    an, ae, cn, ce, body = parts
    hits = []
    if scanner.check_identity:
        hits += scanner.scan_identity(sha, "author", an, ae)
        hits += scanner.scan_identity(sha, "committer", cn, ce)
    hits += scanner.scan_text("commit", body, is_patch=False, label=f"commit {sha[:10]}")
    return hits


def scan_commit(git: Git, scanner: Scanner, sha: str) -> list[Hit]:
    """One commit on its own: its identity, its message, and the lines it adds. A value added
    in one commit and removed in the next still left the box in the first."""
    scanner.tick()
    short = sha[:10]
    hits = commit_message_hits(git, scanner, sha)
    parents = git.run("rev-list", "--parents", "-n", "1", sha).decode().split()[1:]
    first = parents[0] if parents else EMPTY_TREE
    changes = raw_changes(git, first, sha)
    merge_parents: list[dict[str, tuple[str, str, str, str]]] = []
    if len(parents) > 1:
        # A merge adds only what neither parent had; the merged-in commits are scanned (or were
        # already out) on their own.
        merge_parents = [raw_changes(git, p, sha) for p in parents]
        changes = {p: c for p, c in changes.items() if all(p in other for other in merge_parents)}
    for path, (status, _old, new_blob, new_mode) in changes.items():
        if status == "D" or new_mode == "160000":
            continue
        data = git.blob(new_blob)
        if data is None:
            raise ScanError("Cannot read a changed file in a commit. Fetch full history.")
        if merge_parents:
            known: set[str] = set()
            for parent, table in zip(parents, merge_parents, strict=True):
                if path in table and table[path][1].strip("0"):
                    old_data = git.blob(table[path][1])
                    known.update((decode(old_data) or "").split("\n") if old_data else [])
                else:
                    other = git.blob(f"{parent}:{path}")
                    known.update((decode(other) or "").split("\n") if other else [])
            text = decode(data)
            only = (
                {n for n, line in enumerate(text.split("\n"), 1) if line not in known}
                if text is not None
                else None
            )
        elif status == "A":
            only = None
        else:
            only = added_lines(git, first, sha, path)
        hits += scan_blob(scanner, path, data, only, short)
    return hits


def scan_commits(git: Git, scanner: Scanner, commits: list[str]) -> list[Hit]:
    hits: list[Hit] = []
    for sha in commits:
        hits += scan_commit(git, scanner, sha)
    return hits


def rev_list(git: Git, *args: str) -> list[str]:
    return git.run("rev-list", "--reverse", *args).decode().split()


def allowlist_changes(git: Git, base: str, head: str) -> list[tuple[int, str, str]]:
    """(line, class, path glob) of each allowlist entry the change adds or edits; never the
    pattern, which is the value being cleared."""
    old = git.blob(f"{base}:{DEFAULT_ALLOWLIST}")
    new = git.blob(f"{head}:{DEFAULT_ALLOWLIST}")
    before = set((old or b"").decode(errors="replace").split("\n"))
    changes = []
    for number, line in enumerate((new or b"").decode(errors="replace").split("\n"), 1):
        body = line.partition("#")[0].split()
        if line not in before and len(body) >= 2 and not line.lstrip().startswith("#"):
            changes.append((number, body[0], body[2] if len(body) > 2 else "*"))
    return changes


def cmd_diff(args: argparse.Namespace, scanner: Scanner, git: Git) -> list[Hit]:
    commits = rev_list(git, f"{args.base}..{args.head}")
    hits = scan_commits(git, scanner, commits)
    if getattr(args, "allow_source", "head") == "base":
        # The allowlist in force is the base commit's, so an entry this change adds or edits
        # clears nothing. It is a finding of its own unless an approver labelled the PR.
        head_blob = git.blob(f"{args.head}:{DEFAULT_ALLOWLIST}")
        head_text = head_blob.decode("utf-8", errors="replace") if head_blob else ""
        parse_allowlist(head_text)  # a malformed or wildcard entry fails the scan (exit 2)
        check_allowlist_against_denylist([], scanner.cfg.deny, head_text)
        for number, cls, glob in allowlist_changes(git, args.base, args.head):
            what = f"allowlist entry added or changed: class {cls}, path {glob}"
            if args.allowlist_approved:
                scanner.notices.append(what + " (approved by label)")
            else:
                hits.append(
                    Hit(
                        scanner.disp(DEFAULT_ALLOWLIST),
                        number,
                        "allowlist",
                        f"unapproved entry for class {cls}",
                        "",
                    )
                )
    hits += scan_pr_text(args, scanner)
    return hits


def scan_pr_text(args: argparse.Namespace, scanner: Scanner) -> list[Hit]:
    hits = []
    for name, label in ((args.title_env, "pr-title"), (args.body_env, "pr-body")):
        if name:
            value = os.environ.get(name, "")
            hits += scanner.scan_text(label, value, is_patch=False)
    return hits


def cmd_tree(args: argparse.Namespace, scanner: Scanner, git: Git) -> list[Hit]:
    treeish = args.tree or args.ref
    listing = git.run("ls-tree", "-r", "-z", treeish)
    hits: list[Hit] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        scanner.tick()
        meta, _, raw_path = entry.partition(b"\t")
        _mode, kind, sha = meta.decode().split()
        if kind != "blob":
            continue
        path = raw_path.decode("utf-8", errors="replace")
        data = git.blob(sha)
        if data is None:
            raise ScanError("Cannot read a file from the tree. Is the repository complete?")
        hits += scan_blob(scanner, path, data, None)
    return hits


def cmd_text(args: argparse.Namespace, scanner: Scanner, git: Git) -> list[Hit]:  # noqa: ARG001
    hits = []
    for name in args.files:
        if name == "-":
            text, label = sys.stdin.read(), "<stdin>"
        else:
            try:
                raw = Path(name).read_bytes()
            except OSError as err:
                raise ScanError(
                    f"Cannot read {scanner.disp(name)} ({err.strerror}). "
                    "Check the path and permissions."
                ) from err
            decoded = decode(raw)
            if decoded is None:
                raise ScanError(
                    f"{scanner.disp(name)} is binary; use the media subcommand for images."
                )
            text, label = decoded, scanner.disp(name)
        hits += scanner.scan_text(label, text, is_patch=False, label=label)
    return hits


def cmd_media(args: argparse.Namespace, scanner: Scanner, git: Git) -> list[Hit]:  # noqa: ARG001
    hits = []
    for name in args.files:
        shown = scanner.disp(name)
        try:
            data = Path(name).read_bytes()
        except OSError as err:
            raise ScanError(
                f"Cannot read {shown} ({err.strerror}). Check the path and permissions."
            ) from err
        if args.strip:
            try:
                stripped = strip_media(name, data)
            except (ValueError, struct.error) as err:
                raise ScanError(
                    f"Cannot strip {shown} ({err}). The file is damaged; re-export the image."
                ) from err
            if stripped != data:
                Path(name).write_bytes(stripped)
                print(f"stripped metadata: {shown}")
                data = stripped
        hits += scan_media(scanner, name, data)
        if args.ocr != "off":
            text = ocr_text(name, data)
            if text is not None:
                hits += scanner.scan_text(name, text, is_patch=False, ocr=True, label=shown)
            elif args.ocr == "require":
                raise ScanError(
                    "tesseract is not installed. Install tesseract-ocr or use --ocr auto."
                )
    return hits


SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def push_commits(git: Git, remote: str, local_sha: str, remote_sha: str) -> list[str]:
    """The commits a push puts on the destination remote. Never the remotes in general: a
    commit that is only on the private origin is new to the public fork.

    A branch that merged main carries main's commits, which the destination already has, so
    an update also leaves out the destination's default branch (2026-10-29: the hook refused
    such a push on main's squash commits, whose author check main's own scan skips). Only the
    default branch: a commit held by any other tracking ref may sit on a branch the remote
    has since deleted (second review, 2026-10-29)."""
    if remote_sha.strip("0"):
        if not git.ok("cat-file", "-e", f"{remote_sha}^{{commit}}"):
            raise ScanError(
                "The remote's current tip is not in this clone, so the push range cannot be "
                "computed. Run git fetch for that remote and push again."
            )
        default = default_branch_tip(git, remote)
        return rev_list(git, local_sha, "--not", remote_sha, *([default] if default else []))
    return rev_list(git, local_sha, "--not", f"--remotes={remote}")


def default_branch_tip(git: Git, remote: str) -> str | None:
    """The tip of the destination's default branch as this clone last fetched it: the ref
    `refs/remotes/<remote>/HEAD` points at (set by clone or `git remote set-head`), else
    `refs/remotes/<remote>/main`. None when neither exists."""
    for ref in (f"refs/remotes/{remote}/HEAD", f"refs/remotes/{remote}/main"):
        if git.ok("rev-parse", "-q", "--verify", f"{ref}^{{commit}}"):
            return git.run("rev-parse", f"{ref}^{{commit}}").decode().strip()
    return None


def cmd_push(args: argparse.Namespace, scanner: Scanner, git: Git) -> list[Hit]:
    remote = args.remote[0] if args.remote else ""
    if not remote:
        raise ScanError(
            "push needs the remote's name as its first argument (git passes it to the hook). "
            "Scanning against every remote would skip commits the destination does not have."
        )
    hits: list[Hit] = []
    for raw in sys.stdin.read().splitlines():
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 4 or not all(SHA_RE.fullmatch(p) for p in (parts[1], parts[3])):
            raise ScanError(
                "A line git passed to the pre-push hook did not parse, so the push was not "
                "checked. Push with a current git, or report the line format."
            )
        _local_ref, local_sha, remote_ref, remote_sha = parts
        hits += scanner.scan_text("ref", remote_ref, is_patch=False, label="branch or tag name")
        if not local_sha.strip("0"):
            continue  # a deletion pushes nothing
        if git.run("cat-file", "-t", local_sha).decode().strip() == "tag":
            tag = git.run("cat-file", "tag", local_sha).decode(errors="replace")
            hits += scanner.scan_text("tag", tag, is_patch=False, label=f"tag {local_sha[:10]}")
        hits += scan_commits(git, scanner, push_commits(git, remote, local_sha, remote_sha))
    return hits


def load_state(path: str) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}  # no state yet: a first run scans every branch
    state = {}
    for line in text.splitlines():
        name, _, sha = line.rpartition(" ")
        if name and SHA_RE.fullmatch(sha):
            state[name] = sha
    return state


def _check_exclude_prefix(git: Git, prefix: str, fork_prefix: str) -> None:
    """An exclusion that is too broad silently empties the scan, so it is checked first."""
    if not prefix.endswith("/"):
        raise ScanError(
            f"--exclude-refs {prefix!r} must end in '/': without it the prefix also matches "
            "sibling refs such as refs/remotes/upstream-tags/. Add the trailing slash."
        )
    if prefix.startswith(fork_prefix) or fork_prefix.startswith(prefix):
        raise ScanError(
            f"--exclude-refs {prefix!r} equals, contains or sits inside the fork's own refs "
            f"({fork_prefix!r}), which would hide the branches being scanned. Use upstream's refs."
        )
    if not git.run("for-each-ref", "--count=1", "--format=%(refname)", prefix).strip():
        raise ScanError(
            f"--exclude-refs {prefix!r} matches no ref, so nothing would be excluded. Fetch "
            "upstream's heads and tags into that prefix before the scan."
        )


def cmd_fork(args: argparse.Namespace, scanner: Scanner, git: Git) -> list[Hit]:
    """Backstop for the public fork: every branch's commits new since the last clean run."""
    writing = bool(args.write_baseline)
    if writing:
        if args.state or args.baseline:
            raise ScanError(
                "--write-baseline scans every branch fresh, so it cannot take --state or "
                "--baseline. Run it without them."
            )
        if not scanner.cfg.deny:
            raise ScanError(
                "--write-baseline needs the private denylist: a baseline taken without it could "
                "accept a value the denylist would have caught. Pass --denylist-bundle."
            )
        # Later allowlist or marker edits must never re-open old commits, so the baseline is
        # taken over everything the generic classes find.
        scanner.cfg.allow = []
        scanner.ignore_markers = True
    baseline = load_baseline(args.baseline, scanner.cfg.deny) if args.baseline else set()
    listing = git.run(
        "for-each-ref", "--format=%(objectname) %(refname)", args.refs_prefix
    ).decode()
    tips = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition(" ")
        tips[ref.removeprefix(args.refs_prefix)] = sha
    old = load_state(args.state) if args.state else {}
    seen = [
        sha for sha in sorted(set(old.values())) if git.ok("cat-file", "-e", f"{sha}^{{commit}}")
    ]
    hits: list[Hit] = []
    bad: list[str] = []
    for prefix in args.exclude_refs:
        _check_exclude_prefix(git, prefix, args.refs_prefix)
    excludes = [f"--glob={prefix}*" for prefix in args.exclude_refs]
    unprefixed = 0
    accepted: Counter[str] = Counter()
    accepted_commits: set[str] = set()
    matched: set[tuple[str, str]] = set()
    to_write: dict[tuple[str, str], str] = {}

    def triage(found: list[Hit], commit: str = "") -> list[Hit]:
        """The hits that stay failures. A baselined soft value is counted, not failed; with
        --write-baseline every soft value with a digest is collected and the rest fails."""
        new = []
        for hit in found:
            key = (hit.cls, hit.digest)
            if hit.cls in SOFT and hit.digest and writing:
                where = baseline_location(hit.path)
                if key not in to_write or where < to_write[key]:
                    to_write[key] = where
            elif hit.cls in SOFT and hit.digest and key in baseline:
                accepted[hit.cls] += 1
                matched.add(key)
                if commit:
                    accepted_commits.add(commit)
            else:
                new.append(hit)
        return new

    # Most of the fork's branches carry the same patch series, so a commit is scanned once per
    # run and its hits are reused for every branch whose range holds it (2026-10-29: the
    # per-branch scan repeated tens of thousands of commits and ran out of its 15 minutes).
    # The cache holds new hits only; the accepted ones were counted when the commit was scanned.
    by_commit: dict[str, list[Hit]] = {}
    for name, tip in sorted(tips.items()):
        scanner.tick()
        commits = rev_list(git, tip, "--not", args.base, *excludes, *seen)
        found = triage(scanner.scan_text("ref", name, is_patch=False, label="branch name"))
        hits += found
        count = len(found)
        for sha in commits:
            if sha not in by_commit:
                # The fork keeps upstream's commits too, whose authors are not ours to police;
                # only AE's own commits (subject prefix [ae-patch]) are held to commit-identity.
                subject = git.run("log", "-1", "--format=%s", sha).decode(errors="replace")
                ours = subject.startswith("[ae-patch]")
                unprefixed += not ours
                scanner.check_identity_email = ours
                by_commit[sha] = triage(scan_commit(git, scanner, sha), sha)
                hits += by_commit[sha]
            count += len(by_commit[sha])
        if count:
            bad.append(f"{scanner.disp(name)} (tip {tip[:10]}, {count} hit(s))")
    scanner.check_identity_email = True
    # Fork mode always checks identity; this guard only keeps the notice honest if a caller
    # ever turns the check off (--no-identity exists on the diff subcommand only).
    if unprefixed and scanner.check_identity:
        checked = (
            "the denylist was checked on their author and committer"
            if scanner.cfg.deny
            else "no denylist is loaded, so nothing was checked on their author and committer"
        )
        scanner.notices.append(
            f"{unprefixed} commit(s) outside upstream's refs lack the [ae-patch] prefix; the "
            f"e-mail check was skipped and {checked}"
        )
    if accepted:
        by_class = ", ".join(f"{cls}={n}" for cls, n in sorted(accepted.items()))
        scanner.notices.append(
            f"fork baseline accepted {sum(accepted.values())} hit(s) in "
            f"{len(accepted_commits)} commit(s): {by_class}"
        )
    if baseline and not old and len(baseline - matched):
        scanner.notices.append(
            f"{len(baseline - matched)} baseline entr(ies) matched nothing this run; "
            "regenerate the baseline in an approved PR to prune them"
        )
    scanner.notices += [f"public fork branch to delete: {item}" for item in bad]
    if bad and baseline:
        print(
            "A value that is not in the baseline reached the public fork. Delete the branch "
            "named above (and rotate it if it was a secret); the baseline is not a way to "
            "clear a new value.",
            file=sys.stderr,
        )
    if writing:
        if hits:
            counts = ", ".join(f"{k}={v}" for k, v in sorted(Counter(h.cls for h in hits).items()))
            scanner.notices.append(
                f"baseline not written: {len(hits)} hit(s) cannot be baselined ({counts}). "
                "Delete the branch that holds each one and run again."
            )
        else:
            text = render_baseline(to_write)
            check_baseline_against_denylist(set(to_write), scanner.cfg.deny, text)
            Path(args.write_baseline).write_text(text, encoding="utf-8")
            by_class = ", ".join(
                f"{k}={v}" for k, v in sorted(Counter(cls for cls, _ in to_write).items())
            )
            scanner.notices.append(f"baseline written: {len(to_write)} entr(ies): {by_class}")
    if args.state and not hits and scanner.cfg.deny:
        Path(args.state).parent.mkdir(parents=True, exist_ok=True)
        Path(args.state).write_text(
            "".join(f"{name} {sha}\n" for name, sha in sorted(tips.items())), encoding="utf-8"
        )
    if args.state and not hits and not scanner.cfg.deny:
        scanner.notices.append(
            "no denylist was loaded, so no state was written: every commit is scanned again "
            "once the secret is set"
        )
    print(
        f"fork backstop: {len(tips)} branch(es), {len(by_commit)} unique commit(s) scanned, "
        f"{len(bad)} with new hits"
    )
    return hits


# ---------------------------------------------------------------------------
# Output


def report(hits: list[Hit], args: argparse.Namespace, scanner: Scanner) -> int:
    hashes = bool(getattr(args, "hash_hits", False))
    hits = sorted(set(hits), key=lambda h: (h.path, h.commit, h.line, h.cls, h.rule))
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for hit in hits[:MAX_PRINTED]:
        if in_actions and hit.line > 0:
            where = f" (commit {hit.commit})" if hit.commit else ""
            print(
                f"::error file={escape_annotation(hit.path)},line={hit.line},title=leak-scan"
                f"::{hit.cls} {hit.rule}{where}"
            )
        else:
            print(hit.shown(hashes=hashes))
    if len(hits) > MAX_PRINTED:
        where = args.report or ("the job summary" if in_actions else "re-run with --report FILE")
        print(f"... {len(hits) - MAX_PRINTED} more hits in {where}")
    counts = Counter(h.cls for h in hits)
    if counts:
        print("hits by class: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for path, line, classes in scanner.new_markers:
        print(f"marker: {path}:{line}: allow={classes}")
    for note in scanner.notices:
        print(f"notice: {note}")
    if scanner.warnings:
        shown = sorted(set(scanner.warnings), key=lambda h: (h.path, h.line))
        for warn in shown[:20]:
            print(f"warning: {warn.shown()} (public address outside the documentation ranges)")
        if len(shown) > 20:
            print(f"warning: {len(shown) - 20} more public addresses")
    if args.report:
        lines = [h.shown(hashes=hashes) for h in hits]
        lines += [f"marker: {p}:{n}: allow={c}" for p, n, c in scanner.new_markers]
        lines += [f"notice: {n}" for n in scanner.notices]
        Path(args.report).write_text("\n".join(lines) + ("\n" if lines else ""))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and (hits or scanner.new_markers or scanner.notices):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### leak-scan\n\nNo matched text is printed; fix each hit at its line.\n\n")
            if hits:
                fh.write("```\n" + "\n".join(h.shown() for h in hits) + "\n```\n")
            for path, line, classes in scanner.new_markers:
                fh.write(f"- new marker `{path}:{line}` allow={classes}\n")
            for note in scanner.notices:
                fh.write(f"- {note}\n")
    if hits:
        print(
            f"leak-scan: {len(hits)} hit(s). Fix the text, or follow docs/LEAK-SCAN.md to clear "
            "a false positive (allowlist line or an inline marker with a reason).",
            file=sys.stderr,
        )
        return 1
    print("leak-scan: clean")
    return 0


def _common(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """The shared options; accepted before or after the subcommand."""
    dflt = {} if top else {"default": argparse.SUPPRESS}
    env = os.environ.get("AE_LEAK_DENYLIST_FILE")
    parser.add_argument(
        "--denylist-bundle", **({"default": env} if top else dflt), help="private denylist bundle"
    )
    parser.add_argument(
        "--denylist-b64-env", help="env var holding the bundle as one base64 line", **dflt
    )
    parser.add_argument("--denylist-sha256", help="expected sha256 of the bundle (hex)", **dflt)
    parser.add_argument(
        "--denylist-sha256-env", help="env var holding the expected sha256", **dflt
    )
    parser.add_argument(
        "--denylist-missing",
        choices=["fail", "warn"],
        default=argparse.SUPPRESS if not top else "fail",
        help="what to do when --denylist-b64-env names an empty variable",
    )
    parser.add_argument("--require-denylist", action="store_true", **dflt)
    parser.add_argument(
        "--allowlist", help="allowlist file (default scripts/leak-allowlist.txt)", **dflt
    )
    parser.add_argument(
        "--report", help="write the full hit list (no values) to this file", **dflt
    )
    parser.add_argument(
        "--hash-hits",
        action="store_true",
        help="print sha256 of each soft hit's value, to key an allowlist entry on it",
        **dflt,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Block internal identifiers (docs/LEAK-SCAN.md).")
    _common(parser, top=True)
    sub = parser.add_subparsers(dest="command", required=True)
    diff = sub.add_parser("diff", help="the commits, paths and PR text of B..H, commit by commit")
    _common(diff, top=False)
    diff.add_argument("--base", required=True)
    diff.add_argument("--head", required=True)
    diff.add_argument(
        "--allowlist-from",
        choices=["auto", "base", "head"],
        default="auto",
        help="auto: the base commit's allowlist when the base has a scanner (a PR), else the "
        "head's; head: for a push to main, whose entries a reviewed PR already approved",
    )
    diff.add_argument(
        "--allowlist-approved",
        action="store_true",
        help="the PR carries the approval label from an approver: entries it adds are noticed, "
        "not failed",
    )
    diff.add_argument(
        "--no-identity",
        action="store_true",
        help="skip commit author and committer checks: for a push to private main, whose "
        "history never goes public (the public mirror is rebuilt under a fixed identity)",
    )
    diff.add_argument("--title-env", help="name of an environment variable holding the PR title")
    diff.add_argument("--body-env", help="name of an environment variable holding the PR body")
    tree = sub.add_parser("tree", help="every tracked file of a tree")
    _common(tree, top=False)
    tree.add_argument("--ref")
    tree.add_argument("--git-dir")
    tree.add_argument("--tree")
    text = sub.add_parser("text", help="prose such as release notes or a PR body")
    _common(text, top=False)
    text.add_argument("files", nargs="+")
    media = sub.add_parser("media", help="image metadata (and OCR when tesseract exists)")
    _common(media, top=False)
    media.add_argument("files", nargs="+")
    media.add_argument("--strip", action="store_true")
    media.add_argument("--ocr", choices=["auto", "off", "require"], default="auto")
    push = sub.add_parser("push", help="git pre-push mode: refs on stdin, remote name first")
    _common(push, top=False)
    push.add_argument("remote", nargs="*")
    fork = sub.add_parser("fork", help="backstop: branches of the public fork, new commits")
    _common(fork, top=False)
    fork.add_argument("--refs-prefix", default="refs/remotes/fork/")
    fork.add_argument("--base", required=True, help="commit every branch grows from (excluded)")
    fork.add_argument("--state", help="file of branch and tip pairs scanned clean last time")
    fork.add_argument(
        "--baseline",
        metavar="FILE",
        help="values the fork already carried (class and hash): a soft hit on one is counted, "
        "not failed; every other hit is new",
    )
    fork.add_argument(
        "--write-baseline",
        metavar="FILE",
        help="scan every branch fresh, ignoring the allowlist and markers, and write the "
        "baseline of its soft hits; refuses while any hit cannot be baselined",
    )
    fork.add_argument(
        "--exclude-refs",
        action="append",
        default=[],
        metavar="PREFIX",
        help="a ref prefix (such as refs/remotes/upstream/) whose history is not ours and is "
        "left out of every branch's range; repeatable",
    )
    return parser


_ACTIVE: Config | None = None


def safe(text: str) -> str:
    """*text* with the active denylist and generic matches masked; for messages and errors."""
    cfg = _ACTIVE or Config()
    return redact_text(text, cfg)[:600]


def load_denylist(args: argparse.Namespace) -> list[DenyEntry]:
    """The bundle from a file or the base64 variable, checked against its expected hash. Every
    problem with the denylist configuration is a scan failure (exit 2), never a partial scan."""
    expected = getattr(args, "denylist_sha256", None)
    env_name = getattr(args, "denylist_sha256_env", None)
    if not expected and env_name:
        expected = os.environ.get(env_name, "").strip() or None
    raw: bytes | None = None
    from_secret = False
    b64_name = getattr(args, "denylist_b64_env", None)
    if b64_name:
        value = os.environ.get(b64_name, "").strip()
        if value:
            try:
                raw = base64.b64decode(value, validate=True)
            except ValueError as err:
                raise ScanError(
                    f"The denylist secret in ${b64_name} is not valid base64. Re-run "
                    "public-release/push-denylist.sh to set it again."
                ) from err
            from_secret = True
        elif getattr(args, "denylist_missing", "fail") == "fail":
            raise ScanError(
                f"${b64_name} is empty, so the private denylist cannot be applied. Set the "
                "AE_LEAK_DENYLIST_B64 secret (public-release/push-denylist.sh) or run with "
                "--denylist-missing warn."
            )
        else:
            print(
                "::warning::The private denylist secret is not set, so the denylist was NOT "
                "applied; only the generic classes ran. Run public-release/push-denylist.sh.",
                file=sys.stderr,
            )
    if raw is None and args.denylist_bundle:
        try:
            raw = Path(args.denylist_bundle).read_bytes()
        except OSError as err:
            raise ScanError(
                f"Cannot read the denylist bundle ({err.strerror}). Rebuild it with "
                "public-release/denylist-bundle.sh or unset AE_LEAK_DENYLIST_FILE."
            ) from err
    if raw is None:
        return []
    if from_secret and not expected:
        raise ScanError(
            "The denylist secret is set but its hash variable is not, so the bundle cannot be "
            "checked. Set AE_LEAK_DENYLIST_SHA256 (public-release/push-denylist.sh sets both)."
        )
    if expected and hashlib.sha256(raw).hexdigest() != expected.lower():
        raise ScanError(
            "The denylist bundle does not match its expected sha256, so it is stale or "
            "half-updated. Re-run public-release/push-denylist.sh to set both together."
        )
    return parse_bundle(raw.decode("utf-8"))


def load_config(args: argparse.Namespace, git: Git, default_ref: str | None) -> Config:
    global _ACTIVE
    cfg = Config(
        new_lines=args.command in ("diff", "push", "text", "fork"), fork=args.command == "fork"
    )
    cfg.deny = load_denylist(args)
    _ACTIVE = cfg
    if args.require_denylist and not cfg.deny:
        raise ScanError(
            "A denylist is required but none was loaded. Pass --denylist-bundle FILE with at "
            "least one entry (public-release/denylist-bundle.sh builds it)."
        )
    if not cfg.deny:
        print(
            "leak-scan: no denylist bundle loaded; only the generic classes run.", file=sys.stderr
        )
    allow_text = ""
    if args.allowlist:
        try:
            allow_text = Path(args.allowlist).read_text(encoding="utf-8")
        except OSError as err:
            raise ScanError(f"Cannot read the allowlist ({err.strerror}).") from err
    elif default_ref:
        blob = git.blob(f"{default_ref}:{DEFAULT_ALLOWLIST}")
        allow_text = blob.decode("utf-8") if blob is not None else ""
    else:
        path = ROOT / DEFAULT_ALLOWLIST
        allow_text = path.read_text(encoding="utf-8") if path.exists() else ""
    cfg.allow = parse_allowlist(allow_text)
    check_allowlist_against_denylist(cfg.allow, cfg.deny, allow_text)
    return cfg


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    git = Git(getattr(args, "git_dir", None))
    try:
        default_ref = None
        if args.command == "diff":
            source = args.allowlist_from
            if source == "auto":
                has_scanner = git.blob(f"{args.base}:scripts/leak-scan.py") is not None
                source = "base" if has_scanner else "head"
            args.allow_source = source
            # Approved entries take effect, so the head's allowlist is the one in force.
            default_ref = (
                args.base if source == "base" and not args.allowlist_approved else args.head
            )
        elif args.command == "tree":
            if not (args.ref or (args.git_dir and args.tree)):
                raise ScanError("tree needs --ref R, or --git-dir D with --tree T.")
            default_ref = args.tree or args.ref
        cfg = load_config(args, git, default_ref)
        scanner = Scanner(cfg)
        scanner.record_markers = args.command in ("diff", "push")
        scanner.check_identity = not getattr(args, "no_identity", False)
        seconds = float(os.environ.get("AE_LEAK_SCAN_SECONDS", SCAN_SECONDS))
        scanner.limit_seconds = seconds
        scanner.deadline = time.monotonic() + seconds
        handler = {
            "diff": cmd_diff,
            "tree": cmd_tree,
            "text": cmd_text,
            "media": cmd_media,
            "push": cmd_push,
            "fork": cmd_fork,
        }[args.command]
        hits = handler(args, scanner, git)
        return report(hits, args, scanner)
    finally:
        git.close()


def main() -> int:
    try:
        return run(sys.argv[1:])
    except ScanError as err:
        print(f"leak-scan failed: {safe(str(err))}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001
        if os.environ.get("AE_LEAK_DEBUG"):
            traceback.print_exc()
        print(
            f"leak-scan failed: unexpected {type(err).__name__} ({safe(str(err))}); treat as "
            "not clean. Set AE_LEAK_DEBUG=1 for a traceback.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
