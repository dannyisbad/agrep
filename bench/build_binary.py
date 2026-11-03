"""Build a release binary with source/build paths remapped on every target."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tomllib


def host_target():
    output = subprocess.run(['rustc', '-vV'], capture_output=True, text=True, check=True).stdout
    return next(line.split(': ', 1)[1] for line in output.splitlines() if line.startswith('host: '))


def config_rustflags(root, target):
    config = Path(root) / '.cargo' / 'config.toml'
    if not config.is_file():
        return []
    with config.open('rb') as handle:
        return list(tomllib.load(handle).get('target', {}).get(target, {}).get('rustflags', []))


def build_environment(root, env, target):
    output = dict(env)
    flags = output.get('CARGO_ENCODED_RUSTFLAGS')
    flags = flags.split('\x1f') if flags is not None else shlex.split(output.get('RUSTFLAGS', ''))
    # Rustflags from the environment replace the config file's, so carry the target's over.
    flags = config_rustflags(root, target) + flags
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
    target = args.target or host_target()
    subprocess.run(command, env=build_environment(Path.cwd(), os.environ, target), check=True)
