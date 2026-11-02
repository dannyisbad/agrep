"""Build a release binary with source/build paths remapped on every target."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess


def build_environment(root, env):
    output = dict(env)
    flags = output.get('CARGO_ENCODED_RUSTFLAGS')
    flags = flags.split('\x1f') if flags is not None else shlex.split(output.get('RUSTFLAGS', ''))
    paths = {str(Path(root).resolve()), str(Path.home())}
    for key in ('HOME', 'USERPROFILE', 'CARGO_HOME', 'RUSTUP_HOME', 'RUNNER_TEMP',
                'RUNNER_WORKSPACE', 'RUNNER_TOOL_CACHE', 'TEMP', 'TMP', 'TMPDIR'):
        if env.get(key):
            paths.add(env[key])
    paths.add(str(Path(env.get('CARGO_HOME', Path.home() / '.cargo')).resolve()))
    paths.add(str(Path(env.get('RUSTUP_HOME', Path.home() / '.rustup')).resolve()))
    variants = {p.replace('\\', '/') for p in paths} | paths
    if os.name == 'nt':
        variants |= {p.replace('/', '\\') for p in variants}
    flags.extend(f'--remap-path-prefix={p}=/build' for p in sorted(variants, key=lambda p: (len(p), p)))
    output.pop('RUSTFLAGS', None)
    output['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join(flags)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target')
    args = parser.parse_args()
    command = ['cargo', 'build', '--locked', '--release', '--bin', 'agrep']
    if args.target:
        command.extend(['--target', args.target])
    subprocess.run(command, env=build_environment(Path.cwd(), os.environ), check=True)
