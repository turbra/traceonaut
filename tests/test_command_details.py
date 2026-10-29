"""Action labels and opt-in display never retain credential/program content."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from traceonaut.command_details import invocation_details, validate_history
from traceonaut.shell_command_names import command_names


class DetailTests(unittest.TestCase):
    def test_actions_skip_git_global_option_values(self):
        cases = [(['git','commit','-m','SECRET'], 'git commit'),
                 (['git','-C','commit','status'], 'git status'),
                 (['git','-Cwork','-chttp.extraHeader=SECRET','push'], 'git push'),
                 (['git','--git-dir=work/.git','clone','https://example.test'], 'git clone'),
                 (['git','--config-env=key=TOKEN','diff'], 'git diff'),
                 (['git','PRIVATE_ALIAS'], 'git other'), (['git','-C'], 'git other'),
                 (['tail','-f','app.log'], 'tail -f'), (['cat','app.log'], 'cat'),
                 (['podman','exec','container','git'], 'podman exec')]
        for words, expected in cases:
            self.assertEqual(command_names(words, actions=True), ([expected], True))

    def test_wrapped_and_compound_actions_preserve_occurrences(self):
        words = ['bash','-lc','git status && git push; env TOKEN=SECRET sudo -n git commit -m SECRET | tail -n 20']
        self.assertEqual(command_names(words, actions=True), (['git status','git push','git commit','tail'], True))
        self.assertEqual(command_names(words), (['git','git','git','tail'], True))

    def test_summaries_omit_credentials_programs_and_messages(self):
        commands = [
            ['git','-C','repo','-c','http.extraHeader=SECRET','commit','-m','SECRET'],
            ['git','clone','https://user:SECRET@example.test/repo?token=SECRET'],
            ['curl','-I','-H','Authorization: SECRET','https://example.test'],
            ['env','TOKEN=SECRET','sudo','-n','git','push'],
            ['python3','-c','SECRET'], ['unrecognized','SECRET'],
            ['bash','-lc',"python3 <<'PY'\nSECRET\nPY\ngit commit -m SECRET"],
            ['bash','-lc',"printf 'SECRET' > result.txt; git status && git push"],
            ['git','config','key','SECRET'], ['git','tag','-mSECRET','v1'],
            ['git','stash','create','SECRET'], ['git','stash','save','SECRET'],
            ['git','submodule','foreach','SECRET'], ['git','bisect','run','./probe.sh','SECRET'],
            ['git','clone','-u','SECRET','repo'],
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                summary = invocation_details({'command':command})['summary']
                self.assertNotIn('SECRET', summary)
                self.assertNotIn('Authorization', summary)
                self.assertLessEqual(len(summary), 768)
        self.assertIn('&& git push', invocation_details({'command':commands[7]})['summary'])
        redirected = invocation_details({'command':['bash','-lc','cat file.txt 2>/dev/null']})['summary']
        self.assertEqual(redirected, 'cat file.txt 2> /dev/null')
        redirect_only = invocation_details({'command':['bash','-lc','> audit.log; cat input.txt']})['summary']
        self.assertEqual(redirect_only, '[redirection only] > audit.log ; cat input.txt')

    def test_truncated_display_round_trips_through_snapshot_validation(self):
        row = invocation_details({'command':['cat','a'*240,'b'*240,'c'*240,'d'*40,'x'],
                                  'cwd':'a'*255 + ' ' + 'b'*20})
        validated = validate_history({'enabled':True, 'rows':[{'id':'a'*64, **row}]})
        self.assertEqual(validated['rows'][0]['summary'], row['summary'])
        self.assertEqual(validated['rows'][0]['directory'], row['directory'])

    def test_file_operands_and_recorded_metadata(self):
        self.assertEqual(invocation_details({'command':['tail','-n','100','--','logs/app.log']})['summary'], 'tail -n 100 -- logs/app.log')
        self.assertEqual(invocation_details({'command':['cat','--','a.txt','b.txt']})['summary'], 'cat -- a.txt b.txt')
        item = {'command':['git','status'], 'cwd':'/workspace/example', 'status':'completed',
                'exit_code':0, 'duration':{'secs':0,'nanos':250000000}}
        row = invocation_details(item)
        self.assertEqual((row['directory'],row['exit_code'],row['duration'],row['outcome']),
                         ('/workspace/example',0,.25,'completed'))
        row = invocation_details({**item,'status':'failed','exit_code':2})
        self.assertEqual(row['outcome'],'failed')
        row = invocation_details({**item,'exit_code':None,'duration':{'secs':float('nan'),'nanos':0}})
        self.assertEqual((row['exit_code'],row['duration'],row['outcome']),(None,None,'unknown'))

    def test_controls_and_templates_never_reach_display(self):
        row = invocation_details({'command':['cat','${TOKEN}'],'cwd':'/work/\x1b[31m${__field.name}\n'})
        self.assertNotIn('$',row['summary']); self.assertNotIn('$',row['directory'])
        self.assertNotIn('\x1b',row['directory']); self.assertNotIn('\n',row['directory'])
        for value in ({'enabled':1,'rows':[]}, {'enabled':True,'rows':[{'id':'bad'}]},
                      {'enabled':True,'rows':[{'id':'a'*64,'summary':'${SECRET}','directory':None}]}):
            with self.assertRaises(ValueError): validate_history(value)
        self.assertEqual(validate_history({'enabled':False,'rows':'ignored'}), {'enabled':False,'rows':[]})


if __name__ == '__main__':
    unittest.main()
