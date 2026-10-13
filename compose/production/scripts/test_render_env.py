#!/usr/bin/env python3
"""Tests for secret-safe Compose environment file rendering."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "compose" / "production" / "scripts" / "render-env.py"


class RenderEnvTests(unittest.TestCase):
    def test_quotes_multiline_values_round_trip_without_output_or_permissive_mode(self) -> None:
        password = "ordinary password with a ' quote"
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "FAKE-KEY-MATERIAL-ONLY\n"
            "-----END PRIVATE KEY-----\n"
        )
        with tempfile.TemporaryDirectory(prefix="render-env-test-") as temporary:
            directory = Path(temporary)
            output = directory / ".env"
            compose_file = directory / "compose.yaml"
            compose_file.write_text(
                "services:\n"
                "  probe:\n"
                "    image: scratch\n"
                "    environment:\n"
                "      POSTGRES_ADMIN_PASSWORD: ${POSTGRES_ADMIN_PASSWORD}\n"
                "      AUTH_JWT_PRIVATE_KEY: ${AUTH_JWT_PRIVATE_KEY}\n"
            )
            environment = {
                **os.environ,
                "POSTGRES_ADMIN_PASSWORD": password,
                "AUTH_JWT_PRIVATE_KEY": private_key,
            }
            rendered = subprocess.run(
                [
                    "python3", str(SCRIPT), "--output", str(output),
                    "--var", "POSTGRES_ADMIN_PASSWORD",
                    "--var", "AUTH_JWT_PRIVATE_KEY",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(rendered.returncode, 0)
            output_text = rendered.stdout + rendered.stderr
            self.assertTrue(password not in output_text and private_key not in output_text)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

            resolved = subprocess.run(
                [
                    "docker", "compose", "--env-file", str(output),
                    "-f", str(compose_file), "config", "--format", "json",
                ],
                cwd=ROOT,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key not in {"POSTGRES_ADMIN_PASSWORD", "AUTH_JWT_PRIVATE_KEY"}
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(resolved.returncode, 0)
            values = json.loads(resolved.stdout)["services"]["probe"]["environment"]
            self.assertTrue(
                values["POSTGRES_ADMIN_PASSWORD"] == password,
                f"password round-trip length mismatch: {len(password)} vs "
                f"{len(values['POSTGRES_ADMIN_PASSWORD'])}",
            )
            self.assertTrue(values["AUTH_JWT_PRIVATE_KEY"] == private_key)
            self.assertTrue(password not in resolved.stderr and private_key not in resolved.stderr)


if __name__ == "__main__":
    unittest.main()
