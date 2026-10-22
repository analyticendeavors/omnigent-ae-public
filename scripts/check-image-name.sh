#!/usr/bin/env bash
# Check the AE_IMAGE repository variable before a workflow builds, tags or
# pushes with it. Added 2026-10-21 for
# https://github.com/analyticendeavors/omnigent-ae-public/issues/13, shared by
# .github/workflows/image.yml (before the build) and release.yml (before the
# tag), so a bad value fails both before anything is pushed.
#
# Usage: scripts/check-image-name.sh IMAGE OWNER
#
# IMAGE must be ghcr.io/<OWNER lower-cased>/<path>, the only registry and
# namespace the workflow's GITHUB_TOKEN can push to. Each path component
# follows the OCI distribution rule: lower-case letters and digits, joined by
# one '.', one or two '_', or any run of '-'. No tag or digest suffix.
# Exit 0 when it passes; otherwise print one ::error:: line and exit 1.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 IMAGE OWNER" >&2; exit 2; }
image="$1"
owner="${2,,}"
component='[a-z0-9]+(([.]|_{1,2}|-+)[a-z0-9]+)*'
pattern="^ghcr[.]io/${owner}/${component}(/${component})*$"

if ! [[ "$image" =~ $pattern ]]; then
  echo "::error::repository variable AE_IMAGE is '$image'; it must be ghcr.io/$owner/<name> in lower case, with no tag, the only package this workflow's token can push to. Fix or delete the variable."
  exit 1
fi
echo "AE_IMAGE $image is under ghcr.io/$owner"
