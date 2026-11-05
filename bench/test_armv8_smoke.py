import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from release_identity import version
from smoke_armv8 import SESSION, TEXT, smoke


class Armv8SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('config.json', 'tokenizer.json', 'model.safetensors'):
            (self.root / name).touch()
        self.outputs = [
            (f'agrep {version()}\n', ''),
            ('indexed 3 messages across 2 sessions', 'semantic 6/6 rows'),
            (json.dumps({'session': SESSION, 'snippet': TEXT}), ''),
            (json.dumps({'hits': [{'session': SESSION, 'window': [{'text': TEXT}]}]}), ''),
            ('\n'.join(map(json.dumps, [
                {'semantic': {'state': 'ready'}, 'semantic_coverage': {'complete': True}},
                {'session': SESSION, 'sem_score': 0.98},
            ])), ''),
        ]

    def execute(self, fail_at=None, code=0):
        calls = []

        def run(command, **kwargs):
            index = len(calls)
            calls.append((command, kwargs['env']))
            stdout, stderr = self.outputs[index]
            return subprocess.CompletedProcess(command, code if index == fail_at else 0, stdout, stderr)

        with patch('smoke_armv8.subprocess.run', side_effect=run):
            smoke(self.root / 'agrep', self.root)
        return calls

    def test_all_commands_run_under_cortex_a72_with_isolated_data_and_existing_model(self):
        with patch.dict('os.environ', {'AGREP_DATA_DIR': '/private/live', 'CODEX_HOME': '/private/store'}):
            calls = self.execute()
        self.assertEqual(len(calls), 5)
        for command, env in calls:
            self.assertEqual(command[:3], ['qemu-aarch64', '-cpu', 'cortex-a72'])
            self.assertTrue(self.root.samefile(env['AGREP_MODEL_DIR']))
            self.assertNotEqual(env['AGREP_DATA_DIR'], '/private/live')
            self.assertNotIn('CODEX_HOME', env)
        self.assertEqual(calls[1][0][-2:], ['index', '--semantic'])
        self.assertIn('recall', calls[3][0])
        self.assertIn('-s', calls[4][0])

    def test_every_nonzero_exit_including_sigill_fails_the_guard(self):
        for index in range(5):
            for code in (1, 2, -4, 132):
                with self.subTest(command=index, code=code):
                    with self.assertRaisesRegex(AssertionError, f'exit {code}'):
                        self.execute(fail_at=index, code=code)

    def test_meaning_requires_complete_semantic_results(self):
        self.outputs[-1] = (json.dumps({'semantic': {'state': 'ready'},
                                       'semantic_coverage': {'complete': False}}), '')
        with self.assertRaises(AssertionError):
            self.execute()

    def test_missing_model_fails_before_running_the_binary(self):
        (self.root / 'model.safetensors').unlink()
        with patch('smoke_armv8.subprocess.run') as run:
            with self.assertRaises(FileNotFoundError):
                smoke(self.root / 'agrep', self.root)
        run.assert_not_called()


class SharedModelSmokeTests(unittest.TestCase):
    def test_semantic_smoke_drains_inline_and_preserves_the_model(self):
        import smoke as native_smoke
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / 'model'
            model.mkdir()
            calls = []

            def run(command, **kwargs):
                args = command[1:]
                calls.append((args, kwargs['env']))
                code, output = 0, ''
                if args == ['--version']:
                    output = f'agrep {version()}'
                elif args == ['setup', '--yes']:
                    pass
                elif args == ['index', '--semantic']:
                    pass
                elif args[0] == 'cobalt lantern':
                    output = '\n'.join(map(json.dumps, [
                        {'kind': 'agrep-meta'}, {'session': native_smoke.SESSION}]))
                elif args[0] == 'absolutely_missing_7921':
                    code = 1
                elif args[0] == 'around':
                    output = native_smoke.TEXT
                elif args[0] == 'status':
                    output = '{"ready": true}'
                elif '-s' in args:
                    output = json.dumps({'session': native_smoke.SESSION, 'sem_score': 0.9})
                else:
                    self.fail(f'unexpected command: {args}')
                return subprocess.CompletedProcess(command, code, output, '')

            with patch('smoke.subprocess.run', side_effect=run):
                native_smoke.smoke(['agrep'], semantic=True, model=model)
            self.assertTrue(model.is_dir())
            self.assertTrue(all(Path(env['AGREP_MODEL_DIR']).samefile(model) for _, env in calls))
            self.assertIn(['index', '--semantic'], [args for args, _ in calls])
