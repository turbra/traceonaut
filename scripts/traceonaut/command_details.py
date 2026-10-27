"""Bounded action names and opt-in command descriptions; no shell execution."""
from __future__ import annotations

import math
from pathlib import PurePosixPath
import re

MAX_DISPLAY = 768
MAX_DIRECTORY = 256
GIT_ACTIONS = frozenset('add am archive bisect blame branch bundle checkout cherry cherry-pick clean clone commit config describe diff fetch format-patch gc grep init log ls-files ls-remote merge mv pull push range-diff rebase reflog remote reset restore revert rev-list rev-parse rm show sparse-checkout stash status submodule switch tag worktree'.split())
CLI_ACTIONS = {
    'podman': frozenset('build exec images info inspect kill logs network pod ps pull push restart rm rmi run start stats stop version volume'.split()),
    'docker': frozenset('build compose exec images info inspect kill logs network ps pull push restart rm rmi run start stats stop version volume'.split()),
    'kubectl': frozenset('apply auth config create delete describe diff edit exec explain get logs patch rollout run scale top version wait'.split()),
    'oc': frozenset('apply auth config create delete describe diff edit exec explain get logs patch project rollout run scale top version wait'.split()),
    'npm': frozenset('audit ci exec install ls outdated pack publish run test uninstall update version'.split()),
    'bd': frozenset('blocked close comments create dep dolt list ready search show status sync update'.split()),
}
PATH_VALUE = re.compile(r'[A-Za-z0-9_./~@:+,=*-]{1,240}\Z')


def safe_text(value, limit=MAX_DISPLAY):
    if not isinstance(value, str):
        return None
    value = ''.join(c if c.isprintable() and c not in '\r\n\t' else ' ' for c in value)
    # Prevent Grafana display-name template interpolation, not shell expansion.
    value = re.sub(r'\$|\[\[|\]\]', '_', value).strip()
    return value[:limit].rstrip() or None


def _operand(value):
    if not isinstance(value, str) or not PATH_VALUE.fullmatch(value):
        return '[omitted]'
    # URLs, assignments and template syntax can carry credentials.
    if '://' in value or '=' in value or value.startswith('-'):
        return '[omitted]'
    return safe_text(value) or '[omitted]'


def _git_index(words):
    index = 1
    while index < len(words):
        word = words[index]
        if word == '--':
            return index + 1
        if not word.startswith('-'):
            return index
        if word in {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'}:
            index += 2
        elif word.startswith(('-C', '-c', '--git-dir=', '--work-tree=', '--namespace=', '--config-env=')):
            index += 1
        elif word in {'--no-pager', '--paginate', '--bare', '--no-optional-locks', '--literal-pathspecs', '--no-replace-objects'}:
            index += 1
        else:
            return None
    return None


def action_name(words):
    name = PurePosixPath(words[0]).name
    if name == 'git':
        index = _git_index(words)
        action = words[index] if index is not None and index < len(words) else None
        return 'git ' + (action if action in GIT_ACTIONS else 'other')
    if name in CLI_ACTIONS:
        action = words[1] if len(words) > 1 else None
        return name + ' ' + (action if action in CLI_ACTIONS[name] else 'other')
    if name == 'tail' and any(v in {'-f', '-F', '--follow', '--follow=name', '--follow=descriptor'} for v in words[1:]):
        return 'tail -f'
    return name


def _arguments(words, *, flags=(), numeric=(), paths=False):
    result, index = [], 0
    while index < len(words) and index < 64:
        word = words[index]
        if word in flags or word == '--':
            result.append(word)
        elif word in numeric and index + 1 < len(words) and re.fullmatch(r'[0-9]{1,8}', words[index+1]):
            result.extend((word, words[index+1])); index += 1
        elif any(word.startswith(flag) and re.fullmatch(r'[0-9]{1,8}', word[len(flag):])
                 for flag in numeric if flag.startswith('-') and not flag.startswith('--')):
            result.append(word)
        elif paths and not word.startswith('-'):
            result.append(_operand(word))
        else:
            result.append('[arguments omitted]')
            break
        index += 1
    if index < len(words) and (not result or result[-1] != '[arguments omitted]'):
        result.append('[arguments omitted]')
    return result


def _display_argv(words, depth=0):
    if depth > 4 or not words:
        return '[command omitted]'
    words = list(words)
    while words and re.match(r'[A-Za-z_][A-Za-z0-9_]*=', words[0]):
        words.pop(0)
    if not words:
        return '[environment assignments omitted]'
    name = PurePosixPath(words[0]).name
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.+-]{0,79}', name):
        return '[command omitted]'
    if name in {'sh', 'bash', 'zsh', 'dash'} and len(words) == 3 and words[1] in {'-c', '-lc', '-cl'}:
        return _display_shell(words[2], depth+1)
    # Wrapper details are intentionally omitted. Action extraction independently
    # handles supported wrappers; an audit summary never exposes env values.
    if name in {'env', 'sudo', 'command', 'exec'}:
        from .shell_command_names import command_names
        actions, _ = command_names(words, actions=True)
        return ('; '.join(actions) if actions else name) + ' [wrapper arguments omitted]'
    if name == 'git':
        index = _git_index(words)
        if index is None or index >= len(words) or words[index] not in GIT_ACTIONS:
            return 'git [arguments omitted]'
        sub = words[index]
        prefix = ['git'] + (['[global options omitted]'] if index > 1 else []) + [sub]
        remaining = words[index+1:]
        path_actions = {'add', 'branch', 'checkout', 'clean', 'clone', 'diff', 'fetch', 'init',
                        'log', 'merge', 'mv', 'pull', 'push', 'reset', 'restore', 'revert',
                        'rm', 'show', 'status', 'switch'}
        if sub not in path_actions:
            return ' '.join(prefix + (['[arguments omitted]'] if remaining else []))
        flags = {'--all', '-a', '--force', '-f', '--force-with-lease', '--dry-run', '-n',
                 '--verbose', '-v', '--quiet', '-q', '--short', '-s', '--branch', '-b',
                 '--porcelain', '--stat', '--name-only', '--cached', '--staged', '--oneline',
                 '--no-pager', '--rebase', '--ff-only', '--prune', '--tags'}
        if sub == 'push':
            flags.update({'-u', '--set-upstream'})
        return ' '.join(prefix + _arguments(remaining, flags=flags, numeric={'--depth'}, paths=True))
    if name in {'cat', 'tail', 'head', 'ls', 'pwd', 'wc'}:
        flags = {'-f', '-F', '--follow', '--follow=name', '--follow=descriptor', '-n', '-b', '-s', '-A', '-E', '-T',
                 '-v', '-q', '-l', '-c', '-w', '-m', '-a', '-h', '-la', '-lh', '-lah', '-al', '-alh'}
        numeric = {'-n', '-c', '--lines', '--bytes'} if name in {'tail', 'head'} else set()
        return ' '.join([name] + _arguments(words[1:], flags=flags - numeric, numeric=numeric, paths=True))
    if name in {'python', 'python3', 'node', 'perl', 'ruby', 'sh', 'bash', 'zsh', 'dash', 'eval', 'source'}:
        return name + (' [program and arguments omitted]' if len(words) > 1 else '')
    if name == 'curl':
        flags = {'-I', '--head', '-s', '--silent', '-S', '--show-error', '-L', '--location', '-f', '--fail'}
        return ' '.join([name] + _arguments(words[1:], flags=flags))
    action = action_name(words)
    return action + (' [arguments omitted]' if len(words) > 1 else '')


def _display_shell(text, depth):
    from .shell_command_names import _parts, command_names
    if depth > 4 or len(text) > 128*1024:
        return '[shell program omitted]'
    _, complete = command_names(['bash', '-c', text])
    if not complete:
        return '[shell program omitted; syntax coverage partial]'
    try:
        parts = _parts(text)
    except (ValueError, IndexError):
        return '[shell program omitted]'
    words, output, redirects, index = [], [], [], 0
    while index < len(parts):
        value, operator, raw = parts[index]
        if operator and value in {'>', '>>', '<', '<<', '<<-', '<<<', '>&', '<&', '&>'}:
            destination = parts[index+1][0]
            redirects.extend((raw, '[input omitted]' if value in {'<<', '<<-', '<<<'} else _operand(destination)))
            index += 2
            continue
        if operator:
            if words or redirects:
                output.append(' '.join([_display_argv(words, depth) if words else '[redirection only]', *redirects]))
                words, redirects = [], []
            output.append(';' if value == '\n' else value)
        else:
            words.append(value)
        index += 1
    if words or redirects:
        output.append(' '.join([_display_argv(words, depth) if words else '[redirection only]', *redirects]))
    return ' '.join(output) or '[empty shell invocation]'


def invocation_details(item):
    command = item.get('command')
    if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command) or sum(map(len, command)) > 128*1024:
        display = '[command omitted]'
    else:
        display = _display_argv(command)
    code = item.get('exit_code')
    code = code if type(code) is int and -255 <= code <= 255 else None
    state = item.get('status')
    outcome = 'completed' if state == 'completed' and code == 0 else 'failed' if state == 'failed' and code is not None and code != 0 else 'unknown'
    duration = item.get('duration')
    elapsed = None
    if isinstance(duration, dict):
        secs, nanos = duration.get('secs'), duration.get('nanos')
        if type(secs) is int and type(nanos) is int and 0 <= secs <= 2**53-1 and 0 <= nanos < 10**9:
            value = secs + nanos / 10**9
            if math.isfinite(value) and value <= 2**53-1:
                elapsed = value
    return {'summary': safe_text(display), 'directory': safe_text(item.get('cwd'), MAX_DIRECTORY),
            'exit_code': code, 'duration': elapsed, 'outcome': outcome}


def validate_history(value):
    """Bounded presentation-only projection, excluding collector internals."""
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('invalid command history')
    scope = value.get('scope', 'cwo')
    if scope not in {'cwo', 'all'}:
        raise ValueError('invalid command history scope')
    if not value['enabled']:
        return {'enabled': False, 'rows': []}
    rows = value.get('rows')
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('invalid command history rows')
    seen, result = set(), []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not re.fullmatch('[0-9a-f]{64}', row['id']) or row['id'] in seen:
            raise ValueError('invalid command history identity')
        seen.add(row['id'])
        entry = {'id': row['id']}
        for field, limit in (('summary', MAX_DISPLAY), ('directory', MAX_DIRECTORY)):
            text = row.get(field)
            if text is not None and (not isinstance(text, str) or not text or safe_text(text, limit) != text):
                raise ValueError('invalid command history display')
            entry[field] = text
        result.append(entry)
    return {'enabled': True, 'scope': scope, 'rows': result}
