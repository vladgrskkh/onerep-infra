#!/usr/bin/env python3
"""Host-only regression tests for restore-check setup cleanup."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "compose" / "production" / "scripts" / "restore-check.sh"


class RestoreCheckSetupTests(unittest.TestCase):
    def test_openssl_failure_removes_restore_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="restore-check-test-") as temporary:
            directory = Path(temporary)
            temp_root = directory / "tmp"
            bin_dir = directory / "bin"
            temp_root.mkdir()
            bin_dir.mkdir()
            fake_openssl = bin_dir / "openssl"
            openssl_called = directory / "openssl-called"
            fake_openssl.write_text("#!/bin/sh\n: > \"$OPENSSL_CALLED\"\nexit 42\n")
            fake_openssl.chmod(0o755)

            result = subprocess.run(
                [str(SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMPDIR": str(temp_root),
                    "OPENSSL_CALLED": str(openssl_called),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(openssl_called.is_file())
            self.assertEqual(list(temp_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
