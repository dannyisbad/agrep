"""Enforce the binary-only wheel contract and validate every RECORD digest."""
import argparse
import base64
import csv
from email.parser import BytesParser
import io
import json
from pathlib import Path
import stat
import zipfile
import hashlib
from release_identity import ROOT, version
from validate_binary_privacy import validate_bytes
from binary_format import validate_target


def validate(path):
    v = version()
    target = next((t for t in json.loads((ROOT / 'bench/targets.json').read_text(encoding='utf-8'))
                   if path.name == f'agrep-{v}-py3-none-{t["platform"]}.whl'), None)
    if target is None:
        raise ValueError(f'unsupported wheel filename: {path.name}')
    info = f'agrep-{v}.dist-info'
    binary = f'agrep/{target["exe"]}'
    expected = {'agrep/__init__.py', binary, f'{info}/WHEEL', f'{info}/METADATA',
                f'{info}/entry_points.txt', f'{info}/RECORD',
                f'{info}/licenses/LICENSE', f'{info}/licenses/THIRD_PARTY_LICENSES.txt'}
    with zipfile.ZipFile(path) as wheel:
        if len(wheel.namelist()) != len(expected) or set(wheel.namelist()) != expected:
            raise ValueError('wheel contents differ from binary-only allowlist')
        metadata = BytesParser().parsebytes(wheel.read(f'{info}/METADATA'))
        if metadata['Name'] != 'agrep' or metadata['Version'] != v or metadata.get_all('Requires-Dist'):
            raise ValueError('invalid metadata or runtime Python dependencies')
        tags = BytesParser().parsebytes(wheel.read(f'{info}/WHEEL'))
        if tags['Tag'] != f'py3-none-{target["platform"]}' or tags['Root-Is-Purelib'] != 'false':
            raise ValueError('invalid platform tag')
        if wheel.read('agrep/__init__.py') != (ROOT / 'packaging/agrep/__init__.py').read_bytes():
            raise ValueError('launcher differs from reviewed source')
        if wheel.read(f'{info}/entry_points.txt') != b'[console_scripts]\nagrep = agrep:main\n':
            raise ValueError('invalid console entrypoint')
        for name in ('LICENSE', 'THIRD_PARTY_LICENSES.txt'):
            if wheel.read(f'{info}/licenses/{name}') != (ROOT / name).read_bytes():
                raise ValueError('license mismatch')
        mode = wheel.getinfo(binary).external_attr >> 16
        if not stat.S_ISREG(mode) or mode & 0o111 != 0o111:
            raise ValueError('binary must be executable regular file')
        validate_bytes(wheel.read(binary))
        validate_target(wheel.read(binary), target)
        records = list(csv.reader(io.StringIO(wheel.read(f'{info}/RECORD').decode())))
        if len(records) != len(expected) or {r[0] for r in records} != expected:
            raise ValueError('invalid RECORD members')
        for name, digest, size in records:
            if name == f'{info}/RECORD':
                if digest or size:
                    raise ValueError('RECORD must have empty self-hash')
                continue
            raw = wheel.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
            if digest != 'sha256=' + encoded or size != str(len(raw)):
                raise ValueError(f'RECORD mismatch: {name}')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheels', type=Path, nargs='+')
    for wheel in parser.parse_args().wheels:
        validate(wheel)
        print(f'wheel OK: {wheel}')
