#!/usr/bin/env bash
# Fetch a PINNED node linux-x64 runtime for the Arm-B opencode plugins.
#
# opencode's plugin loader needs a node runtime present; the SWE-bench eval images
# (conda + Python) ship none. We fetch node once to .localdata/ and bind-mount it
# read-only into each Arm-B container (the same pattern as fetch_opencode.sh). The
# verified-loadable smart-prompts plugin has NO npm deps, so node alone is enough —
# no node_modules required.
#
# Usage: ./fetch_node.sh   (prints the absolute path to the node bin dir on stdout)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="${NODE_VERSION:-20.18.0}"
DEST="$HERE/.localdata/node-v${VERSION}-linux-x64"

if [ ! -x "$DEST/bin/node" ]; then
  echo "[node] fetching pinned node v${VERSION} linux-x64 ..." >&2
  TGZ="$HERE/.localdata/node-v${VERSION}-linux-x64.tar.xz"
  curl -fsSL -o "$TGZ" "https://nodejs.org/dist/v${VERSION}/node-v${VERSION}-linux-x64.tar.xz"
  tar -xJf "$TGZ" -C "$HERE/.localdata/"
fi
echo "[node] ready: $DEST/bin/node" >&2
echo "$DEST"
