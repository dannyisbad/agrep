"""Require the Linux arm64 binary to search on a Cortex-A72 without FP16."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from release_identity import ROOT, version

SESSION = '11111111-1111-4111-8111-111111111111'
TEXT = 'how do i fix the flaky timer test'


def smoke(binary, model):
    binary, model = Path(binary).resolve(), Path(model).resolve()
    for name in ('config.json', 'tokenizer.json', 'model.safetensors'):
        if not (model / name).is_file():
            raise FileNotFoundError(f'provision the model before the ARMv8.0 smoke: {name}')
    with tempfile.TemporaryDirectory(prefix='agrep-armv8-') as temporary:
        root = Path(temporary)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('AGREP_', 'CLAUDE_', 'CODEX_', 'PI_', 'OPENCODE_',
                                      'GEMINI_', 'KIMI_', 'CLINE_', 'CURSOR_', 'CRUSH_'))}
        env.update(HOME=str(root), USERPROFILE=str(root),
                   AGREP_HOME=str(ROOT / 'crates/agrep/tests/fixtures/claude/home'),
                   AGREP_DATA_DIR=str(root / 'data'), AGREP_MODEL_DIR=str(model),
                   XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'),
                   XDG_DATA_HOME=str(root / 'data-home'), LOCALAPPDATA=str(root / 'local'),
                   APPDATA=str(root / 'config'), AGREP_PROFILE='classic')

        def run(*args):
            result = subprocess.run(['qemu-aarch64', '-cpu', 'cortex-a72', str(binary), *args],
                                    env=env, capture_output=True, text=True, encoding='utf-8',
                                    timeout=300)
            if result.returncode != 0:
                raise AssertionError(f'{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}')
            return result

        assert run('--version').stdout.strip() == f'agrep {version()}'
        indexed = run('index', '--semantic')
        assert 'indexed 3 messages across 2 sessions' in indexed.stdout, indexed.stdout
        assert 'semantic 6/6 rows' in indexed.stderr, indexed.stderr
        keyword = list(map(json.loads, run('flaky', '--lexical', '--json', '--no-auto').stdout.splitlines()))
        assert any(row.get('session') == SESSION and row.get('snippet') == TEXT
                   for row in keyword), keyword
        recalled = json.loads(run('recall', 'flaky', '--json', '--no-auto').stdout)
        assert any(hit['session'] == SESSION and any(row.get('text') == TEXT for row in hit['window'])
                   for hit in recalled['hits']), recalled
        meaning = list(map(json.loads, run('-s', 'flaky timer test', '--json', '--no-auto').stdout.splitlines()))
        meta, hits = meaning[0], meaning[1:]
        assert meta['semantic']['state'] == 'ready' and meta['semantic_coverage']['complete'], meta
        assert hits and hits[0]['session'] == SESSION and hits[0]['sem_score'] > 0, hits
        print('ARMv8.0 smoke OK: version, fixture index, keyword, recall, CPU meaning; no FP16 required')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    args = parser.parse_args()
    smoke(args.binary, args.model)
