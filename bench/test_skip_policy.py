import json
from pathlib import Path
import tempfile
import unittest
from check_skips import validate


class SkipPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ('bench', 'crates', 'npm'):
            (self.root / folder).mkdir()
        self.policy = {'platform_inapplicable': [], 'partial_assertions': []}
        self.save()

    def save(self):
        (self.root / 'bench/test-skips.json').write_text(json.dumps(self.policy))

    def reject_rust(self, source):
        (self.root / 'crates/probe.rs').write_text(source)
        with self.assertRaises(ValueError):
            validate([], self.root)

    def test_body_gates_and_early_returns_need_exact_registration(self):
        for source in (
            '#[test] fn probe() { #[cfg(unix)] { assert!(true); } }',
            '#[test] fn probe() { if cfg!(windows) { return; } assert!(true); }',
            '#[test] fn probe() { return; }',
            '#[cfg(unix)] /* keep this gate */ #[test] fn probe() {}',
        ):
            self.reject_rust(source)
        (self.root / 'crates/probe.rs').write_text('#[test] fn probe() { #[cfg(unix)] { assert!(true); } }')
        self.policy['partial_assertions'] = [{'test': 'crates/probe.rs::probe', 'gates': ['cfg(unix)'], 'reason': 'POSIX assertion'}]
        self.save()
        validate([], self.root)
        self.policy['partial_assertions'][0]['gates'] = ['cfg(windows)']
        self.save()
        with self.assertRaises(ValueError):
            validate([], self.root)

    def test_inner_and_out_of_line_module_gates_are_inherited(self):
        for header in ('#![cfg(unix)]\n', '#[cfg(unix)] mod child;\n', '#[cfg(unix)] pub(crate) mod child;\n'):
            (self.root / 'crates/probe.rs').write_text(header + ('#[test] fn probe() {}' if header.startswith('#!') else ''))
            (self.root / 'crates/probe').mkdir(exist_ok=True)
            (self.root / 'crates/probe/child.rs').write_text('#[test] fn child_test() {}')
            with self.assertRaises(ValueError):
                validate([], self.root)

    def test_only_registered_initial_metal_capability_guard_is_accepted(self):
        guard = 'let Some(device) = crate::semantic_device::metal_device() else { return; };'
        source = '#[cfg(target_os="macos")] #[test] fn probe() { ' + guard + ' assert!(device.is_metal()); }'
        self.reject_rust(source)
        self.policy['platform_inapplicable'] = [{
            'test': 'crates/probe.rs::probe',
            'gates': ['cfg(target_os=LITERAL)', 'capability(semantic_device::metal_device)'],
            'reason': 'The hosted platform has no GPU with the required features.',
        }]
        self.save()
        validate([], self.root)
        for altered in (
            source.replace('metal_device()', 'other_device()'),
            source.replace(guard, 'if crate::semantic_device::metal_device().is_none() { return; }'),
            source.replace(guard, guard + ' if true { return; }'),
            source.replace(guard, 'assert!(true); ' + guard),
            source.replace('else { return; }', 'else { let ignored = 1; return; }'),
            source.replace('target_os="macos"', 'target_os="windows"').replace(guard, ''),
        ):
            self.reject_rust(altered)
        self.policy['platform_inapplicable'][0]['gates'] = ['cfg(target_os=LITERAL)']
        self.save()
        self.reject_rust(source)

    def test_todo_skip_and_expected_failure_source_markers(self):
        for source in ('test.todo("later");', 'test("later", {todo:true}, () => {});', 'test("later", t => t.skip());', 'test("later", {skip:"reason"}, () => {});', 'test("later", {"todo":true}, () => {});'):
            (self.root / 'npm/test_probe.js').write_text(source)
            with self.assertRaises(ValueError):
                validate([], self.root)
        (self.root / 'npm/test_probe.js').unlink()
        for source in (
            'import unittest\n@unittest.expectedFailure\ndef test_probe(): pass\n',
            'from unittest import expectedFailure as xfail\n@xfail\ndef test_probe(): pass\n',
        ):
            (self.root / 'bench/test_probe.py').write_text(source)
            with self.assertRaises(ValueError):
                validate([], self.root)

    def test_todo_and_expected_failures_in_logs(self):
        path = self.root / 'test.log'
        for text in ('ℹ todo 1\n', 'OK (expected failures=1)\n'):
            path.write_text(text)
            with self.assertRaises(ValueError):
                validate([path], self.root)
