"""Literal curl endpoints and narrowly attributed connection reports, without I/O."""
from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

from .shell_command_names import ASSIGNMENT, MAX_TEXT, SHELLS, _parts, command_names

MAX_ENTRIES = 8
MAX_PAYLOAD_FILES = 4
MAX_PAYLOAD_PATHS_TEXT = 768
HTTP_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT'})
_PAYLOAD_OPTIONS = frozenset({'--data', '--data-raw', '--data-binary', '--data-urlencode',
                              '--json', '--form', '--form-string', '--upload-file'})
WRITE_OUT = 'TRACEONAUT_CONNECTION {"local_ip":"%{local_ip}"}\\n'
_REPORT_FORMATS = {WRITE_OUT, WRITE_OUT[:-2] + '\n'}
_REPORT = re.compile(r'TRACEONAUT_CONNECTION \{"local_ip":"([^"\\\r\n]{0,64})"\}\n\Z')
_REDIRECTS = {'>', '>>', '<', '<<', '<<-', '<<<', '>&', '<&', '&>'}
_WRAPPER_FLAGS = {
    'command': {'-p'}, 'exec': {'-c', '-l'},
    'env': {'-i', '--ignore-environment'},
    'sudo': {'-n', '-E', '-H', '-S', '-k', '-K', '-b'},
}
_WRAPPER_VALUES = {
    'command': set(), 'exec': {'-a'},
    'env': {'-u', '--unset', '-C', '--chdir'},
    'sudo': {'-u', '--user', '-g', '--group', '-h', '--host', '-p', '--prompt',
             '-C', '--close-from', '-T', '--command-timeout'},
}
_CURL_FLAGS = {
    '--silent', '--show-error', '--head', '--location', '--location-trusted',
    '--fail', '--fail-with-body', '--insecure', '--verbose', '--disable',
    '--ipv4', '--ipv6', '--http1.0', '--http1.1', '--http2', '--http2-prior-knowledge',
    '--http3', '--compressed', '--globoff', '--include', '--no-progress-meter',
    '--proxy-insecure', '--path-as-is', '--remote-name', '--remote-name-all', '--get',
}
_CURL_VALUES = {
    '--url', '--output', '--write-out', '--header', '--user', '--proxy-user',
    '--data', '--data-ascii', '--data-raw', '--data-binary', '--data-urlencode', '--json',
    '--form', '--form-string', '--cookie', '--cookie-jar', '--user-agent', '--referer',
    '--cert', '--key', '--cacert', '--capath', '--proxy', '--preproxy', '--noproxy',
    '--resolve', '--connect-to', '--interface', '--max-time', '--connect-timeout',
    '--retry', '--retry-delay', '--retry-max-time', '--request', '--upload-file',
    '--dump-header', '--stderr', '--proto', '--proto-redir', '--max-redirs',
}
_SHORT_FLAGS = frozenset('sSILfkvq046giOG')
_SHORT_VALUES = {
    'o': '--output', 'w': '--write-out', 'H': '--header', 'u': '--user',
    'U': '--proxy-user', 'd': '--data', 'F': '--form', 'b': '--cookie',
    'c': '--cookie-jar', 'A': '--user-agent', 'e': '--referer', 'E': '--cert',
    'x': '--proxy', 'm': '--max-time', 'X': '--request', 'T': '--upload-file',
    'D': '--dump-header',
}


def _unwrap(words):
    """Match the command-name parser's supported wrappers without retaining values."""
    words = list(words)
    for _ in range(5):
        while words and ASSIGNMENT.match(words[0]):
            words.pop(0)
        if not words:
            return []
        name = PurePosixPath(words[0]).name
        if name not in _WRAPPER_FLAGS:
            return words
        words.pop(0)
        while words:
            word = words[0]
            if word == '--':
                words.pop(0)
                break
            if name == 'env' and ASSIGNMENT.match(word):
                words.pop(0)
            elif not word.startswith('-'):
                break
            elif word in _WRAPPER_FLAGS[name]:
                words.pop(0)
            elif word in _WRAPPER_VALUES[name] and len(words) > 1:
                del words[:2]
            elif (name == 'sudo' and word.startswith('--preserve-env=')) or any(
                    word.startswith(flag + '=') for flag in _WRAPPER_VALUES[name]
                    if flag.startswith('--')):
                words.pop(0)
            else:
                return []
    return []


def _commands(command, depth=0):
    """Return simple argv groups and whether their stdout has no shell redirects."""
    if depth > 4:
        return []
    words = _unwrap(command)
    if not words:
        return []
    if PurePosixPath(words[0]).name not in SHELLS:
        return [(words, True)]
    if len(words) != 3 or words[1] not in {'-c', '-lc', '-cl'}:
        return []
    parts = _parts(words[2])
    groups, group, redirected, index = [], [], False, 0
    controlled = False
    while index < len(parts):
        value, operator, _ = parts[index]
        if operator and value in _REDIRECTS:
            redirected = True
            index += 2
            continue
        if operator:
            controlled = controlled or value in {'&&', '||', '|', '|&', '&'}
            if group or redirected:
                groups.append((group, redirected))
            group, redirected = [], False
        else:
            group.append(value)
        index += 1
    if group or redirected:
        groups.append((group, redirected))
    result = []
    for group, redirected in groups:
        nested = _commands(group, depth + 1)
        # Empty/opaque groups still prevent attributing combined stdout to curl.
        result.extend((argv, safe and not redirected and not controlled) for argv, safe in nested)
        if not nested:
            result.append(([], False))
    return result


def _destination(value):
    if (not isinstance(value, str) or len(value) > 8192
            or any(c.isspace() or not c.isprintable() for c in value)
            or any(c in value for c in '$`\\{}')):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.netloc:
            return None
        host, port = parsed.hostname, parsed.port
        if not host or '%' in host or parsed.netloc.endswith(':'):
            return None
        if any(c in parsed.path + parsed.query + parsed.fragment for c in '[]'):
            return None  # Curl URL globbing can produce multiple transfers.
        if port is not None and not 1 <= port <= 65535:
            return None
        if ':' in host:
            host = '[' + str(ipaddress.IPv6Address(host)) + ']'
        else:
            host = host.encode('idna').decode('ascii').lower()
            if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                          for label in host.rstrip('.').split('.')):
                return None
            if re.fullmatch(r'[0-9.]+', host):
                host = str(ipaddress.IPv4Address(host))
        return parsed.scheme.lower() + '://' + host + (':' + str(port) if port is not None else '')
    except (ValueError, UnicodeError):
        return None


def _literal_path(value):
    """Accept a small, readable literal-path grammar, never expanding or reading it."""
    if (not value or len(value) > MAX_PAYLOAD_PATHS_TEXT or value != value.strip()
            or '://' in value or not re.fullmatch(r'[A-Za-z0-9_./ @:+-]+', value)):
        return None
    return value


def _file_source(value, upload=False):
    if value == '-' or (upload and value == '.'):
        return 'Stdin', []
    path = _literal_path(value)
    return ('File', [path]) if path is not None else ('Unknown', [])


def _form_parts(value, separator):
    """Split curl's inner quoted form syntax, without interpreting escapes."""
    parts, start, quoted = [], 0, False
    for index, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            parts.append(value[start:index])
            start = index + 1
    if quoted or '\\' in value:
        return None
    return [*parts, value[start:]]


def _form_source(value):
    field, equals, content = value.partition('=')
    if not equals or not field or any(c in field for c in ';,()'):
        return 'Unknown', []
    if content.startswith(('(', ')')):
        return 'Unknown', []  # Nested multipart syntax is not projected.
    if not content.startswith(('@', '<')):
        return 'Inline', []
    files = _form_parts(content[1:], ',')
    if files is None or (content.startswith('<') and len(files) != 1):
        return 'Unknown', []
    if any(';' in file for file in files[:-1]):
        # A comma inside unquoted file-option metadata is ambiguous here. Do not
        # expose its trailing value as another selected file.
        return 'Unknown', []
    kinds, paths = set(), []
    for file in files:
        parts = _form_parts(file, ';')
        if parts is None:
            return 'Unknown', []
        path = parts[0]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        for option in parts[1:]:
            name, equals, ignored = option.partition('=')
            if not equals or name not in {'type', 'filename', 'headers', 'encoder'} or not ignored:
                return 'Unknown', []
        kind, selected = _file_source(path)
        if kind == 'Unknown':
            return kind, []
        kinds.add(kind)
        paths.extend(selected)
    return (next(iter(kinds)) if len(kinds) == 1 else 'Mixed'), paths


def _payload_source(name, value):
    # Raw and form-string values cannot select files through @ or < syntax.
    if name == '--data-raw':
        return 'Inline', []
    if name == '--form-string':
        field, equals, _ = value.partition('=')
        return ('Inline', []) if field and equals else ('Unknown', [])
    if any(c in value for c in '$`') or any(not c.isprintable() for c in value):
        return 'Unknown', []
    if name == '--upload-file':
        return _file_source(value, upload=True)
    if name == '--form':
        return _form_source(value)
    if name == '--data-urlencode':
        if '=' in value or '@' not in value:
            return 'Inline', []
        return _file_source(value.split('@', 1)[1])
    if value.startswith('@'):
        return _file_source(value[1:])
    return 'Inline', []


def _upload_details(payloads, requests, head, get):
    """Project request intent, not whether a body was read, transmitted, or accepted."""
    families, kinds, paths, names = set(), set(), [], []
    for name, value in payloads:
        families.add('form' if name in {'--form', '--form-string'} else
                     'upload' if name == '--upload-file' else 'data')
        if name not in names:
            names.append(name)
        kind, selected = _payload_source(name, value)
        kinds.add(kind)
        for path in selected:
            if path not in paths:
                paths.append(path)
    conflict = (len(families) > 1 or sum(name == '--upload-file' for name, _ in payloads) > 1
                or (head and bool(payloads) and not (get and families == {'data'}))
                or (get and bool(families - {'data'})))
    if conflict or 'Unknown' in kinds:
        kind = 'Unknown'
    elif not kinds:
        kind = 'None'
    else:
        kind = next(iter(kinds)) if len(kinds) == 1 else 'Mixed'
    files = '; '.join(paths)
    if len(paths) > MAX_PAYLOAD_FILES or len(files) > MAX_PAYLOAD_PATHS_TEXT:
        kind = 'Unknown'
    if kind == 'Unknown':
        files = ''
    if conflict or (requests and (len(set(requests)) != 1 or requests[0] not in HTTP_METHODS)):
        method = 'Unknown'
    elif requests:
        method = requests[0]
    elif head:
        method = 'HEAD'
    elif get:
        method = 'GET'
    elif families == {'upload'}:
        method = 'PUT'
    elif families:
        method = 'POST'
    else:
        method = 'GET'
    return {'http_method': method, 'payload_kind': kind, 'payload_files': files,
            'upload_options': ' '.join(names)}


def _curl(words):
    """Recognize option arity; unknown/config/multiple-transfer syntax is omitted."""
    urls, outputs, reports, payloads, requests = [], [], [], [], []
    source_safe, options, index = True, True, 1
    head, get = False, False

    def option(name, value):
        if name == '--url':
            urls.append(value)
        elif name == '--output':
            outputs.append(value)
        elif name == '--write-out':
            reports.append(value)
        elif name == '--request':
            requests.append(value)
        elif name == '--data-ascii':
            payloads.append(('--data', value))
        elif name in _PAYLOAD_OPTIONS:
            payloads.append((name, value))

    while index < len(words):
        word = words[index]
        if options and word == '--':
            options = False
        elif options and word.startswith('--'):
            name, equals, value = word.partition('=')
            if name in _CURL_FLAGS and not equals:
                head = head or name == '--head'
                get = get or name == '--get'
                if name in {'--remote-name', '--remote-name-all'}:
                    source_safe = False
            elif name in _CURL_VALUES:
                if not equals:
                    index += 1
                    if index >= len(words):
                        return None
                    value = words[index]
                option(name, value)
            else:
                return None
        elif options and word.startswith('-'):
            position = 1
            if len(word) == 1:
                return None
            while position < len(word):
                flag = word[position]
                if flag in _SHORT_FLAGS:
                    head = head or flag == 'I'
                    get = get or flag == 'G'
                    if flag == 'O':
                        source_safe = False
                    position += 1
                elif flag in _SHORT_VALUES:
                    value = word[position + 1:]
                    if not value:
                        index += 1
                        if index >= len(words):
                            return None
                        value = words[index]
                    option(_SHORT_VALUES[flag], value)
                    break
                else:
                    return None
        else:
            urls.append(word)
        index += 1
    if len(urls) != 1:
        return None
    endpoint = _destination(urls[0])
    if endpoint is None:
        return None
    report = source_safe and outputs == ['/dev/null'] and len(reports) == 1 and reports[0] in _REPORT_FORMATS
    return endpoint, report, _upload_details(payloads, requests, head, get)


def _source_ip(stdout):
    if not isinstance(stdout, str) or len(stdout) > 160:
        return None
    match = _REPORT.fullmatch(stdout)
    if not match or not match[1] or '%' in match[1]:
        return None
    try:
        address = ipaddress.ip_address(match[1])
        return None if address.is_unspecified or address.is_multicast else str(address)
    except ValueError:
        return None


def network_details(item):
    """Return up to eight requested curl origins; local socket IP needs exact evidence.

    Addresses are not public egress identities. Proxies/redirects may connect to a
    different peer than the requested origin. Source attribution is unavailable
    for compound commands or multiple transfers. Payload labels describe supplied
    sources, not delivery. Only bounded literal file paths are retained; no files
    are read and no inline payload or response content is retained.
    """
    if not isinstance(item, dict):
        return []
    command = item.get('command')
    if (not isinstance(command, list) or not command
            or any(not isinstance(word, str) or '\0' in word for word in command)
            or sum(map(len, command)) > MAX_TEXT):
        return []
    if not command_names(command)[1]:
        return []
    try:
        commands = _commands(command)
    except (ValueError, IndexError, RecursionError):
        return []
    entries = []
    for words, safe_stdout in commands:
        if not words or PurePosixPath(words[0]).name != 'curl':
            continue
        parsed = _curl(words)
        if parsed is None:
            continue
        destination, report, upload = parsed
        source = _source_ip(item.get('stdout')) if len(commands) == 1 and safe_stdout and report else None
        entries.append({'command': 'curl', 'destination': destination, 'source_ip': source,
                        'source_ip_evidence': 'curl report' if source else 'not recorded',
                        'destination_kind': 'requested endpoint', **upload})
        if len(entries) == MAX_ENTRIES:
            break
    return entries
