from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zlib


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_npm_release_test", ROOT / "bench" / "npm_release.py")
npm_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(npm_release)
VERSION = json.loads((ROOT / "npm" / "package.json").read_text(
    encoding="utf-8"))["version"]


class NpmReleaseTests(unittest.TestCase):
    @staticmethod
    def _npm_tar_header(filename: str, data: bytes, mode: int) -> bytes:
        header = bytearray(512)
        name = filename.encode("ascii")
        header[:len(name)] = name
        header[100:108] = f"{mode:06o}".encode("ascii") + b" \0"
        header[124:136] = f"{len(data):010o}".encode("ascii") + b" \0"
        header[136:148] = (
            f"{npm_release.NPM_TAR_MTIME:010o}".encode("ascii") + b" \0")
        header[148:156] = b" " * 8
        header[156:157] = tarfile.REGTYPE
        header[257:265] = b"ustar\0" + b"00"
        header[329:345] = b"000000 \0" * 2
        checksum = sum(header)
        header[148:156] = f"{checksum:06o}".encode("ascii") + b" \0"
        return bytes(header)

    def _tarball(
        self,
        path: Path,
        name: str,
        version: str,
        *,
        package_mutation=None,
        readme: bytes | None = None,
        extra_member: str | None = None,
        pax: bool = False,
    ) -> None:
        files = npm_release._expected_contents(name, version)
        if package_mutation is not None:
            package = json.loads(files["package/package.json"])
            package_mutation(package)
            files["package/package.json"] = (
                json.dumps(package, indent=2) + "\n").encode()
        if readme is not None:
            files["package/README.md"] = readme
        names = list(npm_release.PACKAGE_MEMBERS)
        if extra_member is not None:
            names.append(f"package/{extra_member}")
            files[names[-1]] = b"unexpected\n"

        if pax:
            raw_tar = io.BytesIO()
            with tarfile.open(
                    fileobj=raw_tar, mode="w:",
                    format=tarfile.PAX_FORMAT) as archive:
                for index, filename in enumerate(names):
                    data = files[filename]
                    info = tarfile.TarInfo(filename)
                    info.size = len(data)
                    info.mode = npm_release.PACKAGE_MODES.get(filename, 0o644)
                    info.mtime = npm_release.NPM_TAR_MTIME
                    if index == 0:
                        info.pax_headers = {"comment": "private"}
                    archive.addfile(info, io.BytesIO(data))
            full_tar = raw_tar.getvalue()
            with tarfile.open(fileobj=io.BytesIO(full_tar), mode="r:") as archive:
                members = archive.getmembers()
            last = max(
                member.offset_data + ((member.size + 511) // 512) * 512
                for member in members)
            tar_data = full_tar[:last + 1024]
        else:
            blocks = []
            for filename in names:
                data = files[filename]
                mode = npm_release.PACKAGE_MODES.get(filename, 0o644)
                blocks.extend((
                    self._npm_tar_header(filename, data, mode),
                    data,
                    b"\0" * (-len(data) % 512),
                ))
            tar_data = b"".join(blocks) + b"\0" * 1024
        compressor = zlib.compressobj(
            9, zlib.DEFLATED, -zlib.MAX_WBITS)
        compressed = compressor.compress(tar_data) + compressor.flush()
        gzip_data = (
            struct.pack("<HBBIBB", 0x8B1F, 8, 0, 0, 2, 255)
            + compressed
            + struct.pack(
                "<II", zlib.crc32(tar_data) & 0xFFFF_FFFF,
                len(tar_data) & 0xFFFF_FFFF)
        )
        path.write_bytes(gzip_data)

    def _bundle(self, directory: Path, version: str = VERSION):
        directory.mkdir()
        for name, filename in npm_release.expected_filenames(version).items():
            self._tarball(directory / filename, name, version)
        return npm_release.local_manifest(directory, version)

    def test_local_bundle_validates_both_exact_tarballs(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary) / "dist")
        self.assertEqual(set(manifest), set(npm_release.PACKAGE_NAMES))
        for artifact in manifest.values():
            self.assertTrue(artifact.integrity.startswith("sha512-"))
            self.assertEqual(artifact.version, VERSION)

    def test_real_npm_pack_matches_closed_tar_contract(self):
        node = shutil.which("node")
        npm = shutil.which("npm")
        if node is None or npm is None:
            self.skipTest("node and npm are required for the pack integration")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = dict(os.environ)
            env["npm_config_cache"] = str(root / "npm-cache")
            result = subprocess.run(
                [node, str(ROOT / "npm" / "publish.js"),
                 "--out-dir", str(root / "dist")],
                cwd=ROOT, env=env, capture_output=True, text=True,
                check=False, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = npm_release.local_manifest(root / "dist", VERSION)
        self.assertEqual(set(manifest), set(npm_release.PACKAGE_NAMES))

    def test_partial_release_stages_only_the_missing_tarball(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            local = self._bundle(dist)
            remote = {
                "@mundy/agrep": local["@mundy/agrep"].integrity,
                "agrep-cli": None,
            }
            missing = npm_release.reconcile(
                local, remote, require_complete=False)
            staged = root / "missing"
            npm_release.stage_missing(dist, staged, missing, local)
            self.assertEqual(
                {path.name for path in staged.iterdir()},
                {local["agrep-cli"].filename})

    def test_mismatch_and_incomplete_registry_states_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = self._bundle(Path(temporary) / "dist")
        with self.assertRaisesRegex(RuntimeError, "integrity mismatch"):
            npm_release.reconcile(local, {
                "@mundy/agrep": "sha512-" + "A" * 88,
                "agrep-cli": None,
            }, require_complete=False)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            npm_release.reconcile(local, {
                "@mundy/agrep": local["@mundy/agrep"].integrity,
                "agrep-cli": None,
            }, require_complete=True)

    def test_tarball_identity_lifecycle_and_member_drift_fail_closed(self):
        cases = ("identity", "script", "extra-member")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "dist"
                root.mkdir()
                for name, filename in npm_release.expected_filenames(VERSION).items():
                    mutation = None
                    if name == "agrep-cli" and case == "identity":
                        mutation = lambda package: package.update(name="other")
                    elif name == "agrep-cli" and case == "script":
                        mutation = lambda package: package.update(
                            scripts={"prepublishOnly": "node steal.js"})
                    self._tarball(
                        root / filename, name, VERSION,
                        package_mutation=mutation,
                        extra_member=("secret.txt"
                                      if name == "agrep-cli"
                                      and case == "extra-member" else None))
                with self.assertRaises(RuntimeError):
                    npm_release.local_manifest(root, VERSION)

    def test_rehashed_private_readme_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dist"
            self._bundle(root)
            alias = root / npm_release.expected_filenames(VERSION)["agrep-cli"]
            self._tarball(alias, "agrep-cli", VERSION, readme=b"private release\n")
            with self.assertRaisesRegex(RuntimeError, "README.md"):
                npm_release.local_manifest(root, VERSION)

    def test_gzip_tail_and_pax_metadata_are_rejected(self):
        for case in ("tail", "pax"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "dist"
                self._bundle(root)
                primary = root / npm_release.expected_filenames(VERSION)["@mundy/agrep"]
                if case == "tail":
                    primary.write_bytes(primary.read_bytes() + b"private-tail")
                else:
                    self._tarball(primary, "@mundy/agrep", VERSION, pax=True)
                with self.assertRaises(RuntimeError):
                    npm_release.local_manifest(root, VERSION)

    def test_extra_package_json_field_is_rejected_even_when_rehashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dist"
            self._bundle(root)
            primary = root / npm_release.expected_filenames(VERSION)["@mundy/agrep"]
            self._tarball(
                primary, "@mundy/agrep", VERSION,
                package_mutation=lambda package: package.update(
                    privateBuilder="/Users/private/release"))
            with self.assertRaisesRegex(RuntimeError, "package.json"):
                npm_release.local_manifest(root, VERSION)

    def test_source_package_contract_rejects_new_publish_lifecycle(self):
        package = json.loads((ROOT / "npm" / "package.json").read_text(
            encoding="utf-8"))
        package["scripts"]["prepublishOnly"] = "node private.js"
        with self.assertRaisesRegex(RuntimeError, "scripts contract"):
            npm_release._validate_source_package(package, VERSION)

    def test_remote_manifest_requires_exact_identity_and_integrity(self):
        integrity = "sha512-" + base64.b64encode(b"a" * 64).decode("ascii")
        with mock.patch.object(
                npm_release, "_registry_package",
                side_effect=lambda name, version: {
                    "name": name,
                    "version": version,
                    "dist": {"integrity": integrity},
                }):
            remote = npm_release.remote_manifest(VERSION)
        self.assertEqual(remote, {
            "@mundy/agrep": integrity,
            "agrep-cli": integrity,
        })


if __name__ == "__main__":
    unittest.main()
