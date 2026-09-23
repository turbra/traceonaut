// @ts-check
const {themes} = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Traceonaut',
  favicon: 'traceonaut-favicon.png',
  tagline: 'Codex session metrics for your existing Prometheus and Grafana.',
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
  presets: [
    ['classic', {
      docs: {
        // Render the public guides in place. Never crawl private repo state.
        path: '..',
        include: [
          'website/docs/home.mdx',
          'references/installation.md',
          'references/deployment.md',
          'references/operations.md',
          'references/codex-beta-dashboard.md',
          'references/codex-unified-dashboard.md',
          'references/cwo-dashboard.md',
          'references/codex-all-sessions-observability.md',
          'references/cwo-integration.md',
          'references/terminal-observation-export.md',
        ],
        routeBasePath: '/',
        sidebarPath: './sidebars.js',
        editUrl: 'https://github.com/turbra/traceonaut/edit/main/',
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
        {label: 'Dashboards', position: 'left', items: [
          {to: '/dashboards/beta/', label: 'Work overview (Beta)'},
          {to: '/dashboards/unified/', label: 'Unified overview'},
          {to: '/data-and-limits/#add-the-stable-dashboard', label: 'All sessions (Stable)'},
          {to: '/dashboards/cwo/', label: 'CWO observed dispatches'},
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
          {label: 'Dashboards', to: '/#dashboards'},
          {label: 'Data and Limits', to: '/data-and-limits/'},
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
