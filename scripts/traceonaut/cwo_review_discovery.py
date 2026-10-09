"""Discover external model invocations from recorded CWO session executions.

Commands are parsed as data, never executed. Only owned, regular redirected
output files are read. Numeric projections survive deletion of temporary output.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
from urllib.parse import unquote, urlsplit

from .codex_session_telemetry import _open_source, _timestamp, _uuid
from .cwo_review_provenance import digest, summaries
from .cwo_review_telemetry import _json, _integer, _label, project, EXPORT_CAP
from .cwo_audit_telemetry import _parse

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_LAUNCHES = 4096
MAX_SCAN_OUTPUT_BYTES = 32 * 1024 * 1024
GAP_REASONS = ('missing_result','conflicting_result','invalid_record','oversized_output',
               'changing_output','unreadable_output','reused_output','unsupported_launch')


def model_check(prompt):
    return isinstance(prompt,str) and re.fullmatch(
        r'(?is)\s*(?:(?:reply|respond)(?:\s+with)?\s+(?:(?:exactly|only)\s+)?)?(?:READY|STDIN_OK)(?:\s+only)?[.!]?\s*', prompt) is not None


def heredoc(text):
    if not isinstance(text,str):
        return None
    return re.fullmatch(r"([^\n]*?)<<\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\n(.*?)\n\2\s*",text,re.S)


def shell_parts(command):
    """Literal words and operators, retaining descriptor adjacency and quotes."""
    if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
        return []
    if Path(command[0]).name in {'bash', 'sh', 'zsh'}:
        if len(command) != 3 or command[1] not in {'-c', '-lc'}:
            return []
        text = command[2]
        body=heredoc(text)
        if body:
            text=body[1]
        elif '\n' in text:
            return []
        if any(c in text for c in ('`', '$(', '${')):
            return []
        pattern = r'''(?:[^\s;&|<>'"\\]+|\\.|'(?:[^']*)'|"(?:\\.|[^"\\])*")+|[;&|<>]+'''
        parts, end = [], 0
        try:
            for match in re.finditer(pattern, text):
                if text[end:match.start()].strip():
                    return []
                raw = match.group()
                operator = raw[0] in ';&|<>'
                fd = None
                if operator and raw[0] in '<>' and parts and end == match.start():
                    if re.fullmatch(r'[0-9]+', parts[-1][3]):
                        fd = int(parts.pop()[0])
                value = raw if operator else shlex.split(raw, posix=True)[0]
                parts.append((value, operator, fd, raw))
                end = match.end()
            return parts if not text[end:].strip() else []
        except ValueError:
            return []
    return [(word, False, None, word) for word in command]


def shell_tokens(command):
    return [part[0] for part in shell_parts(command)]


def launch_words(command):
    parts = shell_parts(command)
    argv, output, prompt = [], None, None
    index, piped = 0, False
    while index < len(parts):
        word, operator, fd, _ = parts[index]
        if operator and word == '|':
            piped = not (len(argv) == 2 and argv[0] == 'cat' and index+1 < len(parts)
                         and Path(parts[index+1][0]).name in {'claude', 'codex'})
        if operator and word in {'>', '>>', '<', '>&'}:
            if index + 1 >= len(parts) or parts[index+1][1]:
                return [], None, None
            target = parts[index+1][0]
            descriptor = (0 if word == '<' else 1) if fd is None else fd
            if word == '>&':
                # Copying stdout to stderr does not change the stdout source.
                if descriptor != 2 or target != '1':
                    return [], None, None
            elif descriptor == 2 and word in {'>', '>>'}:
                pass
            elif descriptor == 1 and word == '>' and not piped:
                if output is not None or not Path(target).is_absolute():
                    return [], None, None
                output = target
            elif descriptor == 0 and word == '<':
                # Relative stdin does not invalidate a launch or its stdout.
                # It supplies no absolute prompt path for optional audit reads.
                prompt = target if Path(target).is_absolute() else None
            else:
                return [], None, None
            index += 2
            continue
        if operator and any(char in word for char in '<>'):
            return [], None, None
        argv.append(word)
        index += 1
    return argv, output, prompt


def launch_metadata(command, *, script_reader=None, depth=0):
    """Recognize literal launch argv and redirects, including sudo/su wrappers."""
    if depth > 4:
        return None
    argv, output, prompt = launch_words(command)
    if not argv:
        return None
    body=heredoc(command[-1]) if len(command)==3 else None
    # A leading cd changes no launch parameters. Conditional trailing commands
    # do not establish that another invocation executed.
    if len(argv) > 3 and argv[0] == 'cd' and argv[2] == '&&':
        argv = argv[3:]
    if any(word in {';', '&&', '||', '&', '>>'} for word in argv):
        return None
    # A pipe may format stdout, but its redirect is never the model's output.
    if '|' in argv:
        split = argv.index('|')
        if split==2 and argv[0]=='cat' and argv[3:4] and Path(argv[3]).name in {'claude','codex'}:
            argv=argv[3:]
        else:
            if '>' in argv[split:]:
                return None
            argv = argv[:split]
    name = Path(argv[0]).name
    if name in {'sudo', 'su'} and '-c' in argv:
        index = argv.index('-c')
        if index+1 >= len(argv):
            return None
        inner = launch_metadata(['sh', '-c', argv[index+1]], script_reader=script_reader, depth=depth+1)
        if inner and output:
            inner['output'] = output
        if inner and body and model_check(body[3]):
            inner['purpose']='model_check'
        return inner
    if name == 'timeout':
        argv = argv[2:]
    if argv and argv[0] == 'exec':
        argv = argv[1:]
    if not argv:
        return None
    name = Path(argv[0]).name
    if name not in {'claude', 'codex'}:
        if script_reader and Path(argv[0]).is_absolute() and argv[0].endswith('.sh'):
            try:
                script = script_reader(Path(argv[0])).decode('utf-8').replace('\\\n', ' ')
            except (OSError, ValueError, UnicodeError):
                return None
            lines = [line.strip() for line in script.splitlines() if line.strip() and not line.startswith('#')]
            calls = [line for line in lines if line.startswith('exec claude ') or line.startswith('exec codex ')]
            if len(calls) == 1 and all(line.startswith(('set ', 'test ', 'cd ', 'exec ')) for line in lines):
                inner = launch_metadata(['sh','-c',calls[0]], depth=depth+1)
                if inner:
                    inner['output'] = output or inner.get('output')
                    inner['script_metadata'] = True
                return inner
        return None
    if name == 'claude' and len(argv)>1 and argv[1] in {
            'auth','config','doctor','install','update','upgrade','mcp','plugin','plugins','setup-token'}:
        return None
    if any(x in argv for x in ('--help','--version','-h')):
        return None
    if name == 'codex' and 'exec' not in argv:
        return None
    def option(*names):
        for i, word in enumerate(argv):
            if word in names and i+1 < len(argv):
                return argv[i+1]
            for key in names:
                if word.startswith(key+'='):
                    return word[len(key)+1:]
        return 'unknown'
    model, effort = option('--model','-m'), option('--effort')
    if name == 'codex':
        for word in argv:
            if word.startswith('model_reasoning_effort='):
                effort = word.split('=',1)[1].strip('"\'')
    try:
        _label(model); _label(effort)
    except ValueError:
        return None
    inline = option('-p','--print') if name=='claude' else argv[-1]
    if body:
        inline=body[3]
    return {'provider':name,'requested_model':model,'effort':effort,'output':output,'prompt':prompt,
            'purpose':'model_check' if model_check(inline) else 'review'}


def read_owned(path):
    with _open_source(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_OUTPUT_BYTES:
            raise ValueError('oversized_output')
        raw = stream.read(MAX_OUTPUT_BYTES+1)
        after = os.fstat(stream.fileno())
    if len(raw) > MAX_OUTPUT_BYTES or (before.st_size,before.st_ctime_ns) != (after.st_size,after.st_ctime_ns):
        raise ValueError('changing_output')
    return raw


def stream_projection(raw, provider):
    records = []
    for line in raw.splitlines():
        if line.strip():
            records.append(_json(line))
    if provider == 'claude':
        finals = [r for r in records if r.get('type') == 'result']
        if len(finals) != 1:
            raise ValueError('missing_result' if not finals else 'conflicting_result')
        result = finals[0]
        inits = [r for r in records if r.get('type') == 'system' and r.get('subtype') == 'init']
        if inits and any(r.get('session_id') != result.get('session_id') for r in inits):
            raise ValueError('conflicting_result')
        return result
    starts = [r for r in records if r.get('type') == 'thread.started']
    finals = [r for r in records if r.get('type') in {'turn.completed','turn.failed'}]
    if len(starts) != 1 or not _uuid(starts[0].get('thread_id')) or len(finals) != 1:
        raise ValueError('missing_result' if not finals else 'conflicting_result')
    end = finals[0]
    usage = end.get('usage') or {}
    if not isinstance(usage,dict):
        raise ValueError('invalid_record')
    projected = {}
    for src,dst in (('input_tokens','input_tokens'),('output_tokens','output_tokens'),('cached_input_tokens','cache_read_input_tokens')):
        if src in usage:
            if not _integer(usage[src]):
                raise ValueError('invalid_record')
            projected[dst] = usage[src]
    if 'input_tokens' in projected and 'cache_read_input_tokens' in projected:
        if projected['input_tokens'] < projected['cache_read_input_tokens']:
            raise ValueError('invalid_record')
        projected['input_tokens'] -= projected['cache_read_input_tokens']
        projected['cache_creation_input_tokens'] = 0
    if 'reasoning_output_tokens' in usage:
        projected['output_tokens_details'] = {'thinking_tokens':usage['reasoning_output_tokens']}
    return {'type':'result','session_id':starts[0]['thread_id'],
            'is_error':end['type']=='turn.failed','usage':projected,'modelUsage':{}}


def project_execution(metadata, result, sid, identity, start, end):
    # This is an observed launch, not a fabricated CWO dispatch. The internal
    # binding permits reuse of the strict numeric projector without adding IDs
    # or a review-acceptance claim to the public metric labels.
    launch = {'started_at':datetime.fromtimestamp(start,timezone.utc).isoformat(),
              'requested_model':metadata['requested_model'],'effort':metadata['effort'],
              'dispatch_id':identity,'packet_sha256':digest(['observed-command',sid,identity])}
    row = project(launch,result,{(identity,launch['packet_sha256']):start},end,
                  attempt_identity=metadata['provider']=='codex')
    if metadata['provider']=='codex':
        row['review_id']=digest(['codex-invocation',result.get('session_id'),identity])
    row.update(observed_source_session=sid, observed_execution=identity,
               finished_timestamp=end, timestamp_basis='command_start', provider=metadata['provider'],
               provider_session_id=result.get('session_id'), provider_result_id=result.get('uuid'), record_state='complete')
    return row


class LaunchDiscovery:
    """Additional derived tables under the existing private single-writer lock."""
    def __init__(self, db):
        self.db = db
        self.remaining = MAX_SCAN_OUTPUT_BYTES
        self.limited = False
        self.audit_errors = 0
        self.audit_seen = set()
        self.audit_skipped = {}
        db.executescript('''
          CREATE TABLE IF NOT EXISTS review_launches(
            id TEXT PRIMARY KEY,path TEXT,sid TEXT,start REAL,end REAL,metadata TEXT,
            projection TEXT,state TEXT,source_hash TEXT);
          CREATE TABLE IF NOT EXISTS review_discovery_errors(path TEXT,sid TEXT,identity TEXT,reason TEXT,at REAL,
            PRIMARY KEY(path,identity));
          CREATE TABLE IF NOT EXISTS review_audit_events(id TEXT PRIMARY KEY,kind TEXT,at REAL,path TEXT);
        ''')

    def read(self,path):
        if self.remaining <= 0:
            self.limited = True
            raise ValueError('changing_output')
        raw=read_owned(path)
        self.remaining-=len(raw)
        if self.remaining<0:
            self.limited=True
            raise ValueError('changing_output')
        return raw

    def projection(self,metadata,raw,sid,identity,start,end):
        if metadata['provider']=='wrapper':
            return self.wrapper_projection(metadata,sid,identity,start,end)
        result=stream_projection(raw,metadata['provider'])
        return project_execution(metadata,result,sid,identity,start,end)

    def wrapper_metadata(self, argv, start, cwd=None):
        if not argv or re.fullmatch(r'python(?:3(?:\.\d+)?)?',Path(argv[0]).name) is None:
            return None
        helper=next((i for i,x in enumerate(argv) if str(x).endswith('/complex-work-orchestration/scripts/run_checked_command.py')),None)
        if helper is None or any(arg not in {'-B','-u'} for arg in argv[1:helper]):
            return None
        spec_path=Path(argv[argv.index('--spec')+1] if '--spec' in argv else argv[helper+1])
        if not spec_path.is_absolute():
            if not isinstance(cwd,str):return None
            if cwd.startswith('file:'):
                parsed=urlsplit(cwd)
                if parsed.netloc or parsed.query or parsed.fragment:return None
                cwd=unquote(parsed.path)
            if not Path(cwd).is_absolute():return None
            spec_path=Path(os.path.abspath(Path(cwd)/spec_path))
        try:
            spec_raw=self.read(spec_path)
        except FileNotFoundError:
            if 'review' in spec_path.name:
                raise ValueError('unsupported_launch') from None
            return None
        spec=_json(spec_raw)
        args=spec.get('argv',[])
        scripts=[Path(x) for x in args[1:] if isinstance(x,str) and x.endswith('.py') and Path(x).is_absolute()]
        if spec.get('mode')!='argv' or len(scripts)!=1 or scripts[0].parent!=spec_path.parent:
            return None
        script=scripts[0]
        if script.name not in {'run_review.py','runner.py'}:
            return None
        # Retained launch artifacts provide association evidence, not a claim
        # that current source code was cryptographically attested at launch.
        if max(spec_path.stat().st_mtime,script.stat().st_mtime)>start:
            raise ValueError('unsupported_launch')
        script_raw=self.read(script)
        tree=ast.parse(script_raw)
        parents={node.targets[0].id for node in tree.body if isinstance(node,ast.Assign)
                 and len(node.targets)==1 and isinstance(node.targets[0],ast.Name)
                 and ast.unparse(node.value)=='Path(__file__).parent'}
        paths={'read_text':set(),'write_text':set()}
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Attribute) or node.func.attr not in paths:
                continue
            value=node.func.value
            if (isinstance(value,ast.BinOp) and isinstance(value.op,ast.Div) and isinstance(value.left,ast.Name)
                    and value.left.id in parents and isinstance(value.right,ast.Constant) and isinstance(value.right.value,str)):
                name=value.right.value
                if Path(name).name==name:
                    paths[node.func.attr].add(name)
        pairs=[name for name in paths['write_text'] if name.endswith('-provenance.json')
               and name.removesuffix('-provenance.json')+'-prompt.txt' in paths['read_text']]
        if len(pairs)!=1:
            raise ValueError('unsupported_launch')
        artifact=script.parent/pairs[0]
        prompt=artifact.with_name(artifact.name.removesuffix('-provenance.json')+'-prompt.txt')
        return {'provider':'wrapper','requested_model':'unknown','effort':'unknown','purpose':'review',
                'output':str(artifact),'prompt':str(prompt),'script_sha256':hashlib.sha256(script_raw).hexdigest(),
                'spec_sha256':hashlib.sha256(spec_raw).hexdigest(),'association_evidence':'retained_launch_artifacts'}

    def wrapper_projection(self,metadata,sid,identity,start,end):
        from .cwo_review_telemetry import ReviewCollector
        artifact=Path(metadata['output'])
        result=ReviewCollector(directories=[]).scan(now=end,files=[artifact],reader=self.read)
        if result['limit_reached']:
            self.limited=True
        if len(result['reviews'])!=1:
            raise ValueError('invalid_record' if result['skipped_records'] else 'missing_result')
        row=result['reviews'][0]
        if not start<=row['timestamp']<=row.get('finished_timestamp',end)<=end:
            raise ValueError('invalid_record')
        row.update(observed_source_session=sid,observed_execution=identity,provider='claude',
                   timestamp_basis='producer_start',wrapper_started_timestamp=start,wrapper_finished_timestamp=end,
                   association_evidence=metadata['association_evidence'],
                   script_sha256=metadata['script_sha256'],spec_sha256=metadata['spec_sha256'])
        return row

    def evaluation(self,metadata,now):
        prompt=metadata.get('prompt')
        if not prompt:
            return {}
        try:
            text=self.read(Path(prompt)).decode('utf-8')
            dispatch=re.findall(r'^Dispatch ID: ([^\r\n]+)$',text,re.M)
            packet=re.findall(r'^Packet SHA-256: ([0-9a-f]{64})$',text,re.M)
            if len(dispatch)!=1 or len(packet)!=1:
                return {}
            found=[]
            for name in ('audit.jsonl','contract-audit.jsonl'):
                path=Path(prompt).parent/name
                if not path.exists():continue
                raw=self.read(path)
                events,skipped=_parse(raw)
                self.audit_seen.add(str(path))
                for reason,count in skipped.items():
                    self.audit_skipped[reason]=self.audit_skipped.get(reason,0)+count
                for event_id,kind,at in events:
                    if now-30*86400<=at<=now:
                        self.db.execute('INSERT OR IGNORE INTO review_audit_events VALUES(?,?,?,?)',(event_id,kind,at,str(path)))
                valid={e[0]:e[2] for e in events if e[1]=='return_evaluated' and e[2]<=now}
                for line in raw.splitlines():
                    event=_json(line)
                    if (event.get('event_hash') in valid and event.get('dispatch_id')==dispatch[0]
                            and event.get('packet_sha256')==packet[0] and event.get('verdict') in {'accept','reject'}):
                        details={k:event[k] for k in ('verdict','recommended_disposition','hold_classification','peer_review_status')
                                 if isinstance(event.get(k),str) and len(event[k])<=128}
                        found.append((valid[event['event_hash']],details))
            return max(found,key=lambda item:item[0])[1] if found else {}
        except FileNotFoundError:
            return {}
        except (OSError,ValueError,UnicodeError):
            self.audit_errors+=1
            return {}

    def merge_audit(self, audit, now, ready):
        from .cwo_audit_telemetry import EXPORT_CAP
        audit=dict(audit) if audit is not None else {'source_available':0,'collection_complete':1,
            'scan_timestamp_seconds':now,'source_files':0,'source_errors':0,'limit_reached':0,
            'events':[],'skipped_records':{},'source_paths':[]}
        with self.db:
            self.db.execute('DELETE FROM review_audit_events WHERE at<?',(now-30*86400,))
            count=self.db.execute('SELECT count(*) FROM review_audit_events').fetchone()[0]
            self.db.execute('DELETE FROM review_audit_events WHERE id IN (SELECT id FROM review_audit_events ORDER BY at DESC LIMIT -1 OFFSET ?)',(EXPORT_CAP,))
        events={r[0]:tuple(r) for r in audit['events']}
        retained=list(self.db.execute('SELECT id,kind,at FROM review_audit_events'))
        events.update({r[0]:tuple(r) for r in retained})
        audit['limit_reached']=int(audit['limit_reached'] or count>EXPORT_CAP or len(events)>EXPORT_CAP or self.limited)
        audit['events']=sorted(events.values(),key=lambda row:(row[2],row[0]),reverse=True)[:EXPORT_CAP]
        audit['source_available']=int(audit['source_available'] or bool(retained))
        audit['source_files']=len(set(audit.get('source_paths',[]))|self.audit_seen)
        audit['source_errors']+=self.audit_errors
        for reason,count in self.audit_skipped.items():
            audit['skipped_records'][reason]=audit['skipped_records'].get(reason,0)+count
        audit['collection_complete']=int(audit['collection_complete'] and ready and not audit['limit_reached']
            and not audit['source_errors'] and not any(audit['skipped_records'].values()))
        return audit

    def consume(self, record, path, sid):
        payload = record.get('payload',{})
        if not isinstance(payload,dict):return
        item = payload.get('item',{})
        if not isinstance(item,dict):return
        if (record.get('type') != 'event_msg' or payload.get('type') != 'item_completed'
                or payload.get('thread_id') != sid or item.get('type') != 'CommandExecution'
                or item.get('status') not in {'completed','failed'}):
            return
        start,end = payload.get('started_at_ms'),payload.get('completed_at_ms')
        if any(type(x) not in (int,float) or not 0 < x < 2**53 for x in (start,end)) or start > end:
            return
        if (any(not isinstance(v,str) or not 0<len(v)<=512 for v in (payload.get('turn_id'),item.get('id')))
                or abs((_timestamp(record.get('timestamp')) or 0)-end/1000)>5):
            if launch_metadata(item.get('command')):
                self.db.execute('INSERT OR REPLACE INTO review_discovery_errors VALUES(?,?,?,?,?)',
                                (path,sid,digest([payload.get('turn_id'),item.get('id')]),'invalid_record',end/1000))
            return
        identity = digest([sid,payload.get('turn_id'),item.get('id')])
        argv=shell_tokens(item.get('command'))
        self.db.execute('DELETE FROM review_discovery_errors WHERE path=? AND identity=?',(path,identity))
        if any(arg in argv for arg in ('--help','--version','-h')):
            self.db.execute('DELETE FROM review_discovery_errors WHERE path=? AND identity=?',(path,identity))
            return
        try:
            metadata = launch_metadata(item.get('command'),script_reader=self.read) or self.wrapper_metadata(argv,start/1000,item.get('cwd'))
        except (OSError,ValueError,IndexError,TypeError,SyntaxError):
            self.db.execute('INSERT OR REPLACE INTO review_discovery_errors VALUES(?,?,?,?,?)',
                            (path,sid,identity,'unsupported_launch',end/1000))
            return
        if not metadata:
            # Literal model invocations with unsupported shell composition must
            # remain visible as gaps; command mentions and control commands do not.
            if argv and (Path(argv[0]).name=='claude' and ('-p' in argv or '--print' in argv)
                         or Path(argv[0]).name=='codex' and 'exec' in argv):
                self.db.execute('INSERT OR REPLACE INTO review_discovery_errors VALUES(?,?,?,?,?)',
                                (path,sid,identity,'unsupported_launch',end/1000))
            return
        old = self.db.execute('SELECT * FROM review_launches WHERE id=?',(identity,)).fetchone()
        start,end = start/1000,end/1000
        metadata['process_status']=item['status']
        metadata['exit_code']=item.get('exit_code')
        if old and old['projection'] and all(json.loads(old['metadata']).get(key) == metadata.get(key)
                for key in ('provider', 'output', 'prompt', 'requested_model', 'effort')):
            return
        projection, state, fingerprint = None,'missing_result',None
        try:
            if metadata.get('output'):
                raw = self.read(Path(metadata['output']))
                if metadata['provider']=='codex':
                    starts=[_json(line) for line in raw.splitlines() if line.strip()]
                    ids={r.get('thread_id') for r in starts if r.get('type')=='thread.started' and _uuid(r.get('thread_id'))}
                    if len(ids)==1:metadata['provider_session_id']=ids.pop()
                if metadata['provider']=='wrapper':
                    projection=self.wrapper_projection(metadata,sid,identity,start,end)
                    state=projection.get('record_state','complete')
                    raise StopIteration
                result = stream_projection(raw,metadata['provider'])
                fingerprint = hashlib.sha256(raw).hexdigest()
            else:
                if metadata['provider']=='codex':
                    result=stream_projection(item.get('stdout','').encode(), 'codex')
                else:
                    finals = summaries(item.get('stdout'))
                    if len(finals) != 1:
                        raise ValueError('missing_result')
                    result = finals[0]
            projection = project_execution(metadata,result,sid,identity,start,end)
            state = 'complete'
        except StopIteration:
            pass
        except (OSError,ValueError,UnicodeError,TypeError,RecursionError) as error:
            state = str(error) if str(error) in {'missing_result','conflicting_result','invalid_record','oversized_output','changing_output'} else 'unreadable_output'
        self.db.execute('INSERT OR REPLACE INTO review_launches VALUES(?,?,?,?,?,?,?,?,?)',
            (identity,path,sid,start,end,json.dumps(metadata),json.dumps(projection) if projection else None,state,fingerprint))

    def merge(self, reviews, snapshot, now):
        associated = {r['session_id'] for r in snapshot.get('cwo_sessions',{}).get('associations',[])}
        sessions = {r['session_id']:r for r in snapshot['sessions']}
        self.db.execute('DELETE FROM review_launches WHERE end<?',(now-30*86400,))
        self.db.execute('DELETE FROM review_discovery_errors WHERE at<?',(now-30*86400,))
        self.limited=self.limited or self.db.execute('SELECT count(*) FROM review_launches').fetchone()[0]>MAX_LAUNCHES
        self.db.execute('DELETE FROM review_launches WHERE id IN (SELECT id FROM review_launches ORDER BY end DESC LIMIT -1 OFFSET ?)',(MAX_LAUNCHES,))
        self.limited=self.limited or self.db.execute('SELECT count(*) FROM review_discovery_errors').fetchone()[0]>MAX_LAUNCHES
        self.db.execute('DELETE FROM review_discovery_errors WHERE rowid IN (SELECT rowid FROM review_discovery_errors ORDER BY at DESC LIMIT -1 OFFSET ?)',(MAX_LAUNCHES,))
        launches = [dict(r) for r in self.db.execute('SELECT * FROM review_launches WHERE end<=? ORDER BY start,id',(now,)) if r['sid'] in associated]
        self.limited = self.limited or len(launches)>MAX_LAUNCHES
        launches=launches[-MAX_LAUNCHES:]
        # Retry late final records even when the source rollout has not grown.
        for launch in launches:
            metadata=json.loads(launch['metadata'])
            if (launch['projection'] and launch['state']=='complete') or not metadata.get('output'):
                continue
            try:
                raw=self.read(Path(metadata['output']))
                row=self.projection(metadata,raw,launch['sid'],launch['id'],launch['start'],launch['end'])
                launch.update(projection=json.dumps(row),state=row.get('record_state','complete'),source_hash=hashlib.sha256(raw).hexdigest())
                self.db.execute('UPDATE review_launches SET projection=?,state=?,source_hash=? WHERE id=?',
                                (launch['projection'],launch['state'],launch['source_hash'],launch['id']))
            except (OSError,ValueError,UnicodeError,TypeError,RecursionError):
                pass
        rows = {r['review_id']:r for r in reviews['reviews']}
        eligible = set(reviews.get('eligible_review_ids', rows))
        withdrawn = set(reviews.get('withdrawn_review_ids', ()))
        outputs = {}
        for launch in launches:
            metadata = json.loads(launch['metadata'])
            if metadata.get('output'):
                outputs.setdefault(metadata['output'],[]).append(launch)
        gaps, poisoned, claimants = {}, set(), {}
        for error in self.db.execute('SELECT sid,reason FROM review_discovery_errors'):
            if error['sid'] in associated:
                gaps[error['reason']]=gaps.get(error['reason'],0)+1
        unprojected_launches = bool(gaps)
        for launch in launches:
            metadata = json.loads(launch['metadata'])
            if metadata.get('purpose')=='model_check':
                continue
            if not launch['projection']:
                gaps[launch['state']] = gaps.get(launch['state'],0)+1
                row={'review_id':digest(['incomplete-invocation',launch['id']]),'timestamp':launch['start'],
                     'outcome':'failed' if metadata.get('process_status')=='failed' else 'unknown',
                     'requested_model':metadata['requested_model'],'reported_model':'unknown','effort':metadata['effort'],
                     'tokens':{},'duration':None,'observed_source_session':launch['sid'],'record_state':launch['state'],
                     'observed_execution':launch['id'],'finished_timestamp':launch['end'],'provider':metadata['provider'],
                     'provider_session_id':metadata.get('provider_session_id')}
            else:
                row = json.loads(launch['projection'])
                withdrawn.add(digest(['incomplete-invocation',launch['id']]))
                if row.get('record_state','complete')!='complete':
                    gaps[row['record_state']]=gaps.get(row['record_state'],0)+1
            row['execution_status']=metadata.get('process_status','unknown')
            row['execution_exit_code']=metadata.get('exit_code')
            # Reused output names cannot backfill more than the latest launch.
            siblings = outputs.get(metadata.get('output'),[])
            if len(siblings)>1 and launch['id']!=siblings[-1]['id'] and not launch['projection']:
                # A reused filename proves no provider identity for an earlier
                # failed execution. Its own command ID and times remain exact.
                row.pop('provider_session_id',None)
                row['output_reused']=True
            if launch['projection'] and len(siblings)>1 and launch['id'] != siblings[-1]['id'] and any(
                    other['source_hash']==launch['source_hash'] for other in siblings if other['id']!=launch['id']):
                gaps['reused_output'] = gaps.get('reused_output',0)+1
                row={'review_id':digest(['incomplete-invocation',launch['id']]),'timestamp':launch['start'],
                     'outcome':'unknown','requested_model':metadata['requested_model'],'reported_model':'unknown',
                     'effort':metadata['effort'],'tokens':{},'duration':None,'observed_source_session':launch['sid'],
                     'record_state':'reused_output'}
            prior = rows.get(row['review_id'])
            if row['review_id'] in poisoned:
                continue
            if prior:
                if any(prior.get(k) != row.get(k) for k in ('tokens','outcome','reported_model','requested_model','effort')):
                    rows.pop(row['review_id'],None)
                    poisoned.add(row['review_id'])
                    gaps['conflicting_result'] = gaps.get('conflicting_result',0)+1
                    continue
                # Existing audited identities/timing stay stable; add observed
                # launch linkage only when the actual provider result matches.
                row = {**row,**prior,'observed_source_session':launch['sid']}
            row['source_session']={'session_id':launch['sid'],'project_id':sessions[launch['sid']]['project_id']}
            row['attribution']='linked'
            details=self.evaluation(metadata,now)
            if details:
                row['audit_evaluation']=details
                row['evaluation']=details['verdict']
                if details['verdict']=='accept' and (details.get('peer_review_status')=='pending' or details.get('hold_classification')=='peer-review-pending'):
                    row['evaluation']='accept_pending_peer'
                if launch['projection']:
                    saved=json.loads(launch['projection'])
                    saved.update(audit_evaluation=details,evaluation=row['evaluation'])
                    self.db.execute('UPDATE review_launches SET projection=? WHERE id=?',(json.dumps(saved),launch['id']))
            claimants.setdefault(row['review_id'],set()).add(launch['sid'])
            rows[row['review_id']]=row
        for key,sids in claimants.items():
            if len(sids)>1 and key in rows:
                rows[key]['attribution']='ambiguous'
                rows[key].pop('source_session',None)
        eligible.update(rows)
        eligible.difference_update(poisoned)
        withdrawn.update(poisoned)
        reviews['eligible_review_ids'] = sorted(eligible)
        reviews['withdrawn_review_ids'] = sorted(withdrawn - rows.keys())
        # Missing results still have known launch identities. They reduce usage
        # coverage, but do not make the enumerated membership incomplete.
        reviews['membership_complete'] = (reviews.get('membership_complete', True)
                                         and not self.limited and not unprojected_launches)
        self.limited=self.limited or len(rows)>EXPORT_CAP
        reviews['reviews']=sorted(rows.values(),key=lambda r:(r['timestamp'],r['review_id']),reverse=True)[:EXPORT_CAP]
        reviews['pending_results'] = sum(row.get('record_state') == 'missing_result' for row in rows.values())
        reviews['discovery']={'launches':sum(json.loads(r['metadata']).get('purpose')!='model_check' for r in launches),
                              'model_checks':sum(json.loads(r['metadata']).get('purpose')=='model_check' for r in launches),
                              'represented':sum(bool(r.get('observed_source_session')) for r in reviews['reviews']),
                              'gaps':gaps,'limit_reached':int(self.limited)}
        reviews['limit_reached'] = int(reviews['limit_reached'] or self.limited)
        if launches:
            reviews['source_available']=1
        reviews['collection_complete'] = int(reviews['source_available'] and not gaps
            and not reviews['source_errors'] and not reviews['pending_results']
            and not reviews['limit_reached'] and not any(count for reason,count in
                reviews['skipped_records'].items() if reason != 'preparation_record'))
        return reviews
