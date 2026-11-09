import re
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class SecurityContracts(unittest.TestCase):
    def test_actions_are_pinned_and_tag_is_never_shell_source(self):
        for name in ('ci.yml','release.yml'):
            text=(ROOT/'.github/workflows'/name).read_text(encoding='utf-8')
            actions=re.findall(r'^\s*uses:\s*(\S+)|^\s*-\s*uses:\s*(\S+)',text,re.M)
            for groups in actions:
                action=next(part for part in groups if part)
                if action.startswith('./'): continue
                self.assertRegex(action,r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')
        release=(ROOT/'.github/workflows/release.yml').read_text(encoding='utf-8')
        for line in release.splitlines():
            if 'github.ref_name' in line:
                self.assertRegex(line.strip(),r'^(run-name:|RELEASE_TAG:)')
        self.assertIn('--tag "$RELEASE_TAG"',release)
        self.assertIn('gh release edit "$RELEASE_TAG"',release)
