module.exports = {
  docs: [
    {type: 'doc', id: 'website/docs/home', label: 'Overview'},
    {type: 'doc', id: 'references/deployment', label: 'Getting Started'},
    {
      type: 'category', label: 'Dashboards', collapsed: false,
      items: [
        {type: 'doc', id: 'references/codex-beta-dashboard', label: 'Beta'},
        {type: 'doc', id: 'references/codex-unified-dashboard', label: 'Unified'},
        {type: 'link', label: 'Stable', href: '/data-and-limits/#add-the-stable-dashboard'},
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
