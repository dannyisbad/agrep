"""Smoke an installed launcher or binary using only a temporary agent HOME."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

SESSION = '01930000-0000-7000-8000-000000000001'
DISTRACTORS = ['Baking sourdough bread needs flour and a hot oven.', 'The football match ended after extra time.', 'Volcanoes erupt when underground magma reaches the surface.', 'A string quartet rehearsed the final movement.']
TEXT = 'The cobalt lantern project stores the migration plan beside the release checklist.'


def assert_semantic_ranking(output):
    rows = [r for r in map(json.loads, output.splitlines()) if r.get('kind') != 'agrep-meta' and r.get('session')]
    assert rows and rows[0]['session'] == SESSION, output
    assert rows[0].get('sem_score') is not None or rows[0].get('lane') == 'semantic', output


def smoke(command, semantic=False):
    with tempfile.TemporaryDirectory(prefix='agrep-smoke-') as temporary:
        root = Path(temporary)
        home = root / 'home'
        path = home / '.pi/agent/sessions/smoke/session.jsonl'
        path.parent.mkdir(parents=True)
        rows = [
            {'type': 'session', 'version': 3, 'id': SESSION, 'cwd': '/work/smoke', 'timestamp': '2026-10-29T12:00:00Z'},
            {'type': 'message', 'id': 'm1', 'timestamp': '2026-10-29T12:00:01Z',
             'message': {'role': 'user', 'content': TEXT}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        for i, text in enumerate(DISTRACTORS):
            distractor = [
                {'type':'session', 'version':3, 'id':f'distractor-{i}', 'cwd':'/work/smoke'},
                {'type':'message', 'id':'m1', 'message':{'role':'user','content':text}},
            ]
            path.with_name(f'distractor-{i}.jsonl').write_text('\n'.join(json.dumps(r) for r in distractor) + '\n', encoding='utf-8')
        env = dict(os.environ)
        # Remove caller/source overrides so no real agent stores enter the smoke.
        for key in list(env):
            if key.startswith(('AGREP_', 'CLAUDE_', 'CODEX_', 'PI_', 'OPENCODE_', 'GEMINI_', 'KIMI_', 'CLINE_', 'CURSOR_', 'CRUSH_')):
                del env[key]
        env.update(HOME=str(home), USERPROFILE=str(home), AGREP_HOME=str(home),
                   AGREP_DATA_DIR=str(root / 'data'), XDG_CONFIG_HOME=str(home / '.config'),
                   XDG_DATA_HOME=str(home / '.local/share'), XDG_CACHE_HOME=str(root / 'cache'),
                   LOCALAPPDATA=str(root / 'cache'), APPDATA=str(home / '.config'), AGREP_PROFILE='classic')

        def run(*args, expected=0):
            result = subprocess.run([*command, *args], env=env, capture_output=True, text=True, encoding='utf-8', timeout=300)
            if result.returncode != expected:
                raise AssertionError(f'{args}: exit {result.returncode}, expected {expected}\n{result.stdout}\n{result.stderr}')
            return result.stdout

        from release_identity import version
        assert run('--version').strip() == f'agrep {version()}'
        if semantic:
            run('setup', '--yes')
        run('index')
        output = run('cobalt lantern', '--lexical', '--json')
        objects = [json.loads(line) for line in output.splitlines()]
        assert objects[0]['kind'] == 'agrep-meta'
        assert any(row.get('session') == SESSION for row in objects[1:]), output
        run('absolutely_missing_7921', '--lexical', expected=1)
        around = run('around', SESSION, '0', '--json')
        assert TEXT in around, around
        status = run('status', '--json')
        assert json.loads(status), status
        if semantic:
            run('__work')
            semantic_output = run('where was the project migration checklist saved', '-s', '--json')
            assert_semantic_ranking(semantic_output)
        print('smoke OK: version, index, keyword hit/miss, around, status' + (', semantic' if semantic else ''))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--semantic', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('supply the installed command after --')
    smoke(command, semantic=args.semantic)
