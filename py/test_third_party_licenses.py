from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_third_party_licenses_test",
    ROOT / "bench" / "third_party_licenses.py")
third_party = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(third_party)


class ThirdPartyLicensesTests(unittest.TestCase):
    def test_committed_notices_match_the_locked_rust_graph(self):
        self.assertEqual(
            third_party.OUTPUT.read_text(encoding="utf-8"),
            third_party.generate())

    @unittest.skipIf(os.name == "nt", "Windows does not preserve POSIX modes")
    def test_atomic_writer_makes_the_notice_world_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "notices.txt"
            third_party._write_atomic(output, "notice\n")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
