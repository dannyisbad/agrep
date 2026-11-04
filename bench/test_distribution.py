import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from release_identity import ROOT, validate as identity, version
from validate_wheel import validate as validate_wheel
from validate_binary_privacy import validate_bytes, InvalidBinary
from validate_repo_privacy import _content_reasons
from validate_manylinux import validate as manylinux, InvalidPolicy
from check_skips import validate as check_skips, gated_tests
from prepare_release import prepare
from binary_format import validate_target
from build_binary import build_environment

sys.path.insert(0, str(ROOT))
import wheel_build

TARGETS = json.loads((ROOT / 'bench/targets.json').read_text(encoding='utf-8'))


def executable_header(target):
    raw = bytearray(128)
    if target['os'] == 'linux':
        raw[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', raw, 18, 62 if target['arch'] == 'x64' else 183)
    elif target['os'] == 'darwin':
        raw[:4] = b'\xcf\xfa\xed\xfe'
        struct.pack_into('<I', raw, 4, 0x1000007 if target['arch'] == 'x64' else 0x100000c)
    else:
        raw[:2] = b'MZ'
        struct.pack_into('<I', raw, 60, 64)
        raw[64:68] = b'PE\0\0'
        struct.pack_into('<H', raw, 68, 0x8664 if target['arch'] == 'x64' else 0xaa64)
    return bytes(raw)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agrep-dist-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, target):
        binary = self.root / 'binary'
        binary.write_bytes(executable_header(target))
        with patch.dict(os.environ, AGREP_WHEEL_PLAT=target['platform'], AGREP_WHEEL_BINARY=str(binary)):
            filename = wheel_build.build_wheel(self.root / 'wheels')
        return self.root / 'wheels' / filename

    def test_all_six_wheels_are_binary_only_and_have_valid_records(self):
        for target in TARGETS:
            with self.subTest(target=target['target']):
                path = self.build(target)
                self.assertEqual(validate_wheel(path), target)
                identity(wheels=[path])

    def test_build_refuses_missing_binary_wrong_arch_and_sdist(self):
        target = TARGETS[0]
        with patch.dict(os.environ, AGREP_WHEEL_PLAT=target['platform'], AGREP_WHEEL_BINARY=str(self.root / 'missing')):
            with self.assertRaisesRegex(RuntimeError, 'stage the release binary'):
                wheel_build.build_wheel(self.root)
        with self.assertRaises(ValueError):
            validate_target(executable_header(TARGETS[1]), target)
        with self.assertRaisesRegex(RuntimeError, 'wheels only'):
            wheel_build.build_sdist(self.root)

    def test_wheel_rejects_extra_files_and_modified_record(self):
        path = self.build(TARGETS[0])
        with zipfile.ZipFile(path, 'a') as wheel:
            wheel.writestr('agrep/unexpected.py', 'bad')
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            validate_wheel(path)
        path = self.build(TARGETS[0])
        with zipfile.ZipFile(path) as wheel:
            contents = {n: (wheel.getinfo(n), wheel.read(n)) for n in wheel.namelist()}
        with zipfile.ZipFile(path, 'w') as wheel:
            for name, (info, raw) in contents.items():
                wheel.writestr(info, raw + b'x' if name == 'agrep/agrep' else raw)
        with self.assertRaisesRegex(ValueError, 'RECORD mismatch'):
            validate_wheel(path)

    def test_version_and_tag_mismatch_fail(self):
        self.assertEqual(identity(tag='v' + version()), version())
        with self.assertRaisesRegex(ValueError, 'does not match'):
            identity(tag='v0.0.0')
        for name in ('Cargo.toml', 'pyproject.toml', 'npm/package.json'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        package = self.root / 'npm/package.json'
        data = json.loads(package.read_text(encoding='utf-8')); data['version'] = '0.0.0'
        package.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            identity(root=self.root)

    def test_manylinux_requires_proven_matching_policy(self):
        report = 'is consistent with the following platform tag: "manylinux_2_17_x86_64".'
        self.assertEqual(manylinux(report, 'manylinux_2_28_x86_64'), 'manylinux_2_17_x86_64')
        for invalid in ('', report.replace('2_17', '2_34'), report.replace('x86_64', 'aarch64')):
            with self.assertRaises(InvalidPolicy):
                manylinux(invalid, 'manylinux_2_28_x86_64')

    def test_build_remaps_spaces_without_losing_existing_flags(self):
        env = build_environment(self.root / 'source with spaces', {
            'RUSTFLAGS': '-C opt-level=3', 'CARGO_HOME': '/home/private-builder/cargo home'
        }, 'x86_64-unknown-linux-gnu')
        flags = env['CARGO_ENCODED_RUSTFLAGS'].split('\x1f')
        self.assertEqual(flags[:2], ['-C', 'opt-level=3'])
        self.assertIn('--remap-path-prefix=/home/private-builder/cargo home=/build', flags)
        self.assertNotIn('RUSTFLAGS', env)

    def test_build_keeps_the_targets_config_rustflags(self):
        (self.root / '.cargo').mkdir()
        shutil.copy2(ROOT / '.cargo/config.toml', self.root / '.cargo/config.toml')
        arm = build_environment(self.root, {'RUSTFLAGS': '-C opt-level=3'},
                                'aarch64-unknown-linux-gnu')['CARGO_ENCODED_RUSTFLAGS'].split('\x1f')
        self.assertEqual(arm[:4], ['-C', 'target-feature=+fp16', '-C', 'opt-level=3'])
        mac = build_environment(self.root, {}, 'aarch64-apple-darwin')['CARGO_ENCODED_RUSTFLAGS']
        self.assertNotIn('fp16', mac)

    def test_privacy_rejects_builder_paths_and_secrets(self):
        with self.assertRaises(InvalidBinary):
            validate_bytes(b'prefix /home/private-builder/project/src/main.rs')
        with self.assertRaises(InvalidBinary):
            validate_bytes(b'C:\\Users\\private-builder\\project\\main.rs')
        self.assertTrue(_content_reasons(b'gh' + b'p_' + b'x' * 36))
        validate_bytes(b'/agrep/src/main.rs /cargo/src/dependency.rs')

    def test_skip_gate_rejects_positive_ignored_counts(self):
        path = self.root / 'test.log'
        path.write_text('test result: ok. 2 passed; 0 ignored; 0 failed\n', encoding='utf-8')
        check_skips([path])
        for text in ('test result: ok. 2 passed; 1 ignored; 0 failed\n', 'ℹ skipped 1', 'ok 1 # SKIP unsupported', 'OK (skipped=1)'):
            path.write_text(text, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'skipped'):
                check_skips([path])

    def test_skip_registry_requires_each_test_in_platform_modules(self):
        text = '#[cfg(unix)] mod pty { #[test] fn suspend() {} #[test] fn restore() {} } #[cfg(windows)] #[test] fn console() {}'
        self.assertEqual(gated_tests(text), {'pty::suspend':['cfg(unix)'], 'pty::restore':['cfg(unix)'], 'console':['cfg(windows)']})
        source = self.root / 'crates/probe.rs'; source.parent.mkdir()
        source.write_text(text, encoding='utf-8')
        policy = self.root / 'bench/test-skips.json'; policy.parent.mkdir()
        policy.write_text(json.dumps({'platform_inapplicable':[]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unlisted platform'):
            check_skips([],root=self.root)
        policy.write_text(json.dumps({'platform_inapplicable':[{'test':'crates/probe.rs::'+name,'gates':gates,'reason':'platform API'} for name,gates in gated_tests(text).items()]}), encoding='utf-8')
        check_skips([],root=self.root)

    def test_semantic_smoke_requires_correct_first_neighbor(self):
        from smoke import assert_semantic_ranking, SESSION
        good = json.dumps({'session':SESSION,'sem_score':0.9})
        assert_semantic_ranking(good)
        with self.assertRaises(AssertionError):
            assert_semantic_ranking(json.dumps({'session':'distractor-0','sem_score':0.9})+'\n'+good)
        with self.assertRaises(AssertionError):
            assert_semantic_ranking(json.dumps({'session':SESSION}))

    def test_release_stays_prerelease_until_publish_gates(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn('prerelease: true',workflow)
        self.assertIn('make_latest: false',workflow)
        promote = workflow.split('  promote:')[1]
        self.assertIn('needs: [pypi, npm]',promote)
        self.assertIn('--prerelease=false --latest',promote)

    def test_release_requires_six_assets_and_wheels_then_pins_their_hashes(self):
        assets = self.root / 'assets'; assets.mkdir()
        wheels = self.root / 'wheels'; wheels.mkdir()
        with self.assertRaisesRegex(ValueError, 'six target binaries'):
            prepare(assets, wheels, self.root / 'sealed')
        for target in TARGETS:
            (assets / target['asset']).write_bytes(executable_header(target))
            self.build(target)
        prepare(assets, wheels, self.root / 'sealed')
        from validate_npm import validate as npm_validate
        tarball = next((self.root / 'sealed/npm').glob('*.tgz'))
        npm_validate(tarball, assets)
        (assets / TARGETS[0]['asset']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hashes'):
            npm_validate(tarball, assets)
        manifest = json.loads((self.root / 'sealed/npm/checksums.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest), 6)
        for target in TARGETS:
            entry = manifest[target['os'] + '-' + target['arch']]
            import hashlib
            self.assertEqual(entry['sha256'], hashlib.sha256(executable_header(target)).hexdigest())
            self.assertTrue((self.root / 'sealed/assets' / (target['asset'] + '.sha256')).is_file())


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='agrep-launcher-test-')
        cls.root = Path(cls.temp.name)
        cls.exe = cls.root / ('probe.exe' if os.name == 'nt' else 'probe')
        source = cls.root / 'probe.rs'
        source.write_text('''use std::{env,io::{self,Read,Write}};
        fn main() { let a:Vec<String>=env::args().skip(1).collect();
         if a.first().map(String::as_str)==Some("wait") {println!("ready");io::stdout().flush().unwrap();io::stdin().read_to_end(&mut Vec::new()).unwrap();}
         if a.first().map(String::as_str)==Some("exit") {std::process::exit(a[1].parse().unwrap());}
         for s in a {println!("{}:{}",s.len(),s);} eprintln!("stderr intact");
        }''', encoding='utf-8')
        subprocess.run(['rustc', str(source), '-o', str(cls.exe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        package = self.work / 'agrep'; package.mkdir()
        shutil.copy2(ROOT / 'packaging/agrep/__init__.py', package / '__init__.py')
        shutil.copy2(self.exe, package / ('agrep.exe' if os.name == 'nt' else 'agrep'))
        self.env = {**os.environ, 'PYTHONPATH': str(self.work)}
        self.command = [sys.executable, '-c', 'import agrep; agrep.main()']

    def test_python_launcher_preserves_argv_stdio_exit_and_posix_signal(self):
        result = subprocess.run([*self.command, 'spaces and $quotes', 'é'], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'18:spaces and $quotes\n2:\xc3\xa9\n')
        self.assertEqual(result.stderr, b'stderr intact\n')
        result = subprocess.run([*self.command, 'exit', '23'], env=self.env)
        self.assertEqual(result.returncode, 23)
        if os.name == 'posix':
            with subprocess.Popen([*self.command, 'wait'], env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
                self.assertEqual(child.stdout.readline(), b'ready\n')
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)

    def test_npm_launcher_preserves_argv_stdio_exit_and_posix_signal(self):
        import hashlib
        npm = self.work / 'npm'; shutil.copytree(ROOT / 'npm', npm)
        platform = {'darwin': 'darwin', 'win32': 'win32'}.get(sys.platform, 'linux')
        arch = subprocess.check_output(['node', '-p', 'process.arch'], text=True).strip()
        digest = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        (npm / 'checksums.json').write_text(json.dumps({platform + '-' + arch: {'asset': 'agrep-test', 'sha256': digest}}), encoding='utf-8')
        cache = self.work / 'cache'
        binary = cache / 'agrep/bin' / version() / (platform + '-' + arch) / digest / ('agrep.exe' if os.name == 'nt' else 'agrep')
        binary.parent.mkdir(parents=True); shutil.copy2(self.exe, binary)
        env = {**os.environ, 'XDG_CACHE_HOME': str(cache), 'LOCALAPPDATA': str(cache)}
        command = ['node', str(npm / 'bin.js')]
        result = subprocess.run([*command, 'spaces and $quotes', 'é'], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'18:spaces and $quotes\n2:\xc3\xa9\n')
        self.assertEqual(result.stderr, b'stderr intact\n')
        self.assertEqual(subprocess.run([*command, 'exit', '23'], env=env).returncode, 23)
        if os.name == 'posix':
            with subprocess.Popen([*command, 'wait'], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
                self.assertEqual(child.stdout.readline(), b'ready\n')
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
