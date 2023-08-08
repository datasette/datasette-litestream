#!/bin/bash

curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.9/litestream-v0.3.9-darwin-amd64.zip
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.9/litestream-v0.3.9-linux-amd64-static.tar.gz
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.9/litestream-v0.3.9-linux-arm64-static.tar.gz
curl -LO https://github.com/benbjohnson/litestream/releases/download/v0.3.9/litestream-v0.3.9-linux-arm7-static.tar.gz

unzip -j litestream-v0.3.9-darwin-amd64.zip litestream -d tmp
mv tmp/litestream tmp/litestream-darwin-amd64
tar -xvzf litestream-v0.3.9-linux-amd64-static.tar.gz -C tmp litestream
mv tmp/litestream tmp/litestream-linux-amd64
tar -xvzf litestream-v0.3.9-linux-arm64-static.tar.gz -C tmp litestream
mv tmp/litestream tmp/litestream-linux-arm64
tar -xvzf litestream-v0.3.9-linux-arm7-static.tar.gz -C tmp litestream
mv tmp/litestream tmp/litestream-linux-arm7


rm litestream-v0.3.9-darwin-amd64.zip
rm litestream-v0.3.9-linux-amd64-static.tar.gz
rm litestream-v0.3.9-linux-arm64-static.tar.gz
rm litestream-v0.3.9-linux-arm7-static.tar.gz
