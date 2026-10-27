"""Network presentation retains origins and explicitly recorded local socket IPs."""
import json
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from traceonaut.command_network import HTTP_METHODS, MAX_ENTRIES, WRITE_OUT, network_details


class CommandNetworkTests(unittest.TestCase):
    def curl(self, *args, **fields):
        return network_details({'command': ['curl', *args], **fields})

    def reported(self, address='192.0.2.10', **fields):
        return {'command': ['curl', '-sS', '--output', '/dev/null', '--write-out', WRITE_OUT,
                            'https://example.test/private?token=SECRET'],
                'stdout': 'TRACEONAUT_CONNECTION {"local_ip":"' + address + '"}\n', **fields}

    def test_origins_exclude_credentials_paths_queries_and_fragments(self):
        rows = self.curl('https://user:SECRET@Example.TEST:8443/private/SECRET?token=SECRET#SECRET')
        self.assertEqual(rows, [{'command': 'curl', 'destination': 'https://example.test:8443',
                                'source_ip': None, 'source_ip_evidence': 'not recorded',
                                'destination_kind': 'requested endpoint',
                                'http_method': 'GET', 'payload_kind': 'None',
                                'payload_files': '', 'upload_options': ''}])
        self.assertNotIn('SECRET', json.dumps(rows))
        self.assertEqual(self.curl('--url=https://example.test/x')[0]['destination'], 'https://example.test')
        self.assertEqual(self.curl('--url', 'http://example.test/')[0]['destination'], 'http://example.test')

    def test_options_are_consumed_without_misidentifying_values(self):
        rows = self.curl('-sS', '-H', 'https://HEADER_SECRET.test', '-uUSER_SECRET',
                         '--data-binary=https://BODY_SECRET.test', '--proxy', 'http://PROXY_SECRET.test',
                         '-XPOST', '--connect-timeout', '5', '-m10', '-o/dev/null',
                         '-wstatus=%{http_code} time=%{time_total}', 'https://example.test/')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['destination'], 'https://example.test')
        self.assertIsNone(rows[0]['source_ip'])
        self.assertNotIn('SECRET', json.dumps(rows))
        self.assertEqual(self.curl('-H', 'https://header.test'), [])
        self.assertEqual(self.curl('--unknown', 'https://secret.test', 'https://example.test'), [])
        self.assertEqual(self.curl('--output'), [])

    def test_shell_wrappers_compounds_and_heredoc_content(self):
        command = ['bash', '-lc', 'env TOKEN=SECRET sudo -n curl -I https://one.test/x; '
                   'false && command curl https://two.test/y | cat']
        rows = network_details({'command': command})
        self.assertEqual([r['destination'] for r in rows], ['https://one.test', 'https://two.test'])
        self.assertTrue(all(r['source_ip'] is None for r in rows))
        command = ['sh', '-c', "cat <<'EOF'\ncurl https://SECRET.test\nEOF\ncurl https://real.test"]
        self.assertEqual([r['destination'] for r in network_details({'command': command})], ['https://real.test'])
        self.assertEqual(network_details({'command': ['env', '--unknown', 'curl', 'https://x.test']}), [])
        self.assertEqual(network_details({'command': ['python3', '-c', 'curl https://SECRET.test']}), [])

    def test_unsupported_or_dynamic_transfer_grammar_is_omitted(self):
        for args in [('-K', 'config', 'https://example.test'), ('--config=config', 'https://example.test'),
                     ('--next', 'https://example.test'), ('-Z', 'https://example.test'),
                     ('--parallel', 'https://example.test'), ('https://one.test', 'https://two.test'),
                     ('https://example.test/{a,b}',), ('https://example.test/[1-3]',),
                     ('https://example.test/$TOKEN',), ('https://example.test/`secret`',)]:
            with self.subTest(args=args):
                self.assertEqual(self.curl(*args), [])
        for text in ('curl "$URL"', 'for x in a; do curl https://example.test; done',
                     'curl https://example.test/$(secret)', 'curl "unterminated'):
            self.assertEqual(network_details({'command': ['bash', '-c', text]}), [])

    def test_url_validation_and_ipv6(self):
        self.assertEqual(self.curl('https://u:SECRET@[2001:db8::1]:443/path')[0]['destination'],
                         'https://[2001:db8::1]:443')
        self.assertEqual(self.curl('http://192.0.2.1:8080')[0]['destination'], 'http://192.0.2.1:8080')
        for url in ('file:///etc/passwd', 'ftp://example.test', 'https://', 'https://example.test:',
                    'https://example.test:0', 'https://example.test:65536', 'https://example.test:bad',
                    'https://[bad]/', 'https://[fe80::1%25eth0]/', 'https://999.999.999.999/',
                    'https://bad_host.test/', 'https://example.test/\nSECRET', 'https://example.test/\x85SECRET',
                    ' https://example.test/',
                    'https://example.test/\\secret'):
            with self.subTest(url=url):
                self.assertEqual(self.curl(url), [])

    def test_exact_report_provides_only_valid_local_socket_address(self):
        rows = network_details(self.reported())
        self.assertEqual(rows[0]['source_ip'], '192.0.2.10')
        self.assertEqual(rows[0]['source_ip_evidence'], 'curl report')
        self.assertEqual(network_details(self.reported('2001:0db8::1'))[0]['source_ip'], '2001:db8::1')
        # Existing session consumers use stdout; aggregated output is not a substitute.
        item = self.reported()
        item['aggregated_output'] = item.pop('stdout')
        self.assertIsNone(network_details(item)[0]['source_ip'])
        item = self.reported()
        item['command'] = ['bash', '-lc', shlex.join(item['command'])]
        self.assertEqual(network_details(item)[0]['source_ip'], '192.0.2.10')
        item['command'][2] += ' 2>/dev/null'
        self.assertIsNone(network_details(item)[0]['source_ip'])
        item['command'][2] = shlex.join(self.reported()['command']) + ' &'
        self.assertIsNone(network_details(item)[0]['source_ip'])

    def test_spoofed_missing_ambiguous_and_invalid_reports_stay_unknown(self):
        for address in ('', 'bad', '0.0.0.0', '::', '224.0.0.1', 'fe80::1%eth0'):
            self.assertIsNone(network_details(self.reported(address))[0]['source_ip'])
        valid = self.reported()['stdout']
        for stdout in ('body\n' + valid, valid + 'body', valid + valid,
                       'TRACEONAUT_CONNECTION {"local_ip":"192.0.2.10","secret":"SECRET"}\n',
                       'HTTP 200 0.05\n', valid.rstrip('\n')):
            self.assertIsNone(network_details(self.reported(stdout=stdout))[0]['source_ip'])
        for command in (['curl', 'https://example.test'],
                        ['curl', '-w', WRITE_OUT, 'https://example.test'],
                        ['curl', '-o', 'output.txt', '-w', WRITE_OUT, 'https://example.test'],
                        ['curl', '-o', '/dev/null', '-w', 'status=%{http_code}', 'https://example.test'],
                        ['curl', '-o', '/dev/null', '-w', WRITE_OUT, '-w', WRITE_OUT, 'https://example.test']):
            self.assertIsNone(network_details(self.reported(command=command))[0]['source_ip'])
        item = self.reported()
        item['command'] = ['sh', '-c', shlex.join(item['command']) + '; printf ignored']
        self.assertIsNone(network_details(item)[0]['source_ip'])

    def test_proxy_redirect_and_requested_interface_are_not_source_evidence(self):
        args = ['--proxy', 'http://user:SECRET@proxy.test:8080', '-L', '--interface', '192.0.2.20',
                '--resolve', 'example.test:443:192.0.2.30', '--connect-to', 'example.test::other.test:',
                'https://example.test/private']
        row = self.curl(*args)[0]
        self.assertEqual(row['destination'], 'https://example.test')
        self.assertIsNone(row['source_ip'])
        item = self.reported()
        item['command'] = ['curl', *args[:-1], '-o', '/dev/null', '-w', WRITE_OUT, args[-1]]
        row = network_details(item)[0]
        self.assertEqual((row['destination'], row['source_ip']), ('https://example.test', '192.0.2.10'))

    def test_entry_and_input_bounds(self):
        text = '; '.join('curl https://host' + str(i) + '.test' for i in range(MAX_ENTRIES + 2))
        self.assertEqual(len(network_details({'command': ['bash', '-c', text]})), MAX_ENTRIES)
        for item in (None, {}, {'command': []}, {'command': ['curl', None]},
                     {'command': ['curl', 'https://example.test/\0']},
                     {'command': ['curl', 'x' * (128 * 1024 + 1)]}):
            self.assertEqual(network_details(item), [])

    def test_data_uploads_keep_file_sources_not_contents(self):
        cases = [(['-d@/example/report.zip'], 'File', '/example/report.zip', '--data'),
                 (['--data-binary', '@report.zip'], 'File', 'report.zip', '--data-binary'),
                 (['--json=@report.json'], 'File', 'report.json', '--json'),
                 (['--data-ascii', '@report.txt'], 'File', 'report.txt', '--data'),
                 (['--data-urlencode', 'SECRET_FIELD@report.txt'], 'File', 'report.txt', '--data-urlencode'),
                 (['--data-urlencode', '@report.txt'], 'File', 'report.txt', '--data-urlencode'),
                 (['--data-urlencode', 'SECRET_FIELD=SECRET_INLINE'], 'Inline', '', '--data-urlencode'),
                 (['--data-urlencode', 'SECRET_FIELD=@SECRET_INLINE'], 'Inline', '', '--data-urlencode'),
                 (['--data', 'SECRET_INLINE'], 'Inline', '', '--data'),
                 (['--data', ''], 'Inline', '', '--data'),
                 (['--data-raw', '@SECRET_INLINE'], 'Inline', '', '--data-raw')]
        for args, kind, files, options in cases:
            with self.subTest(args=args):
                row = self.curl(*args, 'https://example.test')[0]
                self.assertEqual((row['http_method'], row['payload_kind'], row['payload_files'],
                                  row['upload_options']), ('POST', kind, files, options))
                self.assertNotIn('SECRET', json.dumps(row))

    def test_multipart_options_and_field_names_are_not_retained(self):
        cases = [(['-F', 'SECRET_FIELD=@report.zip;type=application/zip;filename=SECRET_NAME'],
                  'File', 'report.zip', '--form'),
                 (['--form', 'SECRET_FIELD=<report.txt;headers=@SECRET_HEADERS'],
                  'File', 'report.txt', '--form'),
                 (['--form', 'SECRET_FIELD=@"report file.zip";filename="SECRET_NAME,other"'],
                  'File', 'report file.zip', '--form'),
                 (['--form', 'SECRET_FIELD=@first.zip,second.zip'],
                  'File', 'first.zip; second.zip', '--form'),
                 (['-FSECRET_FIELD=SECRET_INLINE'], 'Inline', '', '--form'),
                 (['--form-string', 'SECRET_FIELD=@SECRET_INLINE;filename=SECRET_NAME'],
                  'Inline', '', '--form-string'),
                 (['--form-string', 'SECRET_FIELD=<SECRET_INLINE'], 'Inline', '', '--form-string')]
        for args, kind, files, options in cases:
            with self.subTest(args=args):
                row = self.curl(*args, 'https://example.test')[0]
                self.assertEqual((row['http_method'], row['payload_kind'], row['payload_files'],
                                  row['upload_options']), ('POST', kind, files, options))
                self.assertNotIn('SECRET', json.dumps(row))

    def test_upload_file_and_stdin_semantics(self):
        for args, method, kind, files in [
                (['-Treport.zip'], 'PUT', 'File', 'report.zip'),
                (['--upload-file', '@-'], 'PUT', 'File', '@-'),
                (['-T-'], 'PUT', 'Stdin', ''),
                (['--upload-file', '.'], 'PUT', 'Stdin', ''),
                (['-d@-'], 'POST', 'Stdin', ''),
                (['--data-binary', '@-'], 'POST', 'Stdin', ''),
                (['--data-urlencode', 'SECRET_FIELD@-'], 'POST', 'Stdin', ''),
                (['--json', '@-'], 'POST', 'Stdin', ''),
                (['-F', 'SECRET_FIELD=@-'], 'POST', 'Stdin', ''),
                (['-F', 'SECRET_FIELD=<-'], 'POST', 'Stdin', '')]:
            with self.subTest(args=args):
                row = self.curl(*args, 'https://example.test')[0]
                self.assertEqual((row['http_method'], row['payload_kind'], row['payload_files']),
                                 (method, kind, files))
                self.assertNotIn('SECRET', json.dumps(row))

    def test_http_methods_and_get_head_combinations(self):
        for method in HTTP_METHODS:
            self.assertEqual(self.curl('-X' + method, 'https://example.test')[0]['http_method'], method)
        for args, method in [([], 'GET'), (['-I'], 'HEAD'), (['--head'], 'HEAD'),
                             (['-G'], 'GET'), (['--get', '-d@report.zip'], 'GET'),
                             (['-IG', '-d@report.zip'], 'HEAD'),
                             (['-XPOST', '-G', '-d@report.zip'], 'POST'),
                             (['-XPATCH', '-Treport.zip'], 'PATCH'),
                             (['-XPOST', '-I'], 'POST'),
                             (['-XSECRET_CUSTOM'], 'Unknown'), (['-Xpost'], 'Unknown'),
                             (['-XPOST', '-XPUT'], 'Unknown')]:
            with self.subTest(args=args):
                row = self.curl(*args, 'https://example.test')[0]
                self.assertEqual(row['http_method'], method)
                self.assertNotIn('SECRET', json.dumps(row))
        row = self.curl('-G', '-d@report.zip', 'https://example.test')[0]
        self.assertEqual((row['payload_kind'], row['payload_files']), ('File', 'report.zip'))

    def test_mixed_sources_and_canonical_bounded_option_names(self):
        row = self.curl('-d@one.zip', '--data-binary', '@two.zip', '--data-raw', 'SECRET_INLINE',
                        '-d@-', '-d@one.zip', 'https://example.test')[0]
        self.assertEqual((row['http_method'], row['payload_kind'], row['payload_files']),
                         ('POST', 'Mixed', 'one.zip; two.zip'))
        self.assertEqual(row['upload_options'], '--data --data-binary --data-raw')
        self.assertNotIn('SECRET', json.dumps(row))
        row = self.curl('-F', 'SECRET_FIELD=@one.zip', '-F', 'SECRET_FIELD=@-',
                        '--form-string', 'SECRET_FIELD=SECRET_INLINE', 'https://example.test')[0]
        self.assertEqual((row['payload_kind'], row['payload_files'], row['upload_options']),
                         ('Mixed', 'one.zip', '--form --form-string'))
        self.assertNotIn('SECRET', json.dumps(row))

    def test_conflicting_and_ambiguous_payload_grammar_is_unknown(self):
        conflicts = [(['-d@file.zip', '-F', 'f=@file.zip']),
                     (['-Tfile.zip', '-d@file.zip']), (['-Tfirst.zip', '-Tsecond.zip']),
                     (['-I', '-d@file.zip']), (['-I', '-Tfile.zip']),
                     (['-G', '-F', 'f=@file.zip']), (['-G', '-Tfile.zip'])]
        for args in conflicts:
            with self.subTest(args=args):
                row = self.curl(*args, 'https://example.test')[0]
                self.assertEqual((row['http_method'], row['payload_kind'], row['payload_files']),
                                 ('Unknown', 'Unknown', ''))
        for args in [(['-d@$SECRET_PATH']), (['--data-binary', '@*.zip']),
                     (['--json', '@~/report.json']), (['-T{first,second}.zip']),
                     (['-Thttps://SECRET.test/file']), (['-Tfile\nSECRET']),
                     (['-F', 'f=@file.zip;unknown=SECRET']), (['-F', 'f=@file.zip;filename=']),
                     (['-F', 'f=@file.zip;filename=SECRET_NAME,SECRET_TRAILING']),
                     (['-F', 'f=@file.zip;headers=SECRET_HEADER,SECRET_TRAILING']),
                     (['-F', 'f=@"unterminated']), (['-F', 'f=<first,SECRET']),
                     (['-F', 'f=(;type=multipart/mixed']), (['-F', 'SECRET']),
                     (['--form-string', 'SECRET']), (['--data-binary', '@']),
                     (['--upload-file', '']), (['--data-urlencode', 'SECRET_FIELD@'])]:
            with self.subTest(args=args):
                row = self.curl(*args, 'https://example.test')[0]
                self.assertEqual((row['payload_kind'], row['payload_files']), ('Unknown', ''))
                self.assertNotIn('SECRET', json.dumps(row))

    def test_payload_paths_are_bounded_without_partial_unknown_projection(self):
        row = self.curl(*['-d@file' + str(i) + '.zip' for i in range(4)], 'https://example.test')[0]
        self.assertEqual(row['payload_kind'], 'File')
        self.assertEqual(len(row['payload_files'].split('; ')), 4)
        for args in [(['-d@file' + str(i) + '.zip' for i in range(5)]),
                     (['-T' + 'a' * 769]), (['-d@' + 'a' * 384, '-d@' + 'b' * 384]),
                     (['-d@safe.zip', '-d@$UNKNOWN'])]:
            row = self.curl(*args, 'https://example.test')[0]
            self.assertEqual((row['payload_kind'], row['payload_files']), ('Unknown', ''))
        self.assertEqual(len(self.curl('-T' + 'a' * 768, 'https://example.test')[0]['payload_files']), 768)

    def test_upload_report_and_shell_parsing_preserve_evidence_and_privacy(self):
        item = self.reported()
        item['command'][1:1] = ['--data-binary', '@/example/report.zip', '-H', 'SECRET_HEADER',
                               '--user', 'SECRET_AUTH', '--cookie', 'SECRET_COOKIE']
        item['command'] = ['bash', '-lc', shlex.join(item['command'])]
        row = network_details(item)[0]
        self.assertEqual((row['http_method'], row['payload_kind'], row['payload_files'],
                          row['upload_options'], row['source_ip']),
                         ('POST', 'File', '/example/report.zip', '--data-binary', '192.0.2.10'))
        self.assertNotIn('SECRET', json.dumps(row))


if __name__ == '__main__':
    unittest.main()
