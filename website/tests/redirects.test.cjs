const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const config = require('../docusaurus.config');
const rules = require('../redirects.json');
const sidebars = require('../sidebars');
const root = path.resolve(__dirname, '../..');

test('dashboard aliases use the matching Docusaurus redirect plugin', () => {
  const plugin = config.plugins.find(([name]) => name === '@docusaurus/plugin-client-redirects');
  assert.deepEqual(plugin[1].redirects, rules.filter(rule => !rule.fragments));
  const deps = require('../package.json').dependencies;
  assert.equal(deps['@docusaurus/plugin-client-redirects'], deps['@docusaurus/core']);
  assert.deepEqual(plugin[1].redirects, [
    {from: '/dashboards/beta/', to: '/dashboards/work-overview/'},
    {from: '/dashboards/stable/', to: '/dashboards/all-sessions/'},
  ]);
});

test('legacy data links preserve their dashboard destination and query string', () => {
  const rule = rules.find(rule => rule.from === '/data-and-limits/');
  const html = fs.readFileSync(path.join(root, 'website/static/data-and-limits/index.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  for (const [hash, destination] of [['', rule.to], ...Object.entries(rule.fragments)]) {
    const url = new URL(`https://turbra.github.io/traceonaut${rule.from}?example=1${hash}`);
    let actual;
    vm.runInNewContext(script, {window: {location: {
      hash: url.hash, search: url.search, replace: target => {actual = new URL(target, url);},
    }}});
    assert.equal(actual.href, `https://turbra.github.io/traceonaut${destination}?example=1`);
  }
  assert.match(html, /<a href="\.\.\/reference\/data-sources-and-privacy\/">/);
  assert.match(html, /<a href="\.\.\/dashboards\/all-sessions\/">/);
});

test('Quick Start step headings match across all three documents', () => {
  const guide = fs.readFileSync(path.join(root, 'references/getting-started.mdx'), 'utf8');
  const headings = (text, level) => [...text.matchAll(new RegExp(`^#{${level}} (\\d+\\. .+)$`, 'gm'))]
    .map(match => match[1]);
  const expected = headings(guide, 2);
  assert.deepEqual(expected.map(heading => parseInt(heading, 10)), [1, 2, 3]);
  for (const source of ['README.md', 'website/docs/home.mdx']) {
    assert.deepEqual(headings(fs.readFileSync(path.join(root, source), 'utf8'), 3), expected, source);
  }
});

test('sequential home setup stays visible and networking choices synchronize', () => {
  const home = fs.readFileSync(path.join(root, 'website/docs/home.mdx'), 'utf8');
  assert(!home.includes('<Tabs'));
  const guide = fs.readFileSync(path.join(root, 'references/getting-started.mdx'), 'utf8');
  const tabs = [...guide.matchAll(/<Tabs groupId="network">([\s\S]*?)<\/Tabs>/g)];
  assert.equal(tabs.length, 2);
  for (const group of tabs) {
    assert.deepEqual([...group[1].matchAll(/<TabItem value="([^"]+)"/g)].map(m => m[1]), ['local', 'remote', 'container']);
  }
  const operations = sidebars.docs.find(item => item.label === 'Operations');
  assert.deepEqual(operations.link, {type: 'doc', id: 'references/operations'});
  assert(!operations.items.some(item => item.id === 'references/operations'));
  const readme = fs.readFileSync(path.join(root, 'README.md'), 'utf8');
  assert.match(readme, /<a href="https:\/\/turbra.github.io\/traceonaut\/">\s*<img src="assets\/traceonaut.png"/);
});
