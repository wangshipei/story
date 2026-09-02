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

// 连作：按阅读顺序连排成小辑，整块插回最早成员原来的位置，其余篇目不动。
// 阅读顺序以本清单为准（与 CLAUDE.md 连作档案一致，两处要同步改）；
// 阅读器据 series 字段生成辑扉、目录分组和篇内「之N」。
const SERIES = [
  { name: '成年人的浪漫', label: '连作 · 四篇', titles: ['成年人的浪漫', '名字', '备注', '怕冷'] },
];
let ordered = stories;
for (const ser of SERIES) {
  const members = ser.titles.map(t => {
    const st = ordered.find(x => x.title === t);
    if (!st) { console.error(`连作《${ser.name}》缺篇：${t}`); process.exit(1); }
    return st;
  });
  members.forEach((st, k) => {
    st.series = ser.name;
    st.seriesLabel = ser.label;
    st.seriesIndex = k + 1;
    st.seriesCount = members.length;
  });
  const at = Math.min(...members.map(st => ordered.indexOf(st)));
  const memberSet = new Set(members);
  const rest = ordered.filter(st => !memberSet.has(st));
  rest.splice(at, 0, ...members);
  ordered = rest;
}

const book = {
  title: '照常',
  front: '天塌下来的那天，红绿灯照常在变。',
  back: '这本书里没有人哭。眼泪在你那儿。',
};

const out = `// 由 site/build.js 自动生成，不要手改；数据来自仓库根目录的 YYYY-MM-DD-标题.md
export const book = ${JSON.stringify(book, null, 2)};

export const stories = ${JSON.stringify(ordered, null, 2)};
`;
fs.writeFileSync(path.join(__dirname, 'stories.js'), out);
console.log(`stories.js 已生成：${stories.length} 篇`);
