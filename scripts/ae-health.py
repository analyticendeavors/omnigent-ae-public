#!/usr/bin/env python3
"""``GET /v1/ae/health`` on a server, with the stored login, and print the answer.

Run as ``just ae-health <server-url>`` (upstream's venv, where the AE package
is installed). The route sits behind the same login as every other ``/v1/ae``
route, so a bare ``curl`` answers 401 in accounts mode; this sends the token
``omnigent login`` stored for the URL (or ``OMNIGENT_TOKEN``), through the
same client the MCP server and the seeder use. Exit 0 on a 200 with ``ok``,
1 otherwise, with the status and body on stderr. Written 2026-09-23 (Phase 6).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from ae_omni_policies.omni import SERVER_URL_ENV, Settings, http_client


async def _check(url: str) -> int:
    settings = Settings.from_env({**os.environ, SERVER_URL_ENV: url})
    async with http_client(settings) as client:
        response = await client.get("/v1/ae/health")
    body = response.text
    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") is True:
            print(json.dumps(data, indent=2, sort_keys=True))
            return 0
    hint = ""
    if response.status_code == 401:
        hint = f" (no valid login for {url}: run `omnigent login {url}` first)"
    print(f"ae-health: {url}/v1/ae/health answered {response.status_code}{hint}", file=sys.stderr)
    print(body[:2000], file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in {"-h", "--help"}:
        print("usage: ae-health.py <server-url>", file=sys.stderr)
        return 2
    return asyncio.run(_check(argv[1].rstrip("/")))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
