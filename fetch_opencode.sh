#!/usr/bin/env bash
# Fetch a PINNED opencode linux-x64 binary for the Arm-A/B harness.
#
# Both arms run opencode INSIDE each SWE-bench container (so the agent has the
# repo's deps and can run tests). opencode is distributed as pinned npm platform
# packages; we fetch the linux-x64 build once to .localdata/ and bind-mount it
# read-only into every container (-v <bin>:/opt/oc/opencode:ro). Pinning the
# version keeps the agent harness identical across all instances and both arms
# (reproducibility) and avoids per-container network installs.
#
# The SWE-bench eval images are glibc (Ubuntu 22.04 / conda); the standard
# (non-musl) linux-x64 build is correct. The binary is amd64 and runs under
# emulation on Apple Silicon (validated: `opencode --version` -> 1.17.10 in the
# psf__requests-1142 image).
#
# Usage:  ./fetch_opencode.sh            # pins the default version below
#         OPENCODE_VERSION=1.18.0 ./fetch_opencode.sh
# Prints the absolute path to the binary on stdout (last line).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="${OPENCODE_VERSION:-1.17.10}"
DEST="$HERE/.localdata/opencode-$VERSION"
BIN="$DEST/package/bin/opencode"

if [ -x "$BIN" ]; then
  echo "[opencode] already present: $BIN" >&2
  echo "$BIN"; exit 0
fi

mkdir -p "$DEST"
TGZ="$DEST/opencode-linux-x64.tgz"
URL="https://registry.npmjs.org/opencode-linux-x64/-/opencode-linux-x64-$VERSION.tgz"
echo "[opencode] fetching pinned $VERSION from npm registry ..." >&2
curl -fsSL -o "$TGZ" "$URL"
tar xzf "$TGZ" -C "$DEST"

# Verify it's the expected ELF x86-64 binary before anyone mounts/runs it.
if ! file "$BIN" | grep -q 'ELF 64-bit.*x86-64'; then
  echo "[opencode] ERROR: $BIN is not an ELF x86-64 binary" >&2
  file "$BIN" >&2; exit 1
fi
chmod +x "$BIN"
echo "[opencode] ready: $BIN ($(file -b "$BIN" | cut -d, -f1-2))" >&2
echo "$BIN"
