"""Distribution identity and ingest-binary resolution.

package_version names the release, distribution_build_id names shipped Python
and browser bytes, and native_binary_build_id is a content-derived identity for
exact Rust binary bytes. Runtime and writer IDs remain lifecycle identities.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from events import DATA_DIR, data_dir_readonly
import fileops
# common.log re-exports this same layer-0 stderr logger.
from hookless._log import log
from proc import WIN

# This file lives in <repo>/py/ (dev) or <site-packages>/agrep/py/ (installed).
PY_DIR = Path(__file__).resolve().parent
REPO_ROOT = PY_DIR.parent


def _is_dev_checkout() -> bool:
    """A real source tree, not an installed wheel: has the rust crate / git dir
    alongside. Used for CLI naming and dev binary discovery, not data placement."""
    return (REPO_ROOT / "Cargo.toml").exists() or (REPO_ROOT / ".git").exists()


def cli_invocation(
        *args: object, environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return the executable argv users can replay from any working directory."""
    env = os.environ if environ is None else environ
    invoked_as = str(env.get("AGREP_CLI_NAME") or "")
    if invoked_as and (
            not invoked_as.isprintable()
            or "\x00" in invoked_as):
        invoked_as = ""
    if invoked_as:
        path_like = (
            os.sep in invoked_as
            or bool(os.altsep and os.altsep in invoked_as)
            or invoked_as.startswith(".")
        )
        if path_like and not os.path.isabs(invoked_as):
            invoked_as = os.path.abspath(invoked_as)
        base = (invoked_as,)
    elif _is_dev_checkout():
        base = (
            os.path.abspath(sys.executable),
            os.path.abspath(REPO_ROOT / "cli.py"),
        )
    else:
        base = ("agrep",)
    return (*base, *(str(arg) for arg in args))


def package_version() -> str:
    """The Python package/release version without importing the CLI tree."""
    if _is_dev_checkout():
        try:
            raw = (REPO_ROOT / "agrep" / "__init__.py").read_text(
                encoding="utf-8")
            match = re.search(r'__version__\s*=\s*["\']([^"\']+)', raw)
            return match.group(1) if match else "dev"
        except Exception:  # noqa: BLE001 -- an unreadable checkout is not installed
            return "dev"
    try:
        from agrep import __version__  # noqa: PLC0415
        return str(__version__)
    except Exception:  # noqa: BLE001 -- a damaged installation is unidentified
        return "dev"


_DISTRIBUTION_MANIFEST_MAX_BYTES = 1024 * 1024
_DISTRIBUTION_MEMBER_MAX_BYTES = 16 * 1024 * 1024
_DISTRIBUTION_TOTAL_MAX_BYTES = 64 * 1024 * 1024
_NATIVE_BINARY_MAX_BYTES = 64 * 1024 * 1024
_NATIVE_IDENTITY_STOP_S = 0.05


def _consume_stable_regular(path: Path, *, max_bytes: int, consume) -> None:
    """Stream one identity-stable regular file without following a link."""
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = fileops.file_identity(path)
        if before[2] > max_bytes:
            raise OSError("not a bounded regular file")
        descriptor = os.open(path, flags)
        try:
            opened = fileops.file_identity_fd(descriptor)
            if opened != before:
                raise OSError("file identity changed before reading")
            size = 0
            while True:
                chunk = os.read(
                    descriptor, min(1024 * 1024, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise OSError("file exceeds the identity bound")
                consume(chunk)
            if fileops.file_identity_fd(descriptor) != opened:
                raise OSError("file changed while reading")
        finally:
            os.close(descriptor)
        if fileops.file_identity(path) != before:
            raise OSError("file path changed while reading")
    except OSError as exc:
        raise RuntimeError(
            f"cannot identify distribution member {path}: {exc}") from exc


def _stable_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read one identity-stable regular file without following a link."""
    chunks: list[bytes] = []
    _consume_stable_regular(path, max_bytes=max_bytes, consume=chunks.append)
    return b"".join(chunks)


def _manifest_relative(value: object, *, prefix: str = "") -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("invalid distribution manifest path")
    if prefix and not value.startswith(prefix):
        raise RuntimeError("distribution manifest member is outside the package")
    relative = value.removeprefix(prefix)
    path = Path(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise RuntimeError("invalid distribution manifest path")
    return path


def distribution_build_id() -> str:
    """Hash every shipped Python, launcher, data, and explorer member."""
    manifest_path = PY_DIR / "runtime_manifest.json"
    raw = _stable_regular_bytes(
        manifest_path, max_bytes=_DISTRIBUTION_MANIFEST_MAX_BYTES)
    try:
        payload = json.loads(raw)
        entries = payload["files"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"invalid distribution manifest {manifest_path}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(entries, list) or not entries:
        raise RuntimeError(f"invalid distribution manifest schema: {manifest_path}")
    dev_checkout = _is_dev_checkout()
    members: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"source", "member"}:
            raise RuntimeError("invalid distribution manifest entry")
        source = _manifest_relative(entry["source"])
        member = _manifest_relative(entry["member"], prefix="agrep/")
        logical = f"agrep/{member.as_posix()}"
        if logical in seen:
            raise RuntimeError("duplicate distribution manifest member")
        seen.add(logical)
        path = REPO_ROOT / source if dev_checkout else REPO_ROOT / member
        members.append((logical, path))
    digest = hashlib.sha256(b"agrep-distribution-runtime-v1\0")
    total = 0
    for logical, path in sorted(members):
        body = _stable_regular_bytes(
            path, max_bytes=_DISTRIBUTION_MEMBER_MAX_BYTES)
        total += len(body)
        if total > _DISTRIBUTION_TOTAL_MAX_BYTES:
            raise RuntimeError("distribution members exceed the identity bound")
        encoded = logical.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "little"))
        digest.update(encoded)
        digest.update(len(body).to_bytes(8, "little"))
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest()[:20]


def native_binary_build_id(path: Path) -> str:
    """Return a content-derived SHA-256 prefix for one resolved native binary."""
    binary = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    _consume_stable_regular(
        binary, max_bytes=_NATIVE_BINARY_MAX_BYTES, consume=digest.update)
    return digest.hexdigest()[:20]


def _native_identity_child(path: str, sender) -> None:
    try:
        payload = ("verified", native_binary_build_id(Path(path)), "")
    except Exception as exc:  # noqa: BLE001 -- child reports a bounded diagnosis
        payload = ("unavailable", "", f"{type(exc).__name__}: {exc}")
    try:
        sender.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        sender.close()


def bounded_native_binary_build_id(path: Path, *, timeout_s: float) -> str:
    """Derive native content identity without exceeding a caller's IO budget."""
    if timeout_s <= 0.0:
        raise TimeoutError("native identity deadline expired before hashing")
    import multiprocessing
    context = multiprocessing.get_context("spawn" if WIN else "fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_native_identity_child,
        args=(os.fspath(path), sender),
        name="agrep-native-identity",
        daemon=True,
    )
    started = False
    payload = None
    try:
        process.start()
        started = True
        sender.close()
        if not receiver.poll(timeout_s):
            raise TimeoutError(
                f"native identity exceeded {timeout_s:.2f}s deadline")
        try:
            payload = receiver.recv()
        except EOFError as exc:
            raise OSError(
                "native identity worker exited without a result") from exc
    finally:
        receiver.close()
        sender.close()
        if started:
            process.join(0)
            if process.is_alive():
                process.terminate()
                process.join(_NATIVE_IDENTITY_STOP_S)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(_NATIVE_IDENTITY_STOP_S)
            if not process.is_alive():
                process.close()
    if (not isinstance(payload, tuple) or len(payload) != 3
            or payload[0] not in {"verified", "unavailable"}):
        raise OSError("native identity worker returned an invalid result")
    if payload[0] != "verified":
        raise OSError(str(payload[2]) or "native identity is unavailable")
    value = str(payload[1])
    if len(value) != 20 or any(char not in "0123456789abcdef" for char in value):
        raise OSError("native identity worker returned an invalid build id")
    return value


def _package_installer() -> str:
    try:
        import importlib.metadata
        value = importlib.metadata.distribution("agrep").read_text("INSTALLER")
    except Exception:  # noqa: BLE001 -- source trees have no distribution metadata
        return ""
    return str(value or "").strip().lower()


def verified_urlopen(url: str, *, timeout: float):
    """urlopen with the strongest CA bundle this install carries.

    Windows boxes routinely fail urllib's default verification while pip on
    the same box succeeds, because pip vendors certifi and the machine's
    store can lack an intermediate (seen on a clean Windows 11 VM: the
    pinned-model fetch died with CERTIFICATE_VERIFY_FAILED). certifi rides
    the semantic extra for exactly this; absent it, the platform default
    decides and the SSL error stays visible and actionable.
    """
    import ssl
    import urllib.request
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    return urllib.request.urlopen(url, timeout=timeout, context=context)


def semantic_install_argv(
        *, installer: str | None = None, executable: str | None = None,
        version: str | None = None, extra: str = "semantic",
) -> tuple[str, ...]:
    """Install an optional extra into the environment that owns this CLI."""
    manager = _package_installer() if installer is None else installer.lower()
    python = sys.executable if executable is None else executable
    release = package_version() if version is None else version
    requirement = (
        f"agrep[{extra}]"
        if release in {"", "dev"} else f"agrep[{extra}]=={release}"
    )
    if manager == "uv":
        return "uv", "pip", "install", "--python", python, requirement
    return python, "-m", "pip", "install", requirement


def semantic_install_command(**values: str | None) -> str | None:
    argv = semantic_install_argv(**values)
    if not WIN:
        return shlex.join(argv)
    if (any(re.search(r"[&|<>^%!`$;'\"(){}@,#\r\n]", value)
            for value in argv[:-1])
            or re.fullmatch(
                r"agrep\[[a-z]+(?:,[a-z]+)*\](?:==[A-Za-z0-9][A-Za-z0-9._+-]*)?",
                argv[-1]) is None):
        return None
    prefix = subprocess.list2cmdline(argv[:-1])
    return f'{prefix} "{argv[-1]}"'


def semantic_install_hint(**values: str | None) -> str:
    extra = values.get("extra") or "semantic"
    command = semantic_install_command(**values)
    if command is not None:
        return f"`{command}`"
    return (f"install agrep[{extra}] into this agrep environment "
            "from a shell-safe path")


def runtime_build_id(*names: str) -> str:
    """Cheap identity for a resident Python component.

    Package version catches normal A->B upgrades.  File size/mtime identities also
    retire a daemon or worker after an editable/source install changes in place.
    This intentionally avoids hashing large modules on every CLI startup.
    """
    import hashlib
    parts = [f"agrep={package_version()}", f"python={sys.version_info[:2]}"]
    for name in names:
        path = PY_DIR / name
        try:
            st = path.stat()
            parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{name}:missing")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:20]


def ingest_bin() -> Path:
    """Path to the Rust ingest binary (agrep-rs), in resolution order:
      1. $AGREP_RS_BIN            (the wheel launcher sets this to the bundled copy)
      2. <package>/_bin/agrep-rs  (binary shipped inside an installed wheel)
      3. <repo>/target/release/   (a dev cargo build)
    The path may not exist yet (a dev checkout before `cargo build`); callers that
    require it already check .exists() and fall back to building."""
    exe = "agrep-rs.exe" if WIN else "agrep-rs"
    env = os.environ.get("AGREP_RS_BIN")
    if env:
        return Path(env)
    dev = REPO_ROOT / "target" / "release" / exe
    if _is_dev_checkout() and dev.exists():
        return dev
    bundled = PY_DIR.parent / "_bin" / exe
    if bundled.exists():
        return bundled
    # a binary fetched on-demand (degraded install: no prebuilt wheel binary, no rust).
    # site-packages is read-only, so the fetch lands in the per-user data dir.
    fetched = FETCHED_BIN_DIR / package_version() / exe
    if fetched.exists():
        return fetched
    return dev


# fetch_binary target: the package _bin is read-only under site-packages.
FETCHED_BIN_DIR = DATA_DIR / "bin"

_LINUX_GLIBC_FLOOR = (2, 28)


def _linux_glibc_version() -> tuple[int, int] | None:
    """Return the host glibc ABI version, or None when it cannot be proven."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        return None
    match = re.fullmatch(r"glibc\s+(\d+)\.(\d+)(?:\.\d+)?", value.strip())
    return ((int(match.group(1)), int(match.group(2)))
            if match is not None else None)


def _linux_raw_binary_compatible() -> bool:
    version = _linux_glibc_version()
    return version is not None and version >= _LINUX_GLIBC_FLOOR


def _platform_arch() -> str | None:
    import platform as platform_module
    machine = platform_module.machine().lower()
    return (
        "x86_64" if machine in ("x86_64", "amd64", "x64")
        else "aarch64" if machine in ("aarch64", "arm64")
        else None)


def _unsupported_asset_message() -> str:
    if sys.platform.startswith("linux") and _platform_arch() is not None:
        version = _linux_glibc_version()
        detected = (f"detected glibc {version[0]}.{version[1]}"
                    if version is not None else
                    "this host is non-glibc or its libc could not be verified")
        return (
            "no compatible prebuilt agrep-rs is available: Linux release binaries "
            f"require glibc 2.28 or newer; {detected}. Refusing the generic binary. "
            "Build agrep-rs from the agrep source with `cargo build --release`, then "
            "set AGREP_RS_BIN to target/release/agrep-rs."
        )
    return ("no prebuilt agrep-rs is published for this platform - build from source "
            "(install Rust: https://rustup.rs, then `agrep index`).")


def _platform_asset() -> str | None:
    """The release-artifact filename of the prebuilt agrep-rs for THIS platform, or None on
    a platform we don't publish for. This is the contract CI must satisfy: publish exactly
    these names on each release. `<os>-<arch>` keeps the scheme obvious and greppable."""
    arch = _platform_arch()
    if arch is None:
        return None
    if WIN:
        return f"agrep-rs-windows-{arch}.exe"
    if sys.platform == "darwin":
        return f"agrep-rs-macos-{arch}"
    if sys.platform.startswith("linux"):
        return f"agrep-rs-linux-{arch}" if _linux_raw_binary_compatible() else None
    return None


def _fetch_base_url() -> str:
    """Where prebuilt binaries live. AGREP_BIN_URL overrides the base (a directory URL, or a
    local file:// dir for testing); default is this version's GitHub release assets."""
    base = os.environ.get("AGREP_BIN_URL")
    if base:
        return base if base.endswith("/") else base + "/"
    return (
        "https://github.com/dannyisbad/agrep/releases/download/"
        f"v{package_version()}/"
    )


def _display_url(url: str) -> str:
    """The fetch URL as safe to print: an AGREP_BIN_URL mirror may embed
    basic-auth credentials, and terminal scrollback / CI logs must never
    receive them."""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url; credentials, if any, redacted>"
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit(parts._replace(netloc=f"***@{host}"))


def _scrub_credentials(text: str, url: str) -> str:
    """Error text with any echo of the credentialed URL replaced."""
    return text.replace(url, _display_url(url))


def fetch_binary(assume_yes: bool = False) -> Path | None:
    """Offer to download the prebuilt agrep-rs for this platform (the degraded-install path:
    no bundled binary, no rust to build one). NEVER silent - prints what/where/why and, unless
    assume_yes, asks for a yes on a TTY (returns None on a non-TTY so the caller can print
    guidance instead of hanging). Verifies size + sha256 (from `<asset>.sha256` beside it when
    published) before installing into the per-user bin dir. Opt out with AGREP_NO_FETCH.
    Returns the installed path, or None (declined / no publish for this platform / failure)."""
    if os.environ.get("AGREP_NO_FETCH"):
        return None
    asset = _platform_asset()
    if asset is None:
        log(_unsupported_asset_message())
        return None
    exe = "agrep-rs.exe" if WIN else "agrep-rs"
    url = _fetch_base_url() + asset
    dest = FETCHED_BIN_DIR / package_version() / exe
    if _protected_binary_destination(dest):
        log("fetch skipped: AGREP_DATA_READONLY protects this data directory.")
        return None
    log(f"agrep needs its ingest binary and none is bundled. It can fetch the prebuilt\n"
        f"  {asset}\nfrom\n  {_display_url(url)}\ninto\n  {dest}\n"
        f"(~3 MB; verified by sha256). Skip with AGREP_NO_FETCH=1 and `agrep doctor` shows "
        f"the manual route.")
    if not assume_yes:
        if not sys.stdin.isatty():
            log("not a terminal - not fetching. Re-run interactively, set AGREP_BIN_URL, "
                "or install Rust and `agrep index`.")
            return None
        try:
            ans = input("fetch it now? [y/N] ").strip().lower()
        except EOFError:
            return None
        if ans not in ("y", "yes"):
            log("skipped. `agrep doctor` shows how to install the binary manually.")
            return None
    return _download_binary(url, dest)


def _protected_binary_destination(dest: Path) -> bool:
    """Whether a fetch would publish beneath the exact protected data dir."""
    if not data_dir_readonly(DATA_DIR):
        return False
    try:
        target = os.path.normcase(os.path.realpath(os.fspath(dest)))
        root = os.path.normcase(os.path.realpath(os.fspath(DATA_DIR)))
        return os.path.commonpath((target, root)) == root
    except (OSError, ValueError):
        return False


def _download_binary(url: str, dest: Path) -> Path | None:
    """Download `url` to `dest`, fail-closed on integrity, then publish atomically.

    The hash sidecar is mandatory unless the caller explicitly opts into an
    unverified custom mirror with AGREP_ALLOW_UNVERIFIED_BINARY=1.  A versioned
    destination prevents an old fetched executable from surviving a package upgrade.
    """
    import hashlib
    import tempfile
    import urllib.request
    if _protected_binary_destination(dest):
        log("fetch skipped: AGREP_DATA_READONLY protects this data directory.")
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        want = None
        try:
            with verified_urlopen(url + ".sha256", timeout=15) as r:
                want = r.read().decode("utf-8", "replace").split()[0].strip().lower()
            if len(want) != 64 or any(c not in "0123456789abcdef" for c in want):
                raise ValueError("invalid sha256 sidecar")
        except Exception as exc:  # noqa: BLE001 -- converted to an explicit policy result
            if os.environ.get("AGREP_ALLOW_UNVERIFIED_BINARY") != "1":
                log(f"fetch failed: no valid sha256 sidecar "
                    f"({_scrub_credentials(str(exc), url)}). Refusing to install an "
                    "unverified executable. Set AGREP_ALLOW_UNVERIFIED_BINARY=1 only for a "
                    "mirror you trust.")
                return None
            log("  ! explicit unsafe override: installing without sha256 verification")
        chunks: list[bytes] = []
        total = 0
        max_bytes = 64 * 1024 * 1024
        with verified_urlopen(url, timeout=60) as r:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    log(f"fetch failed: binary exceeded {max_bytes // (1024 * 1024)} MiB cap.")
                    return None
                chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            log("fetch failed: empty download.")
            return None
        if want:
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                log(f"fetch failed: sha256 mismatch (want {want[:12]}…, got {got[:12]}…).")
                return None
        suffix = ".part.exe" if WIN else ".part"
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if not WIN:
                os.chmod(tmp, 0o755)
            # A hash proves bytes, while this handshake proves the asset belongs to
            # this package release rather than a correctly-hashed wrong-version file.
            check = subprocess.run([tmp, "--version"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=10,
                                   **({"creationflags": subprocess.CREATE_NO_WINDOW}
                                                  if WIN else {}))
            expected = package_version()
            if check.returncode != 0 or expected not in (check.stdout + check.stderr).split():
                log("fetch failed: binary version mismatch "
                    f"(package {expected}, binary said {(check.stdout or check.stderr).strip()!r}).")
                return None
            # macOS: urllib downloads skip com.apple.quarantine, so no Gatekeeper prompt.
            # A "cannot be opened" report implicates this (fix: sign+notarize).
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        log(f"installed {dest} ({len(data):,} bytes).")
        return dest
    except Exception as e:  # noqa: BLE001 -- any network/IO failure -> degrade to guidance
        log(f"fetch failed: {_scrub_credentials(str(e), url)}. "
            "`agrep doctor` shows the manual route.")
        return None
