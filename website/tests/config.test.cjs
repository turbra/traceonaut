const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const config = require('../docusaurus.config');
const sidebars = require('../sidebars');
const sourceLinks = require('../remark-source-links.cjs');
const root = path.resolve(__dirname, '../..');
const docs = config.presets[0][1].docs;

test('edit links point to each repository source on main', () => {
  assert.equal(typeof docs.editUrl, 'function');
  for (const docPath of docs.include) {
    const url = docs.editUrl({docPath, versionDocsDirPath: '..', version: 'current', locale: 'en'});
    assert.equal(url, `https://github.com/turbra/traceonaut/edit/main/${docPath}`);
    assert.equal(new URL(url).pathname, `/turbra/traceonaut/edit/main/${docPath}`);
  }
});

test('renders only explicit public documents, directly from source', () => {
  assert.equal(docs.path, '..');
  assert.equal(path.isAbsolute(docs.sidebarPath), false);
  assert.equal(path.isAbsolute(config.presets[0][1].theme.customCss), false);
  assert.deepEqual(docs.include, require('../docs-manifest.json'));
  assert.equal(docs.include.length, 27);
  assert.equal(new Set(docs.include).size, docs.include.length);
  for (const file of docs.include) {
    assert.match(file, /^(references\/(?:[a-z-]+\/)*[a-z-]+\.mdx?|website\/docs\/home\.mdx)$/);
    assert(fs.lstatSync(path.join(root, file)).isFile());
  }
  assert.equal(docs.include.filter(file => file.startsWith('references/')).length, 26);
});

test('sidebar document IDs resolve to the same authoritative files', () => {
  const ids = new Set(docs.include.map(file => file.replace(/\.mdx?$/, '')));
  function visit(items) {
    for (const item of items) {
      if (item.type === 'doc') {
        assert(ids.has(item.id), item.id);
        const file = docs.include.find(file => file.replace(/\.mdx?$/, '') === item.id);
        const source = fs.readFileSync(path.join(root, file), 'utf8');
        assert.equal(source.match(/^title: (.+)$/m)[1], item.label);
      }
      if (item.link?.type === 'doc') assert(ids.has(item.link.id), item.link.id);
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
    assert.match(source, /^title: .+$/m);
    assert.match(source, /^description: .+$/m);
    const slug = source.match(/^slug: (.+)$/m)[1];
    if (file !== 'website/docs/home.mdx') {
      const expected = '/' + file.replace(/^references\//, '').replace(/\.mdx?$/, '').replace(/\/index$/, '');
      assert.equal(slug, expected, file);
      assert.equal(source.match(/^# (.+)$/m)[1], source.match(/^title: (.+)$/m)[1]);
    }
    return slug;
  });
  assert.equal(new Set(routes).size, routes.length);
  assert(routes.includes('/'));
  assert(routes.includes('/install'));
  assert(routes.includes('/getting-started'));
  assert(routes.includes('/dashboards/cwo'));
});

test('README and site navigation use matching guide destinations', () => {
  const readme = fs.readFileSync(path.join(root, 'README.md'), 'utf8');
  const header = readme.split('\n---\n')[0];
  const links = [...header.matchAll(/<a href="([^"]+)">([^<]+)<\/a>/g)]
    .map(match => ({label: match[2], href: match[1]}));
  const expected = [
    {label: 'Install', href: 'https://turbra.github.io/traceonaut/install/'},
    {label: 'Quick Start', href: 'https://turbra.github.io/traceonaut/getting-started/'},
    {label: 'Documentation', href: 'https://turbra.github.io/traceonaut/'},
  ];
  assert.deepEqual(links, expected);
  assert.deepEqual(config.themeConfig.navbar.items.slice(0, 3).map(item => ({
    label: item.label, href: config.url + config.baseUrl.slice(0, -1) + item.to,
  })), expected);
  const start = sidebars.docs.find(item => item.label === 'Getting Started');
  assert.deepEqual(start.items.map(item => [item.label, item.id]), [
    ['Install', 'references/install'], ['Quick Start', 'references/getting-started'],
  ]);
  assert.match(fs.readFileSync(path.join(root, 'references/install.md'), 'utf8'), /^# Install$/m);
  assert.match(fs.readFileSync(path.join(root, 'references/getting-started.mdx'), 'utf8'), /^# Quick Start$/m);
  assert.match(fs.readFileSync(path.join(root, 'website/docs/home.mdx'), 'utf8'), /^## Quick Start$/m);
});

test('all dashboards are discoverable and CWO setup stays optional', () => {
  const dashboards = sidebars.docs.find(item => item.type === 'category' && item.label === 'Dashboards');
  assert(dashboards.items.some(item => item.id === 'references/dashboards/all-sessions'));
  const optional = sidebars.docs.find(item => item.label === 'Optional');
  assert(optional.items.some(item => item.id === 'references/dashboards/cwo'));
  const home = fs.readFileSync(path.join(root, 'website/docs/home.mdx'), 'utf8');
  const section = home.split('\n## Dashboards\n')[1].split('\n## ')[0];
  const routes = [
    '/dashboards/work-overview/', '/dashboards/unified/',
    '/dashboards/all-sessions/', '/dashboards/cwo/',
  ];
  const menu = config.themeConfig.navbar.items.find(item => item.label === 'Dashboards');
  assert.deepEqual(menu.items.map(item => item.to), routes);
  for (const route of routes) assert(section.includes(`to="${route}"`));
  for (const name of ['codex-work-overview-beta', 'codex-unified-overview', 'codex-all-sessions', 'cwo-observed-dispatches']) {
    const {title} = JSON.parse(fs.readFileSync(path.join(root, `examples/observability/${name}.json`), 'utf8'));
    assert(section.includes(`<strong>${title}</strong>`), title);
  }
  assert(!config.themeConfig.navbar.items.some(item => item.to === '/dashboards/cwo/'));
  assert(config.themeConfig.footer.links[0].items.some(item => item.to === '/dashboards/'));
});

test('committed banner is the site title and share image', () => {
  assert.deepEqual(config.staticDirectories, ['static', '../assets']);
  assert.equal(config.themeConfig.image, 'traceonaut.png');
  const home = fs.readFileSync(path.join(root, 'website/docs/home.mdx'), 'utf8');
  assert.match(home, /^hide_title: true$/m);
  assert.match(home, /<h1 className="traceonaut-hero">\s*<img src=\{useBaseUrl\('\/traceonaut\.png'\)\} alt="Traceonaut: Explore every run" \/>\s*<\/h1>/);
  assert(fs.lstatSync(path.join(root, 'assets/traceonaut.png')).isFile());
});

test('uses the supplied favicon and external Apache license badge', () => {
  assert.equal(config.favicon, 'traceonaut-favicon.png');
  assert(fs.lstatSync(path.join(root, 'assets', config.favicon)).isFile());
  assert(!fs.existsSync(path.join(root, 'assets/license-apache-2.0.svg')));
  for (const file of ['README.md', 'website/docs/home.mdx']) {
    const source = fs.readFileSync(path.join(root, file), 'utf8');
    assert(source.includes('href="https://www.apache.org/licenses/LICENSE-2.0"'));
    assert(source.includes('src="https://img.shields.io/badge/License-Apache--2.0-2C7A7B?style=flat-square"'));
    assert(source.includes('alt="License: Apache-2.0"'));
    assert(!source.includes('license-apache-2.0.svg'));
  }
});

test('source links go to GitHub while document links and code remain intact', () => {
  const tree = {children: [
    {type: 'link', url: '../../scripts/traceonaut/observability_host.py'},
    {type: 'definition', url: '../schemas/supervisor-project-registration-v1.schema.json'},
    {type: 'link', url: 'getting-started.mdx#1-run-the-collector'},
    {type: 'link', url: 'https://example.org/'},
    {type: 'code', value: '[source](../scripts/example.py)'},
  ]};
  sourceLinks()(tree);
  assert.equal(tree.children[0].url, 'https://github.com/turbra/traceonaut/blob/main/scripts/traceonaut/observability_host.py');
  assert.equal(tree.children[1].url, 'https://github.com/turbra/traceonaut/blob/main/schemas/supervisor-project-registration-v1.schema.json');
  assert.equal(tree.children[2].url, 'getting-started.mdx#1-run-the-collector');
  assert.equal(tree.children[3].url, 'https://example.org/');
  assert.equal(tree.children[4].value, '[source](../scripts/example.py)');
  assert.throws(() => sourceLinks()({type: 'link', url: '../../scripts/../../private.key'}), /escapes/);
});

test('source changes trigger Pages and only build output is uploaded', () => {
  const workflow = fs.readFileSync(path.join(root, '.github/workflows/pages.yml'), 'utf8');
  assert.equal((workflow.match(/'README\.md'/g) || []).length, 2);
  assert.equal((workflow.match(/'references\/\*\*'/g) || []).length, 2);
  assert.equal((workflow.match(/'examples\/observability\/\*\.json'/g) || []).length, 2);
  assert.equal((workflow.match(/'assets\/traceonaut\.png'/g) || []).length, 2);
  assert.equal((workflow.match(/'assets\/traceonaut-favicon\.png'/g) || []).length, 2);
  assert.equal((workflow.match(/'assets\/screenshots\/\*\.png'/g) || []).length, 2);
  assert.equal((workflow.match(/'examples\/observability\/prometheus-scrape\.yaml'/g) || []).length, 2);
  assert.match(workflow, /path: website\/build/);
  assert.match(workflow, /persist-credentials: false/);
  assert.match(workflow, /if: github\.event_name != 'pull_request' && github\.ref == 'refs\/heads\/main'/);
  for (const action of workflow.matchAll(/uses: (\S+)/g)) {
    assert.match(action[1], /^actions\/[a-z-]+@[0-9a-f]{40}$/);
  }
});
