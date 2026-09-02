#!/usr/bin/env node
// 扫描仓库根目录的 NNN-标题.md（三位序号=写作顺序），按序号降序生成 site/stories.js——新篇在最前。
// 标题取首行 `# 标题`，其余为正文。
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const files = fs.readdirSync(root)
  .filter(f => /^\d{3}-.+\.md$/.test(f))
  .sort((a, b) => parseInt(b, 10) - parseInt(a, 10));

const stories = files.map(f => {
  const raw = fs.readFileSync(path.join(root, f), 'utf8').trim();
  let title = f.slice(4).replace(/\.md$/, '');
  let text = raw;
  const m = raw.match(/^#\s*(.+?)\s*\n+([\s\S]*)$/);
  if (m) { title = m[1]; text = m[2].trim(); }
  return { num: parseInt(f, 10), title, text };
});

// 连作：按阅读顺序连排成小辑，整块插回（降序列表里）最靠前成员原来的位置，其余篇目不动。
// 阅读顺序以本清单为准（与 CLAUDE.md 连作档案一致，两处要同步改）；
// 阅读器据 series 字段生成辑扉、目录分组和篇内「之N」。
const SERIES = [
  { name: '成年人的浪漫', label: '连作 · 四篇', titles: ['成年人的浪漫', '名字', '备注', '怕冷'] },
  { name: '彩礼', label: '连作 · 四篇', titles: ['彩礼', '嫁妆', '零头', '礼簿'] },
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

const out = `// 由 site/build.js 自动生成，不要手改；数据来自仓库根目录的 NNN-标题.md
export const book = ${JSON.stringify(book, null, 2)};

export const stories = ${JSON.stringify(ordered, null, 2)};
`;
fs.writeFileSync(path.join(__dirname, 'stories.js'), out);
console.log(`stories.js 已生成：${stories.length} 篇`);
