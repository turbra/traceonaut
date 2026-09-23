module.exports = {
  docs: [
    {type: 'doc', id: 'website/docs/home', label: 'Documentation'},
    {
      type: 'category', label: 'Getting Started', collapsed: false,
      items: [
        {type: 'doc', id: 'references/installation', label: 'Install'},
        {type: 'doc', id: 'references/deployment', label: 'Quick Start'},
      ],
    },
    {
      type: 'category', label: 'Dashboards', collapsed: false,
      items: [
        {type: 'doc', id: 'references/codex-beta-dashboard', label: 'Work overview (Beta)'},
        {type: 'doc', id: 'references/codex-unified-dashboard', label: 'Unified overview'},
        {type: 'link', label: 'All sessions (Stable)', href: '/data-and-limits/#add-the-stable-dashboard'},
        {type: 'doc', id: 'references/cwo-dashboard', label: 'CWO observed dispatches'},
      ],
    },
    {type: 'doc', id: 'references/codex-all-sessions-observability', label: 'Data and Limits'},
    {type: 'doc', id: 'references/operations', label: 'Operations'},
    {
      type: 'category', label: 'Optional Integrations', collapsed: true,
      items: [
        {type: 'doc', id: 'references/cwo-integration', label: 'CWO'},
        {type: 'doc', id: 'references/terminal-observation-export', label: 'Completed Dispatch Export'},
      ],
    },
  ],
};
