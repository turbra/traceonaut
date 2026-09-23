// @ts-check
const {themes} = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Traceonaut',
  tagline: 'Codex session metrics for your existing Prometheus and Grafana.',
  url: 'https://turbra.github.io',
  baseUrl: '/traceonaut/',
  trailingSlash: true,
  organizationName: 'turbra',
  projectName: 'traceonaut',
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
          'references/deployment.md',
          'references/operations.md',
          'references/codex-beta-dashboard.md',
          'references/codex-unified-dashboard.md',
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
    colorMode: {defaultMode: 'light', respectPrefersColorScheme: true},
    navbar: {
      title: 'Traceonaut',
      items: [
        {to: '/', label: 'Docs', position: 'left', activeBaseRegex: '^/traceonaut/$'},
        {to: '/getting-started/', label: 'Getting Started', position: 'left'},
        {to: '/dashboards/beta/', label: 'Dashboards', position: 'left', activeBasePath: 'dashboards'},
        {to: '/operations/', label: 'Operations', position: 'left'},
        {href: 'https://github.com/turbra/traceonaut', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'light',
      links: [
        {title: 'Docs', items: [
          {label: 'Getting Started', to: '/getting-started/'},
          {label: 'Dashboards', to: '/dashboards/beta/'},
          {label: 'Data and Limits', to: '/data-and-limits/'},
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
