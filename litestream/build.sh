#!/bin/bash

function build_wheel {
  cp $1 litestream/bin/litestream
  python3 -m build . --wheel
  mv dist/litestream-0.0.1a1-py3-none-any.whl $2
}


build_wheel tmp/litestream-darwin-amd64 litestream-0.0.1-py3-none-macosx_10_6_x86_64.whl
build_wheel tmp/litestream-linux-amd64 litestream-0.0.1-py3-none-manylinux1_x86_64.whl
build_wheel tmp/litestream-linux-arm7  litestream-0.0.1-py3-none-linux_armv7l.whl
build_wheel tmp/litestream-linux-arm64 litestream-0.0.1-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl

# TODO litestream-0.0.1-py3-none-macosx_11_0_arm64.whl



