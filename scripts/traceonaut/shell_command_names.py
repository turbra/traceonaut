"""Extract literal command names as data; never execute or expand shell text."""
from pathlib import PurePosixPath
import re
import shlex

MAX_TEXT = 128 * 1024
MAX_COMMANDS = 128
SHELLS = {'sh', 'bash', 'zsh', 'dash'}
CONTROL = {'if', 'then', 'elif', 'else', 'fi', 'for', 'while', 'until', 'do',
           'done', 'case', 'esac', 'select', 'function', '{', '}'}
WORD = re.compile(r'''(?:[^\s;&|<>()'"\\]+|\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*")+''')
OPERATOR = re.compile(r'<<-|[;&|<>()]+|\n')
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')


def _parts(text):
    """Quote-aware tokens; heredoc bodies are skipped, including apparent commands."""
    parts, pending, position, last_end = [], [], 0, -1
    expect_here = None
    while position < len(text):
        if text[position] in ' \t\r':
            position += 1
            continue
        if text.startswith('\\\n', position):
            position += 2
            continue
        if text[position] == '#':
            end = text.find('\n', position)
            position = len(text) if end < 0 else end
            continue
        match = OPERATOR.match(text, position)
        operator = match is not None
        match = match or WORD.match(text, position)
        if match is None:
            raise ValueError('unsupported shell token')
        raw = match.group()
        value = raw if operator else shlex.split(raw, posix=True)[0]
        if operator and raw[0] in '<>' and parts and position == last_end:
            if re.fullmatch(r'[0-9]+', parts[-1][2]):
                parts.pop()  # Descriptor, e.g. 2>log; not a command or argument.
        parts.append((value, operator, raw))
        position = last_end = match.end()
        if expect_here is not None:
            if operator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
                raise ValueError('unsupported heredoc delimiter')
            pending.append((value, expect_here))
            expect_here = None
        elif operator and value in {'<<', '<<-'}:
            expect_here = value == '<<-'
        if operator and value == '\n':
            for delimiter, tabs in pending:
                while True:
                    if position >= len(text):
                        raise ValueError('unclosed heredoc')
                    end = text.find('\n', position)
                    end = len(text) if end < 0 else end
                    line = text[position:end]
                    position = min(end + 1, len(text))
                    if (line.lstrip('\t') if tabs else line) == delimiter:
                        break
            pending.clear()
    if pending or expect_here is not None:
        raise ValueError('unclosed heredoc')
    return parts


def _argv(words, depth):
    words = list(words)
    while words and ASSIGNMENT.match(words[0]):
        words.pop(0)
    if not words:
        return [], True
    name = PurePosixPath(words[0]).name
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.+-]{0,79}', name):
        return [], False
    if name in CONTROL:
        return [], False
    if name in {'command', 'exec', 'env', 'sudo'}:
        words.pop(0)
        while words:
            word = words[0]
            if word == '--':
                words.pop(0)
                break
            if name == 'env' and ASSIGNMENT.match(word):
                words.pop(0)
                continue
            if not word.startswith('-'):
                break
            flags = {'command': {'-p'}, 'exec': {'-c', '-l'},
                     'env': {'-i', '--ignore-environment'},
                     'sudo': {'-n', '-E', '-H', '-S', '-k', '-K', '-b'}}[name]
            values = {'command': set(), 'exec': {'-a'},
                      'env': {'-u', '--unset', '-C', '--chdir'},
                      'sudo': {'-u', '--user', '-g', '--group', '-h', '--host',
                               '-p', '--prompt', '-C', '--close-from', '-T', '--command-timeout'}}[name]
            if word in flags:
                words.pop(0)
            elif word in values and len(words) > 1:
                del words[:2]
            elif name == 'sudo' and word.startswith('--preserve-env='):
                words.pop(0)
            elif any(word.startswith(flag+'=') for flag in values if flag.startswith('--')):
                words.pop(0)
            else:
                return [], False
        return command_names(words, depth=depth+1) if words else ([], False)
    if name in SHELLS and len(words) == 3 and words[1] in {'-c', '-lc', '-cl'}:
        return _script(words[2], depth+1)
    # A visible interpreter counts once; its program and subprocesses stay opaque.
    opaque = name in SHELLS or name in {'eval', 'source'}
    return [name], not opaque


def _script(text, depth):
    if depth > 4 or len(text) > MAX_TEXT:
        return [], False
    try:
        parts = _parts(text)
    except (ValueError, IndexError):
        return [], False
    # Loops/functions/substitutions need a shell grammar or runtime evidence.
    # Reject them rather than treating quoted programs or bodies as invocations.
    def substitution(raw):
        # Single-quoted and escaped characters are literal shell data.
        active = re.sub(r"'[^']*'|\\[\s\S]", '', raw)
        return '$(' in active or '`' in active

    if any((operator and value in {'(', ')', '((', '))'}) or
           (not operator and substitution(raw))
           for value, operator, raw in parts):
        return [], False
    groups, words, index = [], [], 0
    while index < len(parts):
        value, operator, raw = parts[index]
        if operator and value in {'>', '>>', '<', '<<', '<<-', '<<<', '>&', '<&', '&>'}:
            if index+1 >= len(parts) or parts[index+1][1]:
                return [], False
            index += 2
            continue
        if operator:
            if value not in {';', '\n', '&&', '||', '|', '|&', '&'}:
                return [], False
            if words:
                groups.append(words)
                words = []
            elif value not in {';', '\n'}:
                return [], False
        else:
            if not words and value in CONTROL and raw == value:
                return [], False
            words.append(value)
        index += 1
    if words:
        groups.append(words)
    names, complete = [], True
    for group in groups:
        found, supported = _argv(group, depth)
        names.extend(found)
        complete = complete and supported
        if len(names) > MAX_COMMANDS:
            return names[:MAX_COMMANDS], False
    return names, complete


def command_names(command, *, depth=0):
    """Return (literal executable occurrences, complete syntax coverage)."""
    if (depth > 4 or not isinstance(command, list) or not command
            or any(not isinstance(word, str) or '\0' in word for word in command)
            or sum(map(len, command)) > MAX_TEXT):
        return [], False
    return _argv(command, depth)
