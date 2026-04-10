#!/usr/bin/env bash
# Bundle the gateway_interceptor Lambda + its Python deps into a deployment zip.
#
# CDK references the resulting zip via Code.fromAsset() in
# infra/data/lib/gateway-stack.ts. Run this BEFORE `npm run deploy` (or
# `npm run synth`) so the zip is fresh.
#
# Why a separate build script and not aws-lambda-python-alpha:
#   PythonFunction wants Docker to bundle in a Linux env. We don't want to
#   require Docker on the dev box. cryptography ships manylinux wheels via
#   pip on macOS that include the Linux .so files we need; uv does the
#   right thing here.
#
# Output: infra/data/build/gateway_interceptor.zip
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/../../.." && pwd )"
WORKER_DIR="$REPO_ROOT/workers/gateway_interceptor"
BUILD_DIR="$REPO_ROOT/infra/data/build/gateway_interceptor"
ZIP_PATH="$REPO_ROOT/infra/data/build/gateway_interceptor.zip"

# Wipe and rebuild — pip leftover state has bitten us before.
rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

echo "==> installing deps into $BUILD_DIR"
# --platform manylinux2014_x86_64 + --python-version 3.13 forces uv to
# pick the Linux wheels for cryptography (cffi, etc.) instead of the macOS
# binaries on a dev laptop. Lambda runs amd64 by default; if we ever
# switch to arm64 Lambdas this needs --platform manylinux2014_aarch64.
uv pip install \
  --target "$BUILD_DIR" \
  --python-version 3.13 \
  --python-platform x86_64-manylinux2014 \
  --no-installer-metadata \
  "pyjwt[crypto] >= 2.8" \
  >/dev/null

echo "==> copying handler source"
# Copy the package source LAST so it overlays cleanly on top of deps.
cp -R "$WORKER_DIR/gateway_interceptor" "$BUILD_DIR/"

# Sanity check: required files exist. We CANNOT do an `import` check here
# because we deliberately installed Linux wheels (cryptography ships
# platform-specific .so files), and those won't load on macOS — the
# import error would be a false negative. Lambda will load them fine.
for required in \
  "$BUILD_DIR/gateway_interceptor/handler.py" \
  "$BUILD_DIR/jwt/__init__.py" \
  "$BUILD_DIR/cryptography/__init__.py" \
  "$BUILD_DIR/cryptography/hazmat/bindings/_rust.abi3.so" ; do
  if [[ ! -e "$required" ]]; then
    echo "  MISSING: $required" >&2
    exit 1
  fi
done
echo "  OK: handler + pyjwt + cryptography (Linux wheels) all present"

echo "==> zipping to $ZIP_PATH"
( cd "$BUILD_DIR" && zip -qr "$ZIP_PATH" . )

bytes=$(stat -f%z "$ZIP_PATH" 2>/dev/null || stat -c%s "$ZIP_PATH")
mb=$(awk "BEGIN {printf \"%.1f\", $bytes / 1024 / 1024}")
echo "==> done: ${mb} MB"
