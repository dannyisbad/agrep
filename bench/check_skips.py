"""Reject unregistered platform assertions, runtime skips, todos and expected failures."""
import argparse
import ast
import json
from pathlib import Path
import re
from release_identity import ROOT

TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|(?:br|r)(\#*)"[\s\S]*?"\1|b?"(?:\\[\s\S]|[^"\\])*"|b?\'(?:\\.|[^\'\\])\'|[A-Za-z_][A-Za-z_0-9]*|[^\s]')

# The sole runtime exclusion: at the start of a registered GPU-only test,
# acquire through the product capability check or return without assertions.
METAL_GATE = 'capability(semantic_device::metal_device)'
METAL_GUARD = re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[^\s]',
    'let Some(device) = crate::semantic_device::metal_device() else { return; };')


def scan_rust(text, inherited=()):
    # Literals are opaque tokens to the item scan; gates keep their source text, so a
    # registration names the platform value it was granted for.
    tokens, source = [], []
    for match in TOKEN.finditer(text):
        token = match.group()
        if token.startswith(('//', '/*')):
            continue
        source.append(token)
        if token.startswith(('"', 'b"', "'", "b'")) or re.match(r'(br|r)#*"', token):
            token = 'LITERAL'
        tokens.append(token)

    def closing(i, end):
        opening = tokens[i]
        close = {'[': ']', '{': '}', '(': ')'}[opening]
        depth = 1
        for j in range(i + 1, end):
            if tokens[j] == opening: depth += 1
            if tokens[j] == close: depth -= 1
            if depth == 0: return j
        raise ValueError('unbalanced Rust item while checking test gates')

    def attribute(i, end):
        inner = tokens[i:i + 2] == ['#', '!']
        bracket = i + (2 if inner else 1)
        if bracket >= end or tokens[bracket] != '[': return None
        stop = closing(bracket, end)
        return inner, ''.join(source[bracket + 1:stop]), stop + 1

    def gate(value):
        return value.startswith(('cfg(', 'cfg_attr(')) and value != 'cfg(test)'

    full, partial, children = {}, {}, []
    def items(start, end, modules=(), parent=inherited):
        # Inner attributes apply to the entire source/module, not the next item.
        attrs, i = [], start
        while i < end and (a := attribute(i, end)) and a[0]:
            if gate(a[1]): parent += (a[1],)
            i = a[2]
        while i < end:
            a = attribute(i, end)
            if a:
                attrs.append(a[1]); i = a[2]; continue
            if tokens[i:i + 2] == ['pub', '(']:
                i = closing(i + 1, end) + 1; continue
            if tokens[i] in ('pub', 'unsafe', 'async'):
                i += 1; continue
            if tokens[i] in ('fn', 'mod') and i + 1 < end:
                kind, name = tokens[i:i + 2]
                j = i + 2
                while j < end and tokens[j] not in ('{', ';'): j += 1
                gates = parent + tuple(a for a in attrs if gate(a))
                key = '::'.join((*modules, name))
                if kind == 'fn' and 'test' in attrs and gates:
                    full[key] = list(dict.fromkeys(gates))
                if j < end and tokens[j] == '{':
                    stop = closing(j, end)
                    if kind == 'mod':
                        items(j + 1, stop, (*modules, name), gates)
                    elif 'test' in attrs:
                        body_gates = []
                        k = j + 1
                        if tokens[k:k + len(METAL_GUARD)] == METAL_GUARD:
                            full[key] = list(dict.fromkeys((*gates, METAL_GATE)))
                            k += len(METAL_GUARD)
                        while k < stop:
                            a = attribute(k, stop)
                            if a:
                                if gate(a[1]): body_gates.append(a[1])
                                k = a[2]; continue
                            if tokens[k:k + 3] == ['cfg', '!', '(']:
                                last = closing(k + 2, stop)
                                body_gates.append(''.join(source[k:last + 1])); k = last + 1; continue
                            if tokens[k] == 'return': body_gates.append('return')
                            k += 1
                        if body_gates: partial[key] = list(dict.fromkeys(body_gates))
                    i = stop + 1
                else:
                    if kind == 'mod': children.append(((*modules, name), gates))
                    i = j + 1
                attrs = []; continue
            if tokens[i] == '{': i = closing(i, end) + 1
            else: i += 1
            attrs = []
    items(0, len(tokens))
    return full, partial, children


def gated_tests(text):
    return scan_rust(text)[0]


def validate(logs, root=ROOT):
    root = root.resolve()
    policy = json.loads((root / 'bench/test-skips.json').read_text(encoding='utf-8'))
    actual, partial, visited = {}, {}, set()

    def source(path, inherited=()):
        path = path.resolve()
        if path in visited: return
        visited.add(path)
        text = path.read_text(encoding='utf-8')
        if re.search(r'#\s*!?\s*\[\s*(?:ignore\b|cfg_attr\([^\n]*\bignore\b)', text):
            raise ValueError(f'unlisted ignored test: {path.relative_to(root)}')
        full, body, children = scan_rust(text, inherited)
        relative = path.relative_to(root).as_posix()
        actual.update({f'{relative}::{name}': gates for name, gates in full.items()})
        partial.update({f'{relative}::{name}': gates for name, gates in body.items()})
        base = path.parent if path.name in ('lib.rs', 'main.rs', 'mod.rs') else path.with_suffix('')
        for names, gates in children:
            child = base.joinpath(*names).with_suffix('.rs')
            if not child.exists(): child = base.joinpath(*names) / 'mod.rs'
            if child.exists(): source(child, gates)

    # Parent modules must run before independently enumerated child files.
    paths = sorted((root / 'crates').rglob('*.rs'), key=lambda p: (len(p.parts), p.name not in ('lib.rs', 'main.rs', 'mod.rs'), str(p)))
    for path in paths: source(path)
    for field, found in [('platform_inapplicable', actual), ('partial_assertions', partial)]:
        registered = {}
        for entry in policy.get(field, []):
            if not entry.get('reason') or not entry.get('gates') or entry['test'] in registered:
                raise ValueError(f'invalid or duplicate {field} registration')
            registered[entry['test']] = entry['gates']
        if found != registered:
            missing = sorted(k for k in found if found[k] != registered.get(k))
            stale = sorted(set(registered) - set(found))
            raise ValueError(f'unlisted platform test gates ({field}): {missing}; stale registry entries: {stale}')
    for path in (root / 'bench').glob('test_*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.module == 'unittest' and any(item.name in ('skip', 'skipIf', 'skipUnless', 'expectedFailure') for item in node.names):
                raise ValueError(f'unlisted test skip or expected failure import: {path.name}')
            if ((isinstance(node, ast.Attribute) and node.attr in ('skip', 'skipIf', 'skipUnless', 'skipTest', 'expectedFailure')) or (isinstance(node, ast.Name) and node.id in ('skip', 'skipIf', 'skipUnless', 'skipTest', 'expectedFailure'))):
                raise ValueError(f'unlisted test skip or expected failure: {path.name}')
    for path in (root / 'npm').glob('test_*.js'):
        text = path.read_text(encoding='utf-8')
        def mask(match):
            token = match.group()
            if token[0] in ('"', "'") and token[1:-1] in ('skip', 'todo') and text[match.end():].lstrip().startswith(':'):
                return token[1:-1]
            return ' ' if token.startswith(('//', '/*')) else 'LITERAL'
        code = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`', mask, text)
        if re.search(r'\b(?:test|it|t)\.(?:skip|todo)\s*\(|\b(?:skip|todo)\s*:\s*(?:true|LITERAL)\b', code):
            raise ValueError(f'unlisted test skip or todo: {path.name}')
    for path in logs:
        if re.search(r'\b[1-9][0-9]* ignored\b|\.\.\. ignored\b|\bskipped=[1-9]|\b(?:skipped|todo)\s+[1-9]|# (?:SKIP|TODO)\b|\bexpected failures=[1-9]', path.read_text(encoding='utf-8'), re.I):
            raise ValueError(f'unlisted skipped or todo test in {path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', type=Path, nargs='*')
    validate(parser.parse_args().logs)
    print('test skip policy OK')
