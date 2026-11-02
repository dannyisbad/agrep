"""Verify the exact npm tarball before publication."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
from release_identity import ROOT, version


def validate(path, assets):
    names = {'bin.js', 'checksums.json', 'package.json', 'LICENSE', 'THIRD_PARTY_LICENSES.txt', 'README.md'}
    with tarfile.open(path, 'r:gz') as package:
        members = package.getmembers()
        if len(members) != len(names) or {m.name for m in members} != {'package/' + n for n in names}:
            raise ValueError('unexpected npm package contents')
        if any(not m.isfile() for m in members):
            raise ValueError('npm package must contain regular files only')
        contents = {m.name.removeprefix('package/'): package.extractfile(m).read() for m in members}
    metadata = json.loads(contents['package.json'])
    if metadata['name'] != '@mundy/agrep' or metadata['version'] != version():
        raise ValueError('npm identity mismatch')
    if metadata.get('dependencies') or metadata.get('optionalDependencies') or metadata.get('scripts'):
        raise ValueError('npm package must not install bootstrap dependencies or scripts')
    if metadata['bin'] != {'agrep': 'bin.js'}:
        raise ValueError('npm entrypoint mismatch')
    for name in ('bin.js', 'LICENSE', 'README.md'):
        if contents[name] != (ROOT / 'npm' / name).read_bytes():
            raise ValueError(f'npm {name} differs from reviewed source')
    if contents['THIRD_PARTY_LICENSES.txt'] != (ROOT / 'THIRD_PARTY_LICENSES.txt').read_bytes():
        raise ValueError('npm notices mismatch')
    targets = json.loads((ROOT / 'bench/targets.json').read_text())
    expected = {t['os'] + '-' + t['arch']: {
        'asset': t['asset'], 'sha256': hashlib.sha256((assets / t['asset']).read_bytes()).hexdigest()
    } for t in targets}
    if json.loads(contents['checksums.json']) != expected:
        raise ValueError('npm hashes do not match the six release assets')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('package', type=Path)
    args = parser.parse_args()
    validate(args.package, args.assets)
    print('npm package OK')
