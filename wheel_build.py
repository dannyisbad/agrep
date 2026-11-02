"""Dependency-free PEP 517 builder for prebuilt platform wheels. No sdist."""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile
from bench.binary_format import validate_target

ROOT = Path(__file__).resolve().parent


def version():
    return re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M)[1]


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    platform = os.environ.get('AGREP_WHEEL_PLAT')
    target = next((t for t in json.loads((ROOT / 'bench/targets.json').read_text())
                   if t['platform'] == platform), None)
    if target is None:
        raise RuntimeError('AGREP_WHEEL_PLAT must name a supported release platform')
    binary = Path(os.environ.get('AGREP_WHEEL_BINARY', ROOT / '_bin' / target['exe']))
    if not binary.is_file() or binary.is_symlink() or binary.stat().st_size == 0:
        raise RuntimeError('stage the release binary with AGREP_WHEEL_BINARY before building')
    validate_target(binary.read_bytes(), target)
    v = version()
    info = f'agrep-{v}.dist-info'
    tag = f'py3-none-{platform}'
    files = {
        'agrep/__init__.py': (ROOT / 'packaging/agrep/__init__.py').read_bytes(),
        f'agrep/{target["exe"]}': binary.read_bytes(),
        f'{info}/METADATA': (f'Metadata-Version: 2.4\nName: agrep\nVersion: {v}\n'
             'Summary: Search, recall and resume your local AI agent history.\n'
             'Requires-Python: >=3.10\nLicense-Expression: MIT\n'
             'License-File: LICENSE\nLicense-File: THIRD_PARTY_LICENSES.txt\n'
             'Description-Content-Type: text/markdown\n\n' + (ROOT / 'README.md').read_text()).encode(),
        f'{info}/WHEEL': f'Wheel-Version: 1.0\nGenerator: agrep\nRoot-Is-Purelib: false\nTag: {tag}\n'.encode(),
        f'{info}/entry_points.txt': b'[console_scripts]\nagrep = agrep:main\n',
    }
    for name in ('LICENSE', 'THIRD_PARTY_LICENSES.txt'):
        files[f'{info}/licenses/{name}'] = (ROOT / name).read_bytes()
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, raw in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
        writer.writerow((name, 'sha256=' + digest, len(raw)))
    writer.writerow((f'{info}/RECORD', '', ''))
    files[f'{info}/RECORD'] = record.getvalue().encode()
    path = Path(wheel_directory) / f'agrep-{v}-{tag}.whl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as wheel:
        for name, raw in files.items():
            member = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = (0o100755 if name == f'agrep/{target["exe"]}' else 0o100644) << 16
            wheel.writestr(member, raw)
    return path.name


def build_sdist(sdist_directory, config_settings=None):
    raise RuntimeError('agrep publishes binary wheels only; build from the Git checkout with Cargo')
