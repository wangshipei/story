#!/bin/bash
# 一键部署 story.shipei.wang：从 md 生成 stories.js，拷到 /var/www/story
set -e
cd "$(dirname "$0")"
node build.js
mkdir -p /var/www/story
cp index.html stories.js /var/www/story/
echo "已部署到 /var/www/story"
