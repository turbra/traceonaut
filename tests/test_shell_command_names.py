"""Command-name projection preserves quoting and never inspects program bodies."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from traceonaut.shell_command_names import command_names


class CommandNameTests(unittest.TestCase):
    def shell(self, text):
        return command_names(['/bin/bash', '-lc', text])

    def test_sequences_pipes_conditionals_and_repeated_names(self):
        self.assertEqual(self.shell('git status; git diff | head\nfalse && curl x || printf failed'),
                         (['git', 'git', 'head', 'false', 'curl', 'printf'], True))

    def test_quoted_operators_programs_and_multiline_arguments(self):
        self.assertEqual(self.shell('printf "; curl secret" "&&"\npython3 -c "print(\'git\')"'),
                         (['printf', 'python3'], True))
        self.assertEqual(self.shell("printf 'line1\ncurl hidden'"), (['printf'], True))
        self.assertEqual(command_names(['printf', ';', 'curl']), (['printf'], True))

    def test_paths_assignments_and_wrapper_options(self):
        self.assertEqual(self.shell('X=secret env -u TOKEN sudo -n -u example /usr/bin/git status'), (['git'], True))
        self.assertEqual(self.shell('command -- curl -I x; exec env --ignore-environment A=x python3 -V'),
                         (['curl', 'python3'], True))
        self.assertEqual(self.shell('sudo --unknown git status'), ([], False))
        self.assertEqual(self.shell('env -u'), ([], False))

    def test_control_words_and_quoted_substitutions_are_arguments(self):
        self.assertEqual(self.shell("echo done; printf '%s' 'if'; rg for file"),
                         (['echo', 'printf', 'rg'], True))
        self.assertEqual(self.shell("rg '\\$[(]' file; rg '$(curl hidden)' file; rg '`git hidden`' file"),
                         (['rg', 'rg', 'rg'], True))

    def test_redirects_are_not_commands(self):
        self.assertEqual(self.shell('git status >out 2>err; curl x 2>&1 | head'), (['git', 'curl', 'head'], True))
        self.assertEqual(self.shell('cat < input; printf x >>output'), (['cat', 'printf'], True))
        self.assertEqual(self.shell('git >'), ([], False))

    def test_heredoc_bodies_are_not_command_text(self):
        self.assertEqual(self.shell("python3 <<'PY'\ncurl SECRET\ngit push\nPY\ncurl x"), (['python3','curl'], True))
        self.assertEqual(self.shell('cat <<-END\n\tgit hidden\n\tEND\ngit status'), (['cat','git'], True))
        self.assertEqual(self.shell('cat <<A <<B\ngit hidden\nA\ncurl hidden\nB'), (['cat'], True))
        self.assertEqual(self.shell('cat <<EOF\nmissing'), ([], False))
        self.assertEqual(self.shell("bash <<'SH'\ncurl hidden\nSH"), (['bash'], False))

    def test_nested_shell_is_literal_and_bounded(self):
        self.assertEqual(self.shell("bash -lc 'git status; curl x'"), (['git','curl'], True))
        self.assertEqual(command_names(['bash', 'script.sh']), (['bash'], False))
        self.assertEqual(command_names(['eval', 'git status']), (['eval'], False))
        self.assertEqual(command_names(['git'], depth=5), ([], False))

    def test_dynamic_and_control_syntax_are_partial(self):
        for text in ('for x in a; do git status; done', 'if true; then curl x; fi',
                     'git $(curl x)', '$PROGRAM status', 'git `echo status`', 'f() { curl x; }'):
            self.assertEqual(self.shell(text), ([], False), text)

    def test_comments_empty_and_caps(self):
        self.assertEqual(self.shell('# curl hidden\ngit status # curl hidden\n'), (['git'], True))
        self.assertEqual(self.shell('A=value'), ([], True))
        names, complete = self.shell(';'.join(['git status']*129))
        self.assertEqual(len(names), 128); self.assertFalse(complete)
        for command in (None, [], [3], ['git\0curl'], ['git', 'x'*131073]):
            self.assertEqual(command_names(command), ([], False))


if __name__ == '__main__':
    unittest.main()
