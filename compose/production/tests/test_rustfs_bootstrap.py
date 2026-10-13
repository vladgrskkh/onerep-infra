#!/usr/bin/env python3
"""Behavior and disposable integration tests for RustFS IAM bootstrap."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PRODUCTION = ROOT / "compose" / "production"
SCRIPT = PRODUCTION / "scripts" / "bootstrap-rustfs.sh"
POLICIES = PRODUCTION / "rustfs"
API_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:CreateBucket"],
            "Resource": ["arn:aws:s3:::gym-media"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": ["arn:aws:s3:::gym-media/*"],
        },
    ],
}
BACKUP_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": ["arn:aws:s3:::gym-media"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": ["arn:aws:s3:::gym-media/*"],
        },
    ],
}


class BootstrapBehaviorTests(unittest.TestCase):
    """Fast tests use a stateful rc stand-in to verify safe script behavior."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="rustfs-bootstrap-unit-")
        self.directory = Path(self.temp.name)
        self.state = self.directory / "state.json"
        self.rc = self.directory / "rc"
        self.rc.write_text(FAKE_RC)
        self.rc.chmod(0o755)
        self.api_secret = "api-secret-test-value-0001"
        self.backup_secret = "backup-secret-test-value-0001"
        self.env = {
            **os.environ,
            "PATH": f"{self.directory}:{os.environ['PATH']}",
            "RC_STATE_FILE": str(self.state),
            "RUSTFS_ENDPOINT": "http://rustfs:9000",
            "RUSTFS_ACCESS_KEY": "TESTROOTACCESSKEY0001",
            "RUSTFS_SECRET_KEY": "root-secret-test-value-0001",
            "S3_ACCESS_KEY": "gym-api",
            "S3_SECRET_KEY": self.api_secret,
            "S3_BACKUP_ACCESS_KEY": "gym-backup",
            "S3_BACKUP_SECRET_KEY": self.backup_secret,
            "S3_BUCKET": "gym-media",
            "AWS_DEFAULT_REGION": "us-east-1",
            "S3_API_POLICY_FILE": str(POLICIES / "gym-api-policy.json"),
            "S3_BACKUP_POLICY_FILE": str(POLICIES / "gym-backup-policy.json"),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_bootstrap(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)],
            cwd=ROOT,
            env=env or self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_bootstrap_is_idempotent_and_never_prints_credentials(self) -> None:
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_bootstrap()
        self.assertEqual(second.returncode, 0, second.stderr)
        output = first.stdout + first.stderr + second.stdout + second.stderr
        for secret in (self.api_secret, self.backup_secret, self.env["RUSTFS_SECRET_KEY"]):
            self.assertNotIn(secret, output)

        state = json.loads(self.state.read_text())
        self.assertEqual(state["bucket"], "gym-media")
        self.assertEqual(state["users"]["gym-api"]["secret"], self.api_secret)
        self.assertEqual(state["users"]["gym-api"]["policy"], "gym-api-policy")
        self.assertEqual(state["users"]["gym-backup"]["secret"], self.backup_secret)
        self.assertEqual(state["users"]["gym-backup"]["policy"], "gym-backup-policy")
        self.assertEqual(state["policies"]["gym-api-policy"], API_POLICY)
        self.assertEqual(state["policies"]["gym-backup-policy"], BACKUP_POLICY)

    def test_credential_drift_fails_without_replacing_existing_user(self) -> None:
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        state = json.loads(self.state.read_text())
        state["users"]["gym-api"]["policy"] = None
        self.state.write_text(json.dumps(state))
        env = {**self.env, "S3_SECRET_KEY": "different-api-secret-test-0001"}

        drift = self.run_bootstrap(env)

        self.assertNotEqual(drift.returncode, 0)
        self.assertRegex((drift.stdout + drift.stderr).lower(), r"rotat(e|ion)")
        state = json.loads(self.state.read_text())
        self.assertEqual(state["users"]["gym-api"]["secret"], self.api_secret)
        self.assertIsNone(state["users"]["gym-api"]["policy"])

    def test_policy_drift_fails_without_replacing_existing_policy(self) -> None:
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        changed = self.directory / "changed-policy.json"
        changed.write_text(json.dumps({"Version": "2012-10-17", "Statement": []}))
        env = {**self.env, "S3_API_POLICY_FILE": str(changed)}

        drift = self.run_bootstrap(env)

        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("policy", (drift.stdout + drift.stderr).lower())
        state = json.loads(self.state.read_text())
        self.assertEqual(state["policies"]["gym-api-policy"], API_POLICY)


class DisposableComposeIntegrationTests(unittest.TestCase):
    """Exercise the pinned RustFS/rc images using a unique disposable project."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("Docker is unavailable")
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, check=False
        )
        if probe.returncode != 0:
            raise unittest.SkipTest("Docker Compose is unavailable")

    def test_real_rustfs_bootstrap_twice_and_rejects_credential_and_policy_drift(self) -> None:
        project = f"rtb-{uuid.uuid4().hex[:16]}"
        self.assertRegex(project, r"^rtb-[0-9a-f]{16}$")
        with tempfile.TemporaryDirectory(prefix=f"{project}-") as temp:
            temp_path = Path(temp)
            env_file = temp_path / ".env.test"
            api_secret = f"api-{uuid.uuid4().hex}-secret"
            backup_secret = f"backup-{uuid.uuid4().hex}-secret"
            root_secret = f"{uuid.uuid4().hex}{uuid.uuid4().hex}"
            env_file.write_text(
                "\n".join(
                    [
                        "AUTH_IMAGE_TAG=disposable-test",
                        "GYM_API_IMAGE_TAG=disposable-test",
                        "POSTGRES_ADMIN_PASSWORD=disposable-admin-password",
                        "AUTH_DATABASE_PASSWORD=disposable-auth-password",
                        "GYM_DATABASE_PASSWORD=disposable-gym-password",
                        "REDIS_PASSWORD=disposable-redis-password",
                        "AUTH_JWT_PRIVATE_KEY=disposable-jwt-key",
                        "CERTBOT_EMAIL=unused@example.invalid",
                        "RUSTFS_ACCESS_KEY=TESTROOTACCESSKEY0001",
                        f"RUSTFS_SECRET_KEY={root_secret}",
                        "S3_ACCESS_KEY=gym-api",
                        f"S3_SECRET_KEY={api_secret}",
                        "S3_BACKUP_ACCESS_KEY=gym-backup",
                        f"S3_BACKUP_SECRET_KEY={backup_secret}",
                        "S3_BUCKET=gym-media",
                        "S3_ENDPOINT=http://rustfs:9000",
                        "S3_PUBLIC_ENDPOINT=https://onerep.mooo.com",
                        "AUTH_BASE_URL=http://auth:8080",
                        "AWS_DEFAULT_REGION=us-east-1",
                        "RESTIC_REPOSITORY=s3:https://example.invalid/restic",
                        "RESTIC_PASSWORD=disposable-restic-password",
                        "RESTIC_S3_ACCESS_KEY=disposable-offsite-access",
                        "RESTIC_S3_SECRET_KEY=disposable-offsite-secret",
                    ]
                )
                + "\n"
            )
            compose = [
                "docker",
                "compose",
                "--project-name",
                project,
                "--env-file",
                str(env_file),
                "-f",
                str(PRODUCTION / "compose.yaml"),
            ]
            self._run([*compose, "up", "-d", "rustfs"])
            try:
                first = self._run([*compose, "run", "--rm", "rustfs-bootstrap"])
                second = self._run([*compose, "run", "--rm", "rustfs-bootstrap"])
                secrets = (api_secret, backup_secret, root_secret)
                for secret in secrets:
                    self.assertNotIn(secret, first.stdout + first.stderr + second.stdout + second.stderr)

                credential_drift = self._run(
                    [*compose, "run", "--rm", "-e", "S3_SECRET_KEY=changed-disposable-secret", "rustfs-bootstrap"],
                    expect_success=False,
                )
                self.assertRegex(
                    credential_drift.stdout.lower() + credential_drift.stderr.lower(), r"rotat(e|ion)"
                )
                for secret in secrets:
                    self.assertNotIn(secret, credential_drift.stdout + credential_drift.stderr)
                restored = self._run([*compose, "run", "--rm", "rustfs-bootstrap"])
                for secret in secrets:
                    self.assertNotIn(secret, restored.stdout + restored.stderr)

                for policy_name, expected in (
                    ("gym-api-policy", API_POLICY),
                    ("gym-backup-policy", BACKUP_POLICY),
                ):
                    result = self._run(
                        [
                            *compose,
                            "run",
                            "--rm",
                            "--entrypoint",
                            "/bin/sh",
                            "rustfs-bootstrap",
                            "-ec",
                            f'export RC_HOST_root="http://$RUSTFS_ACCESS_KEY:$RUSTFS_SECRET_KEY@rustfs:9000"; exec rc --json admin policy info root {policy_name}',
                        ]
                    )
                    result_policy = json.loads(result.stdout)["policy"]
                    self.assertEqual(result_policy.get("policy", result_policy), expected)

                backup_write = self._run(
                    [
                        *compose,
                        "run",
                        "--rm",
                        "--entrypoint",
                        "/bin/sh",
                        "rustfs-bootstrap",
                        "-ec",
                        'printf probe >/tmp/probe; export RC_HOST_backup="http://$S3_BACKUP_ACCESS_KEY:$S3_BACKUP_SECRET_KEY@rustfs:9000"; rc cp --quiet /tmp/probe backup/gym-media/denied-write',
                    ],
                    expect_success=False,
                )
                self.assertNotIn(api_secret, backup_write.stdout + backup_write.stderr)

                with tempfile.TemporaryDirectory(prefix="rustfs-policy-") as policy_dir:
                    changed_file = Path(policy_dir) / "gym-api-policy.json"
                    changed_file.write_text('{"Version":"2012-10-17","Statement":[]}\n')
                    policy_drift = self._run(
                        [
                            *compose,
                            "run",
                            "--rm",
                            "-v",
                            f"{changed_file}:/policies/gym-api-policy.json:ro",
                            "rustfs-bootstrap",
                        ],
                        expect_success=False,
                    )
                    self.assertIn("policy", policy_drift.stdout.lower() + policy_drift.stderr.lower())
                self._run([*compose, "run", "--rm", "rustfs-bootstrap"])

                logs = self._run([*compose, "exec", "-T", "rustfs", "/bin/sh", "-c", "find /logs -type f -print"])
                self.assertEqual(logs.stdout.strip(), "", "RustFS created files in its log directory")
                rustfs_id = self._run([*compose, "ps", "-q", "rustfs"]).stdout.strip()
                logging = subprocess.run(
                    ["docker", "inspect", "--format", "{{json .HostConfig.LogConfig}}", rustfs_id],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(logging.returncode, 0, logging.stderr)
                log_config = json.loads(logging.stdout)
                self.assertEqual(log_config["Type"], "json-file")
                self.assertEqual(log_config["Config"], {"max-file": "3", "max-size": "10m"})
            finally:
                container_result = subprocess.run(
                    [*compose, "ps", "-aq"], capture_output=True, text=True, check=False
                )
                self.assertEqual(container_result.returncode, 0, container_result.stderr)
                for container_id in container_result.stdout.split():
                    label = subprocess.run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            '{{ index .Config.Labels "com.docker.compose.project" }}',
                            container_id,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(label.returncode, 0, label.stderr)
                    self.assertEqual(label.stdout.strip(), project)
                self._run([*compose, "down", "--volumes", "--remove-orphans"])

    def _run(
        self, command: list[str], expect_success: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result


FAKE_RC = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

state_path = Path(os.environ["RC_STATE_FILE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"bucket": None, "users": {}, "policies": {}, "objects": {}}
args = sys.argv[1:]
code = 0
out = ""
if args[:3] == ["alias", "set", "root"]:
    pass
elif args[:2] == ["bucket", "create"]:
    state["bucket"] = args[2].split("/", 1)[1]
elif args[:3] == ["--json", "admin", "policy"] and args[3] == "info":
    name = args[5]
    if name not in state["policies"]:
        code = 5
    else:
        out = json.dumps({"name": name, "policy": state["policies"][name]})
elif args[:3] == ["admin", "policy", "create"]:
    name = args[4]
    state["policies"][name] = json.loads(Path(args[5]).read_text())
elif args[:3] == ["--json", "admin", "user"] and args[3] == "info":
    name = args[5]
    alias = args[4]
    user = state["users"].get(name)
    alias_url = os.environ.get(f"RC_HOST_{alias}", "")
    expected = urlsplit(alias_url).password
    if user is None:
        code = 5
    elif alias != "root" and expected != user["secret"]:
        code = 4
    else:
        policies = [user["policy"]] if user["policy"] else []
        out = json.dumps({"accessKey": name, "status": "enabled", "policies": policies, "memberOf": []})
elif args[:2] == ["admin", "user"] and args[2] == "add":
    name, secret = args[4], args[5]
    state["users"][name] = {"secret": secret, "policy": None}
    out = json.dumps({"access_key": name, "secret_key": secret})
elif args[:3] == ["admin", "policy", "attach"]:
    policy = args[4]
    name = args[args.index("--user") + 1]
    state["users"][name]["policy"] = policy
elif args[:3] == ["bucket", "list"]:
    pass
elif args[:2] == ["cp", "--quiet"] or args[:1] == ["cp"]:
    paths = [part for part in args if not part.startswith("--")]
    source, target = paths[-2:]
    if source.startswith(("api/", "backup/", "root/")):
        alias, bucket, *key = source.split("/")
        out_path = Path(target)
        out_path.write_text(state["objects"]["/".join([bucket, *key])])
    else:
        alias, bucket, *key = target.split("/")
        state["objects"]["/".join([bucket, *key])] = Path(source).read_text()
elif args[:1] == ["rm"]:
    alias, bucket, *key = args[1].split("/")
    del state["objects"]["/".join([bucket, *key])]
else:
    print("unsupported fake rc call: " + " ".join(args), file=sys.stderr)
    code = 2
state_path.write_text(json.dumps(state))
if out:
    print(out)
sys.exit(code)
'''
