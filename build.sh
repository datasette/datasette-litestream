#!/bin/bash
mkdir -p datasette_litestream/bin

function build_wheel {
  cp $1 src/datasette_litestream/bin/litestream
  python3 -m build . --wheel
  mv dist/datasette_litestream-*-py3-none-any.whl $2
}


build_wheel tmp/litestream-darwin-amd64 datasette_litestream-0.0.1-py3-none-macosx_10_6_x86_64.whl
build_wheel tmp/litestream-linux-amd64 datasette_litestream-0.0.1-py3-none-manylinux1_x86_64.whl
build_wheel tmp/litestream-linux-arm7 datasette_litestream-0.0.1-py3-none-linux_armv7l.whl
build_wheel tmp/litestream-linux-arm64 datasette_litestream-0.0.1-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
# TODO litestream-0.0.1-py3-none-macosx_11_0_arm64.whl

rm src/datasette_litestream/bin/litestream
python3 -m build . --sdist
mv dist/datasette-litestream-0.1.tar.gz datasette-litestream-0.1.tar.gz



