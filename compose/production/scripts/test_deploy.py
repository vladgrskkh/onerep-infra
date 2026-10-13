#!/usr/bin/env python3
"""Ordering tests for deploy and certificate renewal scripts."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "compose" / "production" / "scripts"
DEPLOY = SCRIPTS / "deploy.sh"
RENEW = SCRIPTS / "renew-cert.sh"
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["DOCKER_RECORD"]).open("a") as record:
    record.write(json.dumps(args) + "\n")
joined = " ".join(args)
if "CERTIFICATE_STATE" in joined:
    suffix = "PRESENT" if os.environ.get("TEST_CERT_STATE") == "present" else "MISSING"
    print("CERTIFICATE_STATE_" + suffix)
elif "CERTBOT_EMAIL" in joined:
    print("operator@example.invalid")
elif "sha256sum" in joined:
    hashes = os.environ.get("TEST_CERT_HASHES", "before after").split()
    recorded = Path(os.environ["DOCKER_RECORD"]).read_text().splitlines()
    count = sum("sha256sum" in " ".join(json.loads(line)) for line in recorded)
    print(hashes[min(count - 1, len(hashes) - 1)])
elif "--write-out" in joined:
    print("200")
elif "ps --status running" in joined:
    print("test-nginx-container")
'''


class DeployScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="deploy-script-test-")
        self.directory = Path(self.temp.name)
        self.bin_dir = self.directory / "bin"
        self.bin_dir.mkdir()
        docker = self.bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.record = self.directory / "docker-record.jsonl"
        self.env_file = self.directory / ".env"
        self.secret = "synthetic-deployment-secret-only"
        self.env_file.write_text(f"POSTGRES_ADMIN_PASSWORD={self.secret}\n")
        self.env_file.chmod(0o644)
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "DOCKER_RECORD": str(self.record),
            "TEST_CERT_STATE": "missing",
            "TEST_CERT_HASHES": f"{'a' * 64} {'b' * 64}",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), "--env-file", str(self.env_file), *args],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[str]:
        return [" ".join(json.loads(line)) for line in self.record.read_text().splitlines()]

    def test_first_deployment_bootstraps_http_before_certificate_and_tls(self) -> None:
        result = self.run_script(DEPLOY)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        output = result.stdout + result.stderr
        self.assertTrue(self.secret not in output)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        calls = self.calls()
        bootstrap = next(i for i, call in enumerate(calls) if "up --detach nginx-bootstrap" in call)
        cert = next(i for i, call in enumerate(calls) if "certonly" in call)
        stop = next(i for i, call in enumerate(calls) if "stop nginx-bootstrap" in call)
        production = next(
            i for i, call in enumerate(calls) if call.endswith("up --detach nginx")
        )
        auth_migration = next(i for i, call in enumerate(calls) if call.endswith("run --rm migrate-auth"))
        gym_migration = next(i for i, call in enumerate(calls) if call.endswith("run --rm migrate-gym"))
        auth_check = next(i for i, call in enumerate(calls) if "/v1/.well-known/jwks.json" in call)
        gym_check = next(i for i, call in enumerate(calls) if "http://gym-api:8081/v1/health" in call)
        auth_status = next(i for i, call in enumerate(calls) if "run --rm migrate-auth-status" in call)
        gym_status = next(i for i, call in enumerate(calls) if "run --rm migrate-gym-status" in call)
        public_auth = next(i for i, call in enumerate(calls) if "https://onerep.mooo.com/auth/v1/health" in call)
        public_gym = next(i for i, call in enumerate(calls) if "https://onerep.mooo.com/gym/v1/health" in call)
        self.assertTrue(bootstrap < cert < stop < production)
        self.assertTrue(
            auth_migration < gym_migration < auth_status < gym_status
            < auth_check < gym_check < bootstrap
        )
        self.assertTrue(production < public_auth < public_gym)
        self.assertTrue(all(self.secret not in call for call in calls))

    def test_existing_certificate_skips_bootstrap_and_certonly(self) -> None:
        self.environment["TEST_CERT_STATE"] = "present"
        result = self.run_script(DEPLOY)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        calls = self.calls()
        self.assertFalse(any("nginx-bootstrap" in call for call in calls))
        self.assertFalse(any("certonly" in call for call in calls))
        self.assertTrue(any("up --detach nginx" in call for call in calls))

    def test_certificate_renewal_reloads_only_when_fingerprint_changes(self) -> None:
        changed = self.run_script(RENEW)
        self.assertEqual(changed.returncode, 0, changed.stderr[-500:])
        changed_calls = self.calls()
        before = next(i for i, call in enumerate(changed_calls) if "sha256sum" in call)
        renewal = next(i for i, call in enumerate(changed_calls) if "certbot renew" in call)
        after = next(
            i for i, call in enumerate(changed_calls)
            if i > before and "sha256sum" in call
        )
        reload = next(i for i, call in enumerate(changed_calls) if "nginx -s reload" in call)
        self.assertTrue(before < renewal < after < reload)

        self.record.unlink()
        self.environment["TEST_CERT_HASHES"] = f"{'c' * 64} {'c' * 64}"
        unchanged = self.run_script(RENEW)
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr[-500:])
        calls = self.calls()
        self.assertTrue(any("renew" in call for call in calls))
        self.assertFalse(any("nginx -s reload" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
