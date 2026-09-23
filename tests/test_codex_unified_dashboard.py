from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from render_codex_unified_dashboard import (  # noqa: E402
    UNIFIED_UID, render_dashboard, write_unified_dashboard,
)
from render_codex_sessions_dashboard import walk_panels  # noqa: E402

TEMPLATE = ROOT / 'examples/observability/codex-unified-overview.json'


class UnifiedDashboardTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads(TEMPLATE.read_text())
        self.snapshot = {'version': 1, 'sessions': [
            {'session_id': 'parent', 'project_id': 'source', 'title': 'Native work title',
             'project_name': 'CWO', 'agent_name': 'Primary agent', 'kind': 'session', 'parent_id': 'none'},
            {'session_id': 'child', 'project_id': 'source', 'title': 'Review, data',
             'project_name': 'CWO', 'agent_name': 'Subagent', 'kind': 'subagent', 'parent_id': 'parent'},
            {'session_id': 'internal', 'project_id': 'source', 'title': 'Internal work',
             'project_name': 'CWO', 'agent_name': 'Internal', 'kind': 'internal', 'parent_id': 'none'},
        ]}

    def test_canonical_titles_and_ids_agree_across_inventory_details_selector_and_bars(self):
        self.snapshot['sessions'][0].update(alias='Beta-only title', prompt='PRIVATE PROMPT')
        rendered = render_dashboard(self.template, self.snapshot, 'prometheus')
        panels = {p['id']: p for p in walk_panels(rendered['panels'])}
        work = next(v for v in rendered['templating']['list'] if v['name'] == 'session')
        self.assertEqual({o['value']: o['text'] for o in work['options']}['parent'], 'Native work title')
        self.assertIn(r'Review\, data : child', work['query'])
        for identity in (11, 41, 141, 42, 143):
            overrides = panels[identity]['fieldConfig']['overrides']
            field = next(o for o in overrides if o['matcher']['options'] == 'Work')
            mappings = next(p['value'] for p in field['properties'] if p['id'] == 'mappings')
            self.assertEqual(mappings[0]['options']['parent']['text'], 'Native work title')
            self.assertEqual(mappings[0]['options']['child']['text'], 'Review, data')
        for identity in (80, 81):
            field = next(o for o in panels[identity]['fieldConfig']['overrides'] if o['matcher']['options'] == 'parent')
            values = {v['id']: v['value'] for v in field['properties']}
            self.assertEqual(values['displayName'], 'Native work title')
            self.assertIn('var-session=parent&', values['links'][0]['url'])
            self.assertIn('${__url_time_range}', values['links'][0]['url'])
        self.assertNotIn('Beta-only title', json.dumps(rendered))
        self.assertNotIn('PRIVATE PROMPT', json.dumps(rendered))
        self.assertNotIn('Subagent · Subagent', json.dumps(rendered))

    def test_rendering_preserves_queries_and_template_and_binds_all_datasources(self):
        before = copy.deepcopy(self.template)
        result = render_dashboard(self.template, self.snapshot, 'prometheus')
        queries = lambda d: [(p['id'], t['refId'], t['expr']) for p in walk_panels(d['panels']) for t in p.get('targets', [])]
        self.assertEqual(queries(result), queries(before))
        self.assertEqual(self.template, before)
        self.assertNotIn('${DS_PROMETHEUS}', json.dumps(result))
        self.assertEqual(result['uid'], UNIFIED_UID)

    def test_output_cannot_replace_stable_or_beta_or_a_symlink(self):
        rendered = render_dashboard(self.template, self.snapshot)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dashboard.json'
            for uid in ('cwo-codex-beta', 'cwo-supervisor-observability-v1'):
                contents = json.dumps({'uid': uid})
                path.write_text(contents)
                with self.assertRaisesRegex(ValueError, 'another dashboard'):
                    write_unified_dashboard(path, rendered)
                self.assertEqual(path.read_text(), contents)
            path.unlink()
            self.assertTrue(write_unified_dashboard(path, rendered))
            self.assertFalse(write_unified_dashboard(path, rendered))
            link = Path(folder) / 'link.json'; link.symlink_to(path)
            with self.assertRaises(ValueError):
                write_unified_dashboard(link, rendered)

    def test_wrong_template_uid_is_rejected(self):
        for uid in ('cwo-codex-beta', 'cwo-supervisor-observability-v1'):
            self.template['uid'] = uid
            with self.assertRaisesRegex(ValueError, 'separate dashboard UID'):
                render_dashboard(self.template, self.snapshot)

    def test_oversized_output_never_creates_or_replaces_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dashboard.json'
            small = {'uid': UNIFIED_UID}
            with patch('render_codex_unified_dashboard.MAX_DASHBOARD_BYTES', 256):
                oversized = {**small, 'title': 'x' * 256}
                with self.assertRaisesRegex(ValueError, 'output size limit'):
                    write_unified_dashboard(path, oversized)
                self.assertFalse(path.exists())
                write_unified_dashboard(path, small)
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'output size limit'):
                    write_unified_dashboard(path, oversized)
                self.assertEqual(path.read_bytes(), before)

    def test_fixed_state_colors_and_native_unavailable_display(self):
        panels = {p['id']: p for p in walk_panels(self.template['panels'])}
        ring = panels[23]
        colors = {}
        for override in ring['fieldConfig']['overrides']:
            for prop in override['properties']:
                if prop['id'] == 'color': colors[override['matcher']['options']] = prop['value']['fixedColor']
        table = next(o for o in panels[11]['fieldConfig']['overrides'] if o['matcher']['options'] == 'State')
        mappings = next(p['value'] for p in table['properties'] if p['id'] == 'mappings')[0]['options']
        for mapping in mappings.values():
            self.assertEqual(colors[mapping['text']], mapping['color'])
        self.assertEqual(colors['Working'], 'blue')
        self.assertEqual(colors['Waiting'], '#D8D9DA')
        for identity in (4, 6, 7):
            self.assertEqual(panels[identity]['fieldConfig']['defaults']['noValue'], '—')
        for identity in (51, 52, 53, 54):
            self.assertEqual(panels[identity]['fieldConfig']['defaults']['noValue'], 'Unavailable')
        for field in ('Failures', 'Longest command', 'Commands'):
            override = next(o for o in panels[11]['fieldConfig']['overrides'] if o['matcher']['options'] == field)
            cell = next(p['value'] for p in override['properties'] if p['id'] == 'custom.cellOptions')
            self.assertEqual(cell['type'], 'auto')
        self.assertFalse(panels[34]['options']['showUnfilled'])
        self.assertFalse(panels[36]['options']['showUnfilled'])

    def test_scopes_navigation_and_distinct_diagnostic_sections(self):
        panels = {p['id']: p for p in walk_panels(self.template['panels'])}
        scope = panels[5]['options']['content']
        for uid in ('cwo-codex-beta', 'cwo-supervisor-observability-v1', UNIFIED_UID):
            self.assertIn('/d/' + uid, scope)
        self.assertIn('${project:queryparam}&var-session=$__all&${__url_time_range}', scope)
        rows = [p for p in self.template['panels'] if p['type'] == 'row' and p.get('collapsed')]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['collapsed'] for row in rows))
        self.assertIn('before this interval', panels[31]['options']['content'])
        self.assertIn('account-wide', panels[91]['description'].lower())
        self.assertNotIn('style=', json.dumps(self.template))
        self.assertEqual(panels[21]['gridPos']['h'], panels[23]['gridPos']['h'])
        self.assertEqual([panels[id]['gridPos']['w'] for id in (21, 23)], [16, 8])
        self.assertEqual(panels[21]['maxDataPoints'], 180)
        self.assertFalse(panels[21]['fieldConfig']['defaults']['custom']['spanNulls'])
        self.assertEqual(self.template['refresh'], '5s')

    def test_reading_flow_separates_state_role_accounting_and_time_bases(self):
        panels = {p['id']: p for p in walk_panels(self.template['panels'])}
        sections = [p['title'] for p in self.template['panels']
                    if p['type'] == 'row' and not p.get('collapsed')]
        self.assertEqual(sections, [
            'Overview', 'Account allowance · account-wide at range end',
            'Sessions and activity', 'Usage · recorded session history',
            'Time and execution',
        ])
        self.assertIn('subset of matching sessions', panels[7]['targets'][0]['legendFormat'])
        self.assertLess(panels[94]['gridPos']['y'], panels[151]['gridPos']['y'])
        self.assertLess(panels[80]['gridPos']['y'], panels[160]['gridPos']['y'])
        self.assertGreater(panels[81]['gridPos']['y'], panels[160]['gridPos']['y'])
        self.assertIn('not elapsed session time or user wait', panels[161]['options']['content'])
        self.assertGreaterEqual(panels[11]['gridPos']['h'], 16)
        for identity in (37, 51, 52, 53, 201):
            self.assertEqual(panels[identity]['fieldConfig']['defaults']['decimals'], 0)
        for identity in (158, 159):
            mappings = panels[identity]['fieldConfig']['defaults']['mappings'][0]['options']
            self.assertEqual(mappings['0']['text'], 'Incomplete range; unavailable values are unknown')
            self.assertEqual(mappings['-1']['text'], 'Command source stale or unavailable')
        self.assertLess(abs(panels[158]['gridPos']['y'] - panels[51]['gridPos']['y']), 2)
        self.assertLess(abs(panels[159]['gridPos']['y'] - panels[54]['gridPos']['y']), 2)
