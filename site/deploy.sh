#!/bin/bash
# 一键部署 story.shipei.wang：Mac 经 SSH 发布到 V4；服务器上直接复制。
set -euo pipefail
cd "$(dirname "$0")"
node build.js
if [ "$(uname -s)" = Darwin ]; then
  rsync -az -e 'ssh -o BatchMode=yes -o ConnectTimeout=15' \
    index.html stories.js "${STORY_DEPLOY_HOST:-apps2-server}:/var/www/story/"
  echo "已部署到 V4 /var/www/story"
else
  mkdir -p /var/www/story
  cp index.html stories.js /var/www/story/
  echo "已部署到 /var/www/story"
fi
