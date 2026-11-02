"""Validate a single version across Cargo, Python, npm, tag and wheel metadata."""
import argparse
from email.parser import BytesParser
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def version(root=ROOT):
    versions = []
    for name in ('Cargo.toml', 'pyproject.toml'):
        text = (root / name).read_text()
        versions.append(re.search(r'^version = "([^"]+)"', text, re.M)[1])
    versions.append(json.loads((root / 'npm/package.json').read_text())['version'])
    if len(set(versions)) != 1:
        raise ValueError(f'version mismatch: Cargo, Python, npm = {versions}')
    return versions[0]


def validate(root=ROOT, tag=None, wheels=()):
    expected = version(root)
    if tag is not None and tag != f'v{expected}':
        raise ValueError(f'tag {tag!r} does not match v{expected}')
    for path in wheels:
        with zipfile.ZipFile(path) as wheel:
            entries = [n for n in wheel.namelist() if n.endswith('.dist-info/METADATA')]
            if len(entries) != 1:
                raise ValueError(f'{path}: expected one METADATA')
            metadata = BytesParser().parsebytes(wheel.read(entries[0]))
            if metadata['Name'] != 'agrep' or metadata['Version'] != expected:
                raise ValueError(f'{path}: wheel version mismatch')
    return expected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag')
    parser.add_argument('wheels', nargs='*', type=Path)
    args = parser.parse_args()
    print(validate(tag=args.tag, wheels=args.wheels))
