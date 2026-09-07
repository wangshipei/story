#!/usr/bin/env node
// 在独立无头浏览器中操作项目原生分享按钮，保存其生成的PNG，不另写排版逻辑。
import { chromium } from 'playwright-core';
import { createServer } from 'node:http';
import { readFile, mkdir, rename, access } from 'node:fs/promises';
import { resolve, dirname, basename, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
let outputDir;
const numbers = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--output-dir' && args[i + 1]) outputDir = resolve(args[++i]);
  else if (args[i] === '--story' && /^\d{1,3}$/.test(args[i + 1] || '')) numbers.push(Number(args[++i]));
  else throw new Error('用法：node cron/render_share.mjs --output-dir DIR --story 57 --story 58');
}
if (!outputDir || !numbers.length || numbers.some(n => n < 1) || new Set(numbers).size !== numbers.length) {
  throw new Error('请指定输出目录和不重复的小说正典序号');
}

const chrome = process.env.STORY_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
await access(chrome);
const files = new Map([
  ['/', { body: await readFile(join(root, 'site/index.html')), type: 'text/html; charset=utf-8' }],
  ['/stories.js', { body: await readFile(join(root, 'site/stories.js')), type: 'text/javascript; charset=utf-8' }],
]);
const server = createServer((req, res) => {
  const file = files.get(new URL(req.url, 'http://127.0.0.1').pathname);
  if (!file) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': file.type, 'Cache-Control': 'no-store' });
  res.end(file.body);
});
await new Promise((ok, fail) => {
  server.once('error', fail);
  server.listen(0, '127.0.0.1', ok);
});
let browser;
try {
  browser = await chromium.launch({ executablePath: chrome, headless: true });
  const context = await browser.newContext({ colorScheme: 'light', viewport: { width: 820, height: 1080 }, acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  await page.addInitScript(() => {
    localStorage.setItem('zhaochang-reader', JSON.stringify({ i: 0, size: 1, dark: false, surl: false }));
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`, { waitUntil: 'domcontentloaded' });
  const stories = await page.evaluate(async () => (await import('/stories.js')).stories.map(s => ({ num: s.num, title: s.title })));
  await mkdir(outputDir, { recursive: true, mode: 0o700 });
  const images = [];
  for (const number of numbers) {
    const story = stories.find(s => s.num === number);
    if (!story) throw new Error(`构建数据中没有第${number}篇；先运行node site/build.js`);
    await page.locator('#tocBtn').click();
    const row = page.locator('#tocList button.row').filter({ has: page.getByText(story.title, { exact: true }) });
    await row.click();
    await page.locator('#shareBtn').click();
    await page.locator('#shareOv').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.getElementById('shareImg').naturalWidth === 1200);
    const downloadReady = page.waitForEvent('download');
    await page.locator('#shareSaveBtn').click();
    const download = await downloadReady;
    const name = download.suggestedFilename();
    if (basename(name) !== name || name !== `照常-${String(number).padStart(3, '0')}-${story.title}.png`) {
      throw new Error('分享图片文件名与目标篇目不一致');
    }
    const target = join(outputDir, name);
    await download.saveAs(target + '.tmp');
    const data = await readFile(target + '.tmp');
    if (!data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) || data.readUInt32BE(16) !== 1200) {
      throw new Error('分享功能没有生成预期的1200px PNG');
    }
    await rename(target + '.tmp', target);
    images.push(target);
    await page.locator('#shareCloseBtn').click();
  }
  process.stdout.write(JSON.stringify({ images }) + '\n');
} finally {
  await browser?.close();
  await new Promise(ok => server.close(ok));
}
