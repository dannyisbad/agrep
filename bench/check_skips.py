"""Reject runtime skips and require exact registry entries for platform-gated tests."""
import argparse
import json
from pathlib import Path
import re
from release_identity import ROOT

# Mask comments and literals before inspecting Rust items. Raw fixture JSON must
# never manufacture attributes, functions or braces in this scan.
TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|(?:br|r)(\#*)"[\s\S]*?"\1|b?"(?:\\[\s\S]|[^"\\])*"|b?\'(?:\\.|[^\'\\])\'|[A-Za-z_][A-Za-z_0-9]*|[^\s]')

def gated_tests(text):
    tokens = []
    for match in TOKEN.finditer(text):
        token = match.group()
        if token.startswith(('//', '/*', '"', 'b"', "'", "b'")) or re.match(r'(br|r)#*"', token):
            token = 'LITERAL'
        tokens.append(token)

    def closing(i, opening, end):
        depth = 1
        for j in range(i + 1, end):
            if tokens[j] == opening: depth += 1
            if tokens[j] == {'[': ']', '{': '}', '(': ')'}[opening]: depth -= 1
            if depth == 0: return j
        raise ValueError('unbalanced Rust item while checking test gates')

    found = {}
    def items(start, end, modules=(), inherited=()):
        i, attrs = start, []
        while i < end:
            if tokens[i:i+2] == ['#', '[']:
                stop = closing(i+1, '[', end)
                attrs.append(''.join(tokens[i+2:stop])); i = stop+1; continue
            if tokens[i] in ('pub', 'unsafe', 'async'):
                i += 1; continue
            if tokens[i] in ('fn', 'mod') and i+1 < end:
                kind, name = tokens[i:i+2]
                j = i+2
                while j < end and tokens[j] not in ('{', ';'): j += 1
                gates = inherited + tuple(a for a in attrs if a.startswith(('cfg(', 'cfg_attr(')) and a != 'cfg(test)')
                if kind == 'fn' and 'test' in attrs and gates:
                    found['::'.join((*modules, name))] = list(gates)
                if j < end and tokens[j] == '{':
                    stop = closing(j, '{', end)
                    if kind == 'mod': items(j+1, stop, (*modules, name), gates)
                    i = stop+1
                else: i = j+1
                attrs = []; continue
            if tokens[i] == '{':
                i = closing(i, '{', end)+1
            else: i += 1
            attrs = []
    items(0, len(tokens))
    return found


def validate(logs, root=ROOT):
    policy = json.loads((root / 'bench/test-skips.json').read_text())
    actual = {}
    for path in (root / 'crates').rglob('*.rs'):
        text = path.read_text()
        if re.search(r'#\s*\[\s*(?:ignore\b|cfg_attr\([^\n]*\bignore\b)', text):
            raise ValueError(f'unlisted ignored test: {path.relative_to(root)}')
        for name, gates in gated_tests(text).items():
            actual[f'{path.relative_to(root).as_posix()}::{name}'] = gates
    registered = {}
    for entry in policy['platform_inapplicable']:
        if not entry.get('reason') or not entry.get('gates') or entry['test'] in registered:
            raise ValueError('invalid or duplicate platform test registration')
        registered[entry['test']] = entry['gates']
    if actual != registered:
        missing = sorted(k for k in actual if actual[k] != registered.get(k))
        stale = sorted(set(registered)-set(actual))
        raise ValueError(f'unlisted platform test gates: {missing}; stale registry entries: {stale}')
    for path in (root / 'bench').glob('test_*.py'):
        if re.search(r'@(?:unittest\.)?skip|\bself\.skipTest\s*\(', path.read_text()):
            raise ValueError(f'unlisted test skip: {path.name}')
    for path in (root / 'npm').glob('test_*.js'):
        if re.search(r'\btest\.skip\s*\(|\bskip\s*:\s*(?:true|[\"\'])', path.read_text()):
            raise ValueError(f'unlisted test skip: {path.name}')
    for path in logs:
        if re.search(r'\b[1-9][0-9]* ignored\b|\.\.\. ignored\b|\bskipped=[1-9]|\bskipped\s+[1-9]|# SKIP\b', path.read_text()):
            raise ValueError(f'unlisted skipped test in {path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', type=Path, nargs='*')
    validate(parser.parse_args().logs)
    print('test skip policy OK')
