"""Background embed children must write datable log lines (RC2 M10).

semantic-embed.log is the only forensic record of embed failures, and its
lines carried no timestamps, so "when did the last build fail" was
unanswerable. Parents set common.LOG_STAMP_ENV on background spawns; the
child stamps its own stdout/stderr line starts. Manual runs and test
captures stay byte-identical because the wrapper only installs when asked.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ")


class LineStampWriterTests(unittest.TestCase):
    def test_each_line_start_is_stamped_once(self) -> None:
        raw = io.StringIO()
        writer = common._LineStampWriter(raw)
        writer.write("embed done | count=5\n")
        writer.write("embed phases | plan=0.1s\n")
        lines = raw.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, STAMP)
        self.assertEqual(sum(line.count("T") for line in lines), 2)

    def test_partial_line_chunks_stamp_only_at_line_starts(self) -> None:
        """Tracebacks arrive as many small writes, often not line-aligned."""
        raw = io.StringIO()
        writer = common._LineStampWriter(raw)
        writer.write("Traceback (most recent")
        writer.write(" call last):\n  File")
        writer.write(" \"x.py\", line 1\n")
        lines = raw.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, STAMP)
        # no stamp mid-line where the chunk boundary fell
        self.assertIn("Traceback (most recent call last):", lines[0])

    def test_passthrough_of_raw_stream_attributes(self) -> None:
        raw = io.StringIO()
        writer = common._LineStampWriter(raw)
        writer.flush()
        self.assertIs(writer.getvalue.__self__, raw)


class StampStdioLinesTests(unittest.TestCase):
    def _child(self, env_value: str | None) -> str:
        env = {**os.environ}
        env.pop(common.LOG_STAMP_ENV, None)
        if env_value is not None:
            env[common.LOG_STAMP_ENV] = env_value
        code = (
            "import sys; sys.path.insert(0, {py!r})\n"
            "import common\n"
            "common.stamp_stdio_lines()\n"
            "print('embed done | count=1')\n"
            "print('boom', file=sys.stderr)\n"
        ).format(py=str(Path(__file__).resolve().parent))
        proc = subprocess.run(
            [sys.executable, "-c", code], env=env,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout + proc.stderr

    def test_stamps_when_parent_asked(self) -> None:
        out = self._child("1")
        for line in out.splitlines():
            self.assertRegex(line, STAMP)

    def test_byte_identical_without_the_env(self) -> None:
        out = self._child(None)
        self.assertEqual(out.splitlines(), ["embed done | count=1", "boom"])

    def test_background_spawn_sites_set_the_env(self) -> None:
        """The three semantic.py spawns carry the stamp env to the child."""
        source = (Path(__file__).resolve().parent / "semantic.py").read_text(
            encoding="utf-8")
        self.assertEqual(source.count("common.LOG_STAMP_ENV"), 3)


if __name__ == "__main__":
    unittest.main()
