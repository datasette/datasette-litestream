#!/bin/bash
set -euo pipefail

curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-darwin-amd64.zip
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-darwin-arm64.zip
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-arm64.tar.gz
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-arm7.tar.gz

unzip -j litestream-v0.3.13-darwin-amd64.zip litestream -d tmp
mv tmp/litestream tmp/litestream-darwin-amd64
unzip -j litestream-v0.3.13-darwin-arm64.zip litestream -d tmp
mv tmp/litestream tmp/litestream-darwin-arm64
tar -xvzf litestream-v0.3.13-linux-amd64.tar.gz -C tmp litestream
mv tmp/litestream tmp/litestream-linux-amd64
tar -xvzf litestream-v0.3.13-linux-arm64.tar.gz -C tmp litestream
mv tmp/litestream tmp/litestream-linux-arm64
tar -xvzf litestream-v0.3.13-linux-arm7.tar.gz -C tmp litestream
mv tmp/litestream tmp/litestream-linux-arm7


rm litestream-v0.3.13-darwin-amd64.zip
rm litestream-v0.3.13-darwin-arm64.zip
rm litestream-v0.3.13-linux-amd64.tar.gz
rm litestream-v0.3.13-linux-arm64.tar.gz
rm litestream-v0.3.13-linux-arm7.tar.gz
