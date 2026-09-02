#!/usr/bin/env node
// 扫描仓库根目录的 YYYY-MM-DD-标题.md，按文件名（日期升序）生成 site/stories.js。
// 标题取首行 `# 标题`，日期取文件名前 10 位，其余为正文。
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const files = fs.readdirSync(root)
  .filter(f => /^\d{4}-\d{2}-\d{2}-.+\.md$/.test(f))
  .sort();

const stories = files.map(f => {
  const raw = fs.readFileSync(path.join(root, f), 'utf8').trim();
  let title = f.slice(11).replace(/\.md$/, '');
  let text = raw;
  const m = raw.match(/^#\s*(.+?)\s*\n+([\s\S]*)$/);
  if (m) { title = m[1]; text = m[2].trim(); }
  return { title, date: f.slice(0, 10), text };
});

const book = {
  title: '照常',
  front: '天塌下来的那天，红绿灯照常在变。',
  back: '这本书里没有人哭。眼泪在你那儿。',
};

const out = `// 由 site/build.js 自动生成，不要手改；数据来自仓库根目录的 YYYY-MM-DD-标题.md
export const book = ${JSON.stringify(book, null, 2)};

export const stories = ${JSON.stringify(stories, null, 2)};
`;
fs.writeFileSync(path.join(__dirname, 'stories.js'), out);
console.log(`stories.js 已生成：${stories.length} 篇`);
