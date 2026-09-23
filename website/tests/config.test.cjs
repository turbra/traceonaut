const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const config = require('../docusaurus.config');
const sidebars = require('../sidebars');
const sourceLinks = require('../remark-source-links.cjs');
const root = path.resolve(__dirname, '../..');
const docs = config.presets[0][1].docs;

test('renders only explicit public documents, directly from source', () => {
  assert.equal(docs.path, '..');
  assert.equal(path.isAbsolute(docs.sidebarPath), false);
  assert.equal(path.isAbsolute(config.presets[0][1].theme.customCss), false);
  assert.equal(docs.include.length, 8);
  assert.equal(new Set(docs.include).size, docs.include.length);
  for (const file of docs.include) {
    assert.match(file, /^(references\/[a-z-]+\.md|website\/docs\/home\.mdx)$/);
    assert(fs.lstatSync(path.join(root, file)).isFile());
  }
  assert.equal(docs.include.filter(file => file.startsWith('references/')).length, 7);
});

test('sidebar document IDs resolve to the same authoritative files', () => {
  const ids = new Set(docs.include.map(file => file.replace(/\.mdx?$/, '')));
  function visit(items) {
    for (const item of items) {
      if (item.type === 'doc') assert(ids.has(item.id), item.id);
      if (item.items) visit(item.items);
    }
  }
  visit(sidebars.docs);
});

test('Pages routes are unique and broken links fail the build', () => {
  assert.equal(config.url, 'https://turbra.github.io');
  assert.equal(config.baseUrl, '/traceonaut/');
  assert.equal(config.trailingSlash, true);
  assert.equal(config.onBrokenLinks, 'throw');
  assert.equal(config.onBrokenAnchors, 'throw');
  assert.equal(config.markdown.hooks.onBrokenMarkdownLinks, 'throw');
  const routes = docs.include.map(file => {
    const source = fs.readFileSync(path.join(root, file), 'utf8');
    assert.match(source, /^---\nslug: \/[^\n]*\n/);
    return source.match(/^slug: (.+)$/m)[1];
  });
  assert.equal(new Set(routes).size, routes.length);
  assert(routes.includes('/'));
  assert(routes.includes('/getting-started'));
});

test('committed banner is the site title and share image', () => {
  assert.deepEqual(config.staticDirectories, ['static', '../assets']);
  assert.equal(config.themeConfig.image, 'traceonaut.png');
  const home = fs.readFileSync(path.join(root, 'website/docs/home.mdx'), 'utf8');
  assert.match(home, /^hide_title: true$/m);
  assert.match(home, /<h1 className="traceonaut-hero">\s*<img src=\{useBaseUrl\('\/traceonaut\.png'\)\} alt="Traceonaut: Explore every run" \/>\s*<\/h1>/);
  assert(fs.lstatSync(path.join(root, 'assets/traceonaut.png')).isFile());
});

test('source links go to GitHub while document links and code remain intact', () => {
  const tree = {children: [
    {type: 'link', url: '../scripts/traceonaut/observability_host.py'},
    {type: 'definition', url: '../schemas/supervisor-project-registration-v1.schema.json'},
    {type: 'link', url: 'deployment.md#1-run-the-collector'},
    {type: 'link', url: 'https://example.org/'},
    {type: 'code', value: '[source](../scripts/example.py)'},
  ]};
  sourceLinks()(tree);
  assert.equal(tree.children[0].url, 'https://github.com/turbra/traceonaut/blob/main/scripts/traceonaut/observability_host.py');
  assert.equal(tree.children[1].url, 'https://github.com/turbra/traceonaut/blob/main/schemas/supervisor-project-registration-v1.schema.json');
  assert.equal(tree.children[2].url, 'deployment.md#1-run-the-collector');
  assert.equal(tree.children[3].url, 'https://example.org/');
  assert.equal(tree.children[4].value, '[source](../scripts/example.py)');
  assert.throws(() => sourceLinks()({type: 'link', url: '../scripts/../../private.key'}), /escapes/);
});

test('source changes trigger Pages and only build output is uploaded', () => {
  const workflow = fs.readFileSync(path.join(root, '.github/workflows/pages.yml'), 'utf8');
  assert.equal((workflow.match(/'references\/\*\*'/g) || []).length, 2);
  assert.equal((workflow.match(/'assets\/traceonaut\.png'/g) || []).length, 2);
  assert.match(workflow, /path: website\/build/);
  assert.match(workflow, /persist-credentials: false/);
  assert.match(workflow, /if: github\.event_name != 'pull_request' && github\.ref == 'refs\/heads\/main'/);
  for (const action of workflow.matchAll(/uses: (\S+)/g)) {
    assert.match(action[1], /^actions\/[a-z-]+@[0-9a-f]{40}$/);
  }
});
