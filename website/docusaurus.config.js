// @ts-check
const {themes} = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Traceonaut',
  favicon: 'traceonaut-favicon.png',
  tagline: 'Codex and IBM Bob session metrics for your existing Prometheus and Grafana.',
  url: 'https://turbra.github.io',
  baseUrl: '/traceonaut/',
  trailingSlash: true,
  organizationName: 'turbra',
  projectName: 'traceonaut',
  staticDirectories: ['static', '../assets'],
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  markdown: {
    format: 'detect',
    hooks: {onBrokenMarkdownLinks: 'throw'},
  },
  plugins: [
    ['@docusaurus/plugin-client-redirects', {
      redirects: require('./redirects.json').filter(rule => !rule.fragments),
    }],
  ],
  presets: [
    ['classic', {
      docs: {
        // Render the public guides in place. Never crawl private repo state.
        path: '..',
        include: require('./docs-manifest.json'),
        routeBasePath: '/',
        sidebarPath: './sidebars.js',
        editUrl: ({docPath}) => `https://github.com/turbra/traceonaut/edit/main/${docPath}`,
        beforeDefaultRemarkPlugins: [require('./remark-source-links.cjs')],
      },
      blog: false,
      theme: {customCss: './src/css/custom.css'},
    }],
  ],
  themeConfig: {
    image: 'traceonaut.png',
    colorMode: {defaultMode: 'light', respectPrefersColorScheme: true},
    navbar: {
      title: 'Traceonaut',
      items: [
        {to: '/install/', label: 'Install', position: 'left'},
        {to: '/getting-started/', label: 'Quick Start', position: 'left'},
        {to: '/', label: 'Documentation', position: 'left', activeBaseRegex: '^/traceonaut/$'},
        {label: 'Sources', position: 'left', items: [
          {to: '/sources/codex/', label: 'Codex'},
          {to: '/sources/ibm-bob/', label: 'IBM Bob'},
        ]},
        {label: 'Dashboards', position: 'left', items: [
          {to: '/dashboards/', label: 'Choosing a Dashboard'},
          {to: '/dashboards/work-overview/', label: 'Codex: Work Overview'},
          {to: '/dashboards/all-sessions/', label: 'Codex: All Sessions'},
          {to: '/dashboards/tui-beta/', label: 'Codex: Codex TUI · Beta'},
          {to: '/dashboards/cwo/', label: 'Codex: CWO Overview'},
          {to: '/dashboards/ibm-bob-beta/', label: 'IBM Bob: IBM Bob · Beta'},
        ]},
        {href: 'https://github.com/turbra/traceonaut', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'light',
      links: [
        {title: 'Documentation', items: [
          {label: 'Install', to: '/install/'},
          {label: 'Quick Start', to: '/getting-started/'},
          {label: 'Choosing a Dashboard', to: '/dashboards/'},
          {label: 'Scripts', to: '/reference/scripts/'},
          {label: 'Metrics', to: '/reference/metrics/'},
          {label: 'Operations', to: '/operations/'},
        ]},
        {title: 'Project', items: [
          {label: 'GitHub', href: 'https://github.com/turbra/traceonaut'},
          {label: 'Issues', href: 'https://github.com/turbra/traceonaut/issues'},
          {label: 'Apache-2.0 License', href: 'https://github.com/turbra/traceonaut/blob/main/LICENSE'},
        ]},
        {title: 'Related', items: [
          {label: 'Complex Work Orchestration', href: 'https://github.com/gprocunier/complex-work-orchestration'},
          {label: 'Optional CWO Integration', to: '/integrations/cwo/'},
        ]},
      ],
      copyright: `Copyright ${new Date().getFullYear()} Traceonaut contributors. Licensed under Apache-2.0.`,
    },
    prism: {
      theme: themes.github,
      darkTheme: themes.dracula,
      additionalLanguages: ['bash', 'python', 'yaml', 'promql'],
    },
    tableOfContents: {minHeadingLevel: 2, maxHeadingLevel: 3},
  },
};

module.exports = config;
