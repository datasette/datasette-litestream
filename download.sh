#!/bin/bash
set -euo pipefail

# Downloads the prebuilt litestream binaries that get bundled into the
# per-platform wheels (see build.sh). Update LITESTREAM_VERSION to bump the
# bundled litestream release — that also requires updating the pinned sha256
# digests in sha256_for below (published in the checksums.txt asset of the
# litestream release); any mismatch fails the build, by design, so a
# re-pointed tag or tampered release asset cannot flow into a wheel.
#
# Note: litestream 0.5.x changed its release asset naming compared to 0.3.x —
# there is no leading "v" in the filename, the architecture names are
# x86_64/arm64/armv7 (not amd64/arm7), and macOS builds ship as .tar.gz rather
# than .zip. The tmp/* output names below are kept stable so build.sh does not
# need to know about that.

LITESTREAM_VERSION="0.5.12"
BASE="https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}"

# Pinned sha256 digest for each release asset we consume. The version is part
# of the asset name, so bumping LITESTREAM_VERSION with stale entries here
# fails loudly instead of skipping verification.
sha256_for() {
  case "$1" in
    litestream-0.5.12-darwin-x86_64.tar.gz)
      echo "05185e896250fd7bbba4f80dc2996005ae9cdf581880f2f11f6ce6234721c77c" ;;
    litestream-0.5.12-darwin-arm64.tar.gz)
      echo "0efceca85426ab00276db1dce0a756e5c3c74cce23d2ded66310614eddb0f201" ;;
    litestream-0.5.12-linux-x86_64.tar.gz)
      echo "e10049d206079ef12dc623d859780b2f7a06d32418dc1939004381abbafd01f1" ;;
    litestream-0.5.12-linux-arm64.tar.gz)
      echo "14f496b640767279e7e9eca71218150d9251bf2d488e9fae6f012543f50a20ec" ;;
    litestream-0.5.12-linux-armv7.tar.gz)
      echo "b06013c0e314060a4047221dc176baabd0e01bde9c6ae6ec9b51d2fa4da0819c" ;;
    *)
      echo "download.sh: no pinned sha256 for asset $1" >&2
      return 1 ;;
  esac
}

# "download.sh sha256-for <asset>" prints the pinned digest and exits; used by
# the Justfile litestream-bin recipe and CI so the digests live in one place.
if [ "${1:-}" = "sha256-for" ]; then
  sha256_for "$2"
  exit 0
fi

mkdir -p tmp

# out_name = the wheel-platform name build.sh expects under tmp/
# asset_arch = the architecture slug used in the 0.5.x release asset name
download() {
  local out_name="$1"
  local asset_arch="$2"
  local asset="litestream-${LITESTREAM_VERSION}-${asset_arch}.tar.gz"
  curl -fLO "${BASE}/${asset}"
  echo "$(sha256_for "${asset}")  ${asset}" | shasum -a 256 -c -
  tar -xvzf "${asset}" -C tmp litestream
  mv tmp/litestream "tmp/${out_name}"
  rm "${asset}"
}

download litestream-darwin-amd64 darwin-x86_64
download litestream-darwin-arm64 darwin-arm64
download litestream-linux-amd64  linux-x86_64
download litestream-linux-arm64  linux-arm64
download litestream-linux-arm7   linux-armv7
