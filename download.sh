#!/bin/bash
set -euo pipefail

# Downloads the prebuilt litestream binaries that get bundled into the
# per-platform wheels (see build.sh). Update LITESTREAM_VERSION to bump the
# bundled litestream release.
#
# Note: litestream 0.5.x changed its release asset naming compared to 0.3.x —
# there is no leading "v" in the filename, the architecture names are
# x86_64/arm64/armv7 (not amd64/arm7), and macOS builds ship as .tar.gz rather
# than .zip. The tmp/* output names below are kept stable so build.sh does not
# need to know about that.

LITESTREAM_VERSION="0.5.12"
BASE="https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}"

mkdir -p tmp

# out_name = the wheel-platform name build.sh expects under tmp/
# asset_arch = the architecture slug used in the 0.5.x release asset name
download() {
  local out_name="$1"
  local asset_arch="$2"
  local asset="litestream-${LITESTREAM_VERSION}-${asset_arch}.tar.gz"
  curl -fLO "${BASE}/${asset}"
  tar -xvzf "${asset}" -C tmp litestream
  mv tmp/litestream "tmp/${out_name}"
  rm "${asset}"
}

download litestream-darwin-amd64 darwin-x86_64
download litestream-darwin-arm64 darwin-arm64
download litestream-linux-amd64  linux-x86_64
download litestream-linux-arm64  linux-arm64
download litestream-linux-arm7   linux-armv7
