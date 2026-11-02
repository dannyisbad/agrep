"""Stage hashes and npm from the complete six-target binary/wheel set."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from release_identity import ROOT, validate
from validate_binary_privacy import validate as binary_privacy
from validate_wheel import validate as wheel_validate
from validate_npm import validate as npm_validate


def prepare(assets, wheels, output):
    targets = json.loads((ROOT / 'bench/targets.json').read_text())
    expected = {t['asset'] for t in targets}
    if {p.name for p in assets.iterdir()} != expected:
        raise ValueError('release requires exactly the six target binaries')
    paths = list(wheels.glob('*.whl'))
    validate(wheels=paths)
    if len(paths) != 6 or {wheel_validate(p)['asset'] for p in paths} != expected:
        raise ValueError('release requires exactly the six target wheels')
    import zipfile
    for path in paths:
        target = wheel_validate(path)
        with zipfile.ZipFile(path) as wheel:
            if wheel.read('agrep/' + target['exe']) != (assets / target['asset']).read_bytes():
                raise ValueError(f'{path.name}: binary differs from release asset')
    output.mkdir(parents=True, exist_ok=False)
    staged = output / 'assets'
    shutil.copytree(assets, staged)
    npm = output / 'npm'
    shutil.copytree(ROOT / 'npm', npm)
    manifest = {}
    for target in targets:
        asset = staged / target['asset']
        binary_privacy(asset)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        asset.with_name(asset.name + '.sha256').write_text(f'{digest}  {asset.name}\n')
        manifest[f'{target["os"]}-{target["arch"]}'] = {'asset': asset.name, 'sha256': digest}
    (npm / 'checksums.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for name in ('LICENSE', 'THIRD_PARTY_LICENSES.txt'):
        shutil.copy2(ROOT / name, staged / name)
        shutil.copy2(ROOT / name, npm / name)
    import os
    subprocess.run(['npm.cmd' if os.name == 'nt' else 'npm', 'pack', '--ignore-scripts'],
                   cwd=npm, check=True, env={**os.environ, 'NPM_CONFIG_OFFLINE': 'true', 'NPM_CONFIG_UPDATE_NOTIFIER': 'false'})
    archives = list(npm.glob('*.tgz'))
    if len(archives) != 1:
        raise ValueError('npm pack must produce exactly one archive')
    npm_validate(archives[0], staged)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--wheels', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.assets, args.wheels, args.output)
