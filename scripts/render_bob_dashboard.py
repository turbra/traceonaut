#!/usr/bin/env python3
"""Render private Bob chat names into the IBM Bob beta dashboard."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import signal
import sys
import threading

from render_codex_sessions_dashboard import (
    _display_name, _mapping, _named_variable, _object_without_duplicates,
    _read_snapshot_bytes, write_dashboard,
)


def validate_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 1 or
            snapshot.get('source') != 'ibm-bob' or not isinstance(snapshot.get('sessions'), list)):
        raise ValueError('unsupported Bob snapshot')
    rows, seen = [], set()
    if len(snapshot['sessions']) > 100_000:
        raise ValueError('too many Bob chats')
    for row in snapshot['sessions']:
        if not isinstance(row, dict):
            raise ValueError('invalid Bob chat')
        for key in ('session_id', 'project_id'):
            if not isinstance(row.get(key), str) or not re.fullmatch('[a-f0-9]{64}', row[key]):
                raise ValueError('invalid Bob identity')
        if row['session_id'] in seen:
            raise ValueError('duplicate Bob identity')
        seen.add(row['session_id'])
        result = {key: row[key] for key in ('session_id', 'project_id')}
        for key in ('title', 'project_name'):
            if not isinstance(row.get(key), str) or len(row[key]) > 120:
                raise ValueError('invalid Bob display name')
            result[key] = _display_name(' '.join(row[key].split()), 'Name unavailable')
        rows.append(result)
    return rows


def render_dashboard(template, snapshot, datasource_uid=None):
    rows = validate_snapshot(snapshot)
    if template.get('uid') != 'traceonaut-ibm-bob-beta':
        raise ValueError('expected IBM Bob beta template')
    result = copy.deepcopy(template)
    projects, chats = {}, {}
    for row in rows:
        projects.setdefault(row['project_id'], row['project_name'] or 'Project')
        chats[row['session_id']] = row['title'] or 'Untitled chat'
    # Disambiguate only collisions, keeping ordinary names short enough for tables.
    for names in (projects, chats):
        counts = {}
        for name in names.values():
            counts[name] = counts.get(name, 0) + 1
        for key, name in list(names.items()):
            if counts[name] > 1:
                names[key] = name + ' · ' + key[:8]
    for variable in result['templating']['list']:
        _named_variable(variable, projects if variable['name'] == 'project' else chats)
    for panel in result['panels']:
        if panel['type'] == 'table':
            for name, mapping in (('Chat', chats), ('Project', projects)):
                panel['fieldConfig']['overrides'].append({'matcher': {'id': 'byName', 'options': name},
                    'properties': [{'id': 'mappings', 'value': _mapping(mapping, 'Name unavailable')} ]})
    if datasource_uid:
        def replace(value):
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace(item) for item in value]
            return datasource_uid if value == '${DS_PROMETHEUS}' else value
        result = replace(result)
        result.pop('__inputs', None)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--snapshot-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--datasource-uid')
    parser.add_argument('--watch-seconds', type=float)
    args = parser.parse_args(argv)
    if args.watch_seconds is not None and not 1 <= args.watch_seconds <= 60:
        parser.error('--watch-seconds must be between 1 and 60')
    if args.output.absolute() == args.snapshot_file.absolute() or args.output.absolute() == args.template.absolute():
        parser.error('output must be separate from snapshot and template')
    stopping = threading.Event()
    if args.watch_seconds is not None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stopping.set())
    try:
        while True:
            template = json.loads(args.template.read_text())
            snapshot = json.loads(_read_snapshot_bytes(args.snapshot_file), object_pairs_hook=_object_without_duplicates)
            dashboard = render_dashboard(template, snapshot, args.datasource_uid)
            changed = write_dashboard(args.output, dashboard)
            if changed or args.watch_seconds is None:
                print(json.dumps({'status': 'rendered', 'changed': changed, 'named_chats': len(snapshot['sessions'])}), flush=True)
            if args.watch_seconds is None or stopping.wait(args.watch_seconds):
                break
    except FileNotFoundError as error:
        if error.filename and Path(error.filename).absolute() == args.snapshot_file.absolute():
            print(f'Dashboard render unavailable: snapshot file not found: {args.snapshot_file}', file=sys.stderr)
        else:
            print('Bob dashboard render unavailable: check template, protected snapshot and output.', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print('Bob dashboard render unavailable: check template, protected snapshot and output.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
