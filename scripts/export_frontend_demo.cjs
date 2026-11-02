// Refresh fictional fixtures from a local lanis_ui checkout; no JS is needed at runtime.
// Usage: node scripts/export_frontend_demo.cjs /path/to/lanis_ui
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');
const ui = path.resolve(process.argv[2] || '../lanis_ui');
const uiRequire = createRequire(path.join(ui, 'package.json'));
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'lanis-demo-'));
try {
  const entry = path.join(ui, 'src/components/demo/mockApi.ts');
  const bundle = path.join(temporary, 'mock.cjs');
  uiRequire('esbuild').buildSync({ entryPoints: [entry], bundle: true, platform: 'node', format: 'cjs', outfile: bundle });
  const { getMockResponse: mock } = require(bundle);
  const source = fs.readFileSync(entry, 'utf8');
  const responses = {};
  const read = (url, params = {}, method = 'get') => mock(url, method, { params }).data;
  for (const match of source.matchAll(/u === '(\/[^']+)'/g)) {
    const data = read(match[1]);
    if (Object.keys(data).length > 1) responses[match[1]] = data;
  }
  for (const message of responses['/nachrichten/headers'].conversations) {
    const url = '/nachrichten/' + message.Uniquid;
    responses[url] = read(url);
  }
  for (const course of responses['/meinunterricht'].entries) {
    const url = '/meinunterricht/course/' + course.book_id;
    responses[url] = read(url);
  }
  for (let n = 1; n <= 6; n++) {
    const key = 'b' + n + 'e1';
    responses['/meinunterricht/entry?url=' + key] = read('/meinunterricht/entry', { url: key });
  }
  responses['/meinunterricht/entry'] = responses['/meinunterricht/entry?url=b1e1'];
  for (const event of responses['/kalender/events'].events) {
    const url = '/kalender/event/' + event.id;
    responses[url] = read(url);
  }
  for (let n = 0; n <= 4; n++) responses['/dateispeicher?folder_id=' + n] = read('/dateispeicher', { folder_id: n });
  for (const url of ['/dsb/login', '/dsb/plan', '/dsb/plan-urls']) responses[url] = read(url, {}, 'post');
  fs.writeFileSync(path.join(__dirname, '../api/demo/responses.json'), JSON.stringify({
    generated_on: new Date().toISOString().slice(0, 10), responses,
  }, null, 2) + '\n');
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}
