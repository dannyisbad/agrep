"""Execute the bundled agrep binary; all product behavior lives in Rust."""
import os
from pathlib import Path
import signal
import subprocess
import sys


def main():
    binary = str(Path(__file__).with_name("agrep.exe" if os.name == "nt" else "agrep"))
    argv = [binary, *sys.argv[1:]]
    if os.name != "nt":
        os.execv(binary, argv)
    # The child shares the console and receives its console control events.
    # Keep the launcher alive until it can return the child's exact exit code.
    child = subprocess.Popen(argv)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    raise SystemExit(child.wait())
