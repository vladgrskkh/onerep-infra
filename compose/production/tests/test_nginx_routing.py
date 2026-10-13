#!/usr/bin/env python3
"""Host routing checks using an isolated NGINX and RustFS Compose project."""

from __future__ import annotations

import json
import hashlib
import hmac
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
PRODUCTION = ROOT / "compose" / "production"
NGINX_IMAGE = (
    "docker.io/library/nginx:1.30.5-alpine3.24@"
    "sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94"
)
RUSTFS_IMAGE = (
    "ghcr.io/rustfs/rustfs:1.0.1@"
    "sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c"
)
CURL_IMAGE = (
    "docker.io/curlimages/curl:8.22.0@"
    "sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777"
)
PYTHON_IMAGE = (
    "docker.io/library/python:3.14.8-alpine3.24@"
    "sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72"
)
PUBLIC_HOST = "onerep.mooo.com"


class NginxRoutingIntegrationTests(unittest.TestCase):
    """Exercise the production routes with fake services and disposable data."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("Docker is unavailable")
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, check=False
        )
        if probe.returncode != 0:
            raise unittest.SkipTest("Docker Compose is unavailable")
        cls.project = f"ngrt-{uuid.uuid4().hex[:16]}"
        if not re.fullmatch(r"ngrt-[0-9a-f]{16}", cls.project):
            raise RuntimeError("Invalid disposable Compose project name")
        cls.temp = tempfile.TemporaryDirectory(prefix=f"{cls.project}-")
        cls.directory = Path(cls.temp.name)
        cls.compose_file = cls.directory / "compose.yaml"
        cls.compose = [
            "docker", "compose", "--project-name", cls.project,
            "-f", str(cls.compose_file),
        ]
        cls.network = f"{cls.project}_default"
        cls.addClassCleanup(cls._cleanup_disposable_project)

        cls.payload = cls.directory / "payload.bin"
        cls.payload.write_bytes(bytes(range(256)) * 8192)  # 2 MiB, over NGINX's default.
        cls.small_payload = cls.directory / "small-payload.bin"
        cls.small_payload.write_bytes(b"encoded-key-signature-probe")
        cls.access_key = uuid.uuid4().hex[:20].upper()
        cls.secret_key = f"test-secret-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
        cert_dir = cls.directory / "certs"
        cert_dir.mkdir()
        cls._run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(cert_dir / "privkey.pem"),
            "-out", str(cert_dir / "fullchain.pem"), "-days", "1",
            "-subj", f"/CN={PUBLIC_HOST}",
            "-addext", f"subjectAltName=DNS:{PUBLIC_HOST}",
        ])
        cls._write_echo_server()
        cls._write_compose()
        cls._run([*cls.compose, "up", "-d"])
        cls._wait_for("rustfs", "http://rustfs:9000/health/ready")
        bucket_url = cls._presigned_url_for_setup("/gym-media", "PUT")
        bucket = cls._signed_request_for_setup(bucket_url)
        if bucket.returncode != 0 or bucket.stdout.strip() != "200":
            raise RuntimeError("Disposable RustFS bucket setup failed")
        cls._wait_for("auth", "http://auth:8080/ready")
        cls._wait_for("gym-api", "http://gym-api:8081/ready")
        cls._wait_for("nginx", "https://onerep.mooo.com:8443/healthz")
        cls._wait_for(
            "nginx-bootstrap",
            "http://nginx-bootstrap:8080/.well-known/acme-challenge/task5-probe",
        )

    @classmethod
    def _cleanup_disposable_project(cls) -> None:
        try:
            if not re.fullmatch(r"ngrt-[0-9a-f]{16}", cls.project):
                raise RuntimeError("Refusing cleanup outside the unique NGINX test project")
            if not cls.compose_file.exists():
                return
            result = subprocess.run(
                [*cls.compose, "ps", "-aq"], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                raise RuntimeError("Unable to validate the disposable Compose project")
            for container_id in result.stdout.split():
                inspected = subprocess.run(
                    ["docker", "inspect", "--format",
                     '{{ index .Config.Labels "com.docker.compose.project" }}',
                     container_id],
                    capture_output=True, text=True, check=False,
                )
                if inspected.returncode != 0 or inspected.stdout.strip() != cls.project:
                    raise RuntimeError("Refusing cleanup outside the unique test project")
            cleanup = subprocess.run(
                [*cls.compose, "down", "--volumes", "--remove-orphans"],
                capture_output=True, text=True, check=False,
            )
            if cleanup.returncode != 0:
                raise RuntimeError("Disposable NGINX/RustFS Compose cleanup failed")
        finally:
            cls.temp.cleanup()

    @classmethod
    def _write_echo_server(cls) -> None:
        (cls.directory / "echo_server.py").write_text(
            "import json\n"
            "import os\n"
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  body=json.dumps({'path':self.path,'host':self.headers.get('Host'),'service':os.environ['SERVICE']}).encode()\n"
            "  self.send_response(200); self.send_header('Content-Length',str(len(body)))\n"
            "  self.end_headers(); self.wfile.write(body)\n"
            " def log_message(self,*args): pass\n"
            "HTTPServer(('0.0.0.0',int(os.environ['PORT'])),Handler).serve_forever()\n"
        )

    @classmethod
    def _write_compose(cls) -> None:
        nginx_conf = (PRODUCTION / "nginx" / "production.conf").read_text()
        nginx_conf = nginx_conf.replace("listen 443 ssl;", "listen 8443 ssl;")
        (cls.directory / "production.conf").write_text(nginx_conf)
        bootstrap_conf = (PRODUCTION / "nginx" / "bootstrap.conf").read_text()
        bootstrap_conf = bootstrap_conf.replace("listen 80;", "listen 8080;")
        (cls.directory / "bootstrap.conf").write_text(bootstrap_conf)
        challenge_dir = cls.directory / "acme-webroot" / ".well-known" / "acme-challenge"
        challenge_dir.mkdir(parents=True)
        (challenge_dir / "task5-probe").write_text("disposable-challenge-response\n")
        (cls.directory / "compose.yaml").write_text(
            f"""services:
  rustfs:
    image: {RUSTFS_IMAGE}
    environment:
      RUSTFS_VOLUMES: /data
      RUSTFS_ADDRESS: :9000
      RUSTFS_CONSOLE_ENABLE: "true"
      RUSTFS_CONSOLE_ADDRESS: :9001
      RUSTFS_ACCESS_KEY: {cls.access_key}
      RUSTFS_SECRET_KEY: {cls.secret_key}
      RUSTFS_REGION: us-east-1
      RUSTFS_OBS_LOG_DIRECTORY: ""
      RUSTFS_OBS_ENVIRONMENT: production
      RUSTFS_OBS_LOGGER_LEVEL: warn
      RUSTFS_OBS_ENDPOINT: ""
      RUSTFS_OBS_TRACE_ENDPOINT: ""
      RUSTFS_OBS_METRIC_ENDPOINT: ""
      RUSTFS_OBS_LOG_ENDPOINT: ""
    volumes:
      - rustfs-data:/data
    healthcheck:
      test: [CMD, curl, -fsS, --max-time, "5", http://127.0.0.1:9000/health/ready]
      interval: 2s
      timeout: 5s
      retries: 30
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
  auth:
    image: {PYTHON_IMAGE}
    entrypoint: [python3, /test/echo_server.py]
    environment: [SERVICE=auth, PORT=8080]
    volumes: ["{cls.directory}/echo_server.py:/test/echo_server.py:ro"]
  gym-api:
    image: {PYTHON_IMAGE}
    entrypoint: [python3, /test/echo_server.py]
    environment: [SERVICE=gym-api, PORT=8081]
    volumes: ["{cls.directory}/echo_server.py:/test/echo_server.py:ro"]
  nginx:
    image: {NGINX_IMAGE}
    ports: ["127.0.0.1::8443"]
    volumes:
      - "{cls.directory}/production.conf:/etc/nginx/conf.d/default.conf:ro"
      - "{cls.directory}/certs:/etc/letsencrypt/live/{PUBLIC_HOST}:ro"
    depends_on: [rustfs, auth, gym-api]
  nginx-bootstrap:
    image: {NGINX_IMAGE}
    volumes:
      - "{cls.directory}/bootstrap.conf:/etc/nginx/conf.d/default.conf:ro"
      - "{cls.directory}/acme-webroot:/var/www/certbot:ro"
volumes:
  rustfs-data:
"""
        )

    @classmethod
    def _wait_for(cls, service: str, url: str) -> None:
        for _ in range(90):
            result = cls._curl(url, service=service, check=False)
            if result.returncode == 0:
                return
            import time
            time.sleep(1)
        raise RuntimeError(f"Disposable {service} did not become ready")

    @classmethod
    def _presigned_url_for_setup(cls, path: str, method: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        date = timestamp[:8]
        scope = f"{date}/us-east-1/s3/aws4_request"
        parameters = [
            ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
            ("X-Amz-Credential", f"{cls.access_key}/{scope}"),
            ("X-Amz-Date", timestamp),
            ("X-Amz-Expires", "900"),
            ("X-Amz-SignedHeaders", "host"),
        ]
        canonical_query = "&".join(
            f"{name}={quote(value, safe='-_.~')}" for name, value in sorted(parameters)
        )
        host = f"{PUBLIC_HOST}:9000"
        canonical_request = "\n".join(
            [method, path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"]
        )
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", timestamp, scope,
             hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        date_key = hmac.new(
            ("AWS4" + cls.secret_key).encode(), date.encode(), hashlib.sha256
        ).digest()
        region_key = hmac.new(date_key, b"us-east-1", hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return f"http://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"

    @classmethod
    def _signed_request_for_setup(cls, url: str) -> subprocess.CompletedProcess[str]:
        command = [
            "docker", "run", "--rm", "--network", cls.project + "_default",
            "--entrypoint", "/bin/sh", CURL_IMAGE, "-ec",
            "curl -fsS --path-as-is --connect-to "
            f"'{PUBLIC_HOST}:9000:rustfs:9000' --output /dev/null "
            "--write-out '%{http_code}' -X PUT "
            f"'{url}'",
        ]
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @classmethod
    def _curl(
        cls,
        url: str,
        *,
        service: str | None = None,
        args: list[str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "docker", "run", "--rm", "--network", f"{cls.project}_default",
            CURL_IMAGE, "--insecure",
            "--fail", "--silent", "--show-error",
            "--write-out", "%{http_code}",
        ]
        if service == "nginx":
            command.extend(["--connect-to", f"{PUBLIC_HOST}:8443:nginx:8443"])
        if args:
            command.extend(args)
        command.append(url)
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError("Disposable HTTP request failed")
        return result

    def test_compose_does_not_publish_rustfs_console(self) -> None:
        result = subprocess.run(
            ["docker", "compose", "--profile", "*", "--env-file", str(ROOT / ".env.example"),
             "-f", str(PRODUCTION / "compose.yaml"), "config", "--format", "json"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        rustfs = json.loads(result.stdout)["services"]["rustfs"]
        self.assertFalse(rustfs.get("ports"))

    def test_production_nginx_config_validates_with_disposable_certificate(self) -> None:
        result = subprocess.run(
            [*self.compose, "exec", "--no-TTY", "nginx", "nginx", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_bootstrap_nginx_serves_acme_http01_challenge_from_shared_webroot(self) -> None:
        result = self._curl(
            "http://nginx-bootstrap:8080/.well-known/acme-challenge/task5-probe",
            service="nginx-bootstrap",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("disposable-challenge-response", result.stdout)

    def test_media_proxy_preserves_signed_uri_and_streams_uploads(self) -> None:
        config = (PRODUCTION / "nginx" / "production.conf").read_text()
        location = config.split("location /gym-media/ {", 1)[1].split("}", 1)[0]
        self.assertIn("proxy_pass http://rustfs:9000;", location)
        self.assertIn("client_max_body_size 25m;", location)
        self.assertIn("proxy_request_buffering off;", location)

    def test_auth_and_gym_prefixes_are_removed_upstream(self) -> None:
        for prefix, expected_service in (("auth", "auth"), ("gym", "gym-api")):
            url = f"https://{PUBLIC_HOST}:8443/{prefix}/v1/check?x=one%2Btwo"
            result = self._curl(
                url, service="nginx", args=["--include", "--output", "-"], check=False
            )
            self.assertEqual(
                result.returncode, 0,
                "routing response did not complete; "
                f"HTTP output={result.stdout[:200]!r}; "
                f"upstream={self._nginx_upstream_error()}",
            )
            body = self._last_json_body(result.stdout)
            self.assertEqual(body["path"], "/v1/check?x=one%2Btwo")
            self.assertEqual(body["host"], PUBLIC_HOST)
            self.assertEqual(body["service"], expected_service)

    def test_signed_rustfs_presigned_put_get_preserves_encoded_path_host_and_query(self) -> None:
        key = "folder/space?name+literal@tag%2Fsegment"
        put_url = self._presigned_url(key, "PUT", "")
        put = self._signed_request(put_url, "PUT", self.small_payload)
        self.assertEqual(
            put.returncode, 0,
            f"signed PUT failed; HTTP output={put.stdout[:100]!r}; error={put.stderr[:160]!r}",
        )
        self.assertEqual(put.stdout.strip(), "200")

        get_url = self._presigned_url(key, "GET", "x-probe=one%2Btwo")
        get = self._signed_request(get_url, "GET", None, checksum=True)
        self.assertEqual(get.returncode, 0)
        expected_hash = hashlib.sha256(self.small_payload.read_bytes()).hexdigest()
        self.assertEqual(get.stdout.splitlines(), ["200", expected_hash])
        self.assertEqual(get.stderr, "")

    def test_rustfs_accepts_sigv4_for_encoded_key_before_proxying(self) -> None:
        url = self._presigned_url(
            "direct/encoded?key+part", "PUT", "", port=9000, scheme="http"
        )
        result = self._signed_request(
            url, "PUT", self.small_payload, destination="rustfs:9000"
        )
        self.assertEqual(
            result.returncode, 0,
            f"direct RustFS signature baseline failed; HTTP output={result.stdout[:100]!r}",
        )
        self.assertEqual(result.stdout.strip(), "200")

    def test_signed_rustfs_upload_accepts_bodies_above_default_one_megabyte(self) -> None:
        url = self._presigned_url("uploads/over-one-megabyte.bin", "PUT", "")
        result = self._signed_request(url, "PUT", self.payload)
        self.assertEqual(
            result.returncode, 0,
            f"large signed PUT failed; HTTP output={result.stdout[:100]!r}; error={result.stderr[:160]!r}",
        )
        self.assertEqual(result.stdout.strip(), "200")

    def _presigned_url(
        self,
        key: str,
        method: str,
        extra_query: str,
        *,
        port: int = 8443,
        scheme: str = "https",
    ) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        date = timestamp[:8]
        scope = f"{date}/us-east-1/s3/aws4_request"
        path = "/gym-media/" + quote(key, safe="/-_.~")
        parameters = [
            ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
            ("X-Amz-Credential", f"{self.access_key}/{scope}"),
            ("X-Amz-Date", timestamp),
            ("X-Amz-Expires", "900"),
            ("X-Amz-SignedHeaders", "host"),
        ]
        if extra_query:
            parameters.extend(
                (key, value) for key, value in parse_qsl(extra_query, keep_blank_values=True)
            )
        canonical_query = "&".join(
            f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
            for name, value in sorted(parameters)
        )
        host = f"{PUBLIC_HOST}:{port}"
        canonical_request = "\n".join(
            [method, path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256", timestamp, scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        date_key = hmac.new(
            ("AWS4" + self.secret_key).encode(), date.encode(), hashlib.sha256
        ).digest()
        region_key = hmac.new(date_key, b"us-east-1", hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return (
            f"{scheme}://{host}{path}?{canonical_query}"
            f"&X-Amz-Signature={signature}"
        )

    def _signed_request(
        self,
        url: str,
        method: str,
        payload: Path | None,
        *,
        checksum: bool = False,
        destination: str = "nginx:8443",
    ) -> subprocess.CompletedProcess[str]:
        request_url = urlsplit(url)
        port = request_url.port or (443 if request_url.scheme == "https" else 80)
        tls = "-k " if request_url.scheme == "https" else ""
        request = (
            f"curl {tls}-fsS --path-as-is --connect-to "
            f"'{PUBLIC_HOST}:{port}:{destination}' --output /tmp/result "
            "--write-out '%{http_code}\\n' "
            f"-X {method} "
            + ("--data-binary @/payload.bin " if payload is not None else "")
            + f"'{url}'"
            + ("; sha256sum /tmp/result | cut -d' ' -f1" if checksum else "")
        )
        command = [
            "docker", "run", "--rm", "--network", self.project + "_default",
            "-v", f"{payload or self.payload}:/payload.bin:ro",
            "--entrypoint", "/bin/sh",
            CURL_IMAGE, "-ec", request,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        return result

    def _nginx_upstream_error(self) -> str:
        result = subprocess.run(
            [*self.compose, "logs", "--no-color", "nginx"],
            capture_output=True, text=True, check=False,
        )
        markers = (
            "connect() failed",
            "host not found in upstream",
            "no live upstreams",
            "upstream timed out",
        )
        details = []
        for line in result.stderr.splitlines() + result.stdout.splitlines():
            if any(marker in line for marker in markers):
                error = line.split(" while ", 1)[0].rsplit(": ", 1)[-1]
                target = re.search(r'upstream: "http://([^/]+)', line)
                details.append(f"{error} target={target.group(1) if target else 'unknown'}")
        containers = subprocess.run(
            [*self.compose, "ps", "-q", "nginx", "auth", "gym-api"],
            capture_output=True, text=True, check=False,
        )
        addresses = []
        for container in containers.stdout.split():
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container],
                capture_output=True, text=True, check=False,
            )
            if inspected.returncode == 0:
                addresses.append(inspected.stdout.strip())
        detail = "; ".join(details[-3:]) or "no matching NGINX error log entry"
        return f"{detail}; service-networks={','.join(addresses)}"

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError("Disposable test setup command failed")
        return result

    @staticmethod
    def _last_json_body(output: str) -> dict[str, str]:
        start = output.index("{")
        value, _ = json.JSONDecoder().raw_decode(output[start:])
        return value


class NginxSetupFailureCleanupTests(unittest.TestCase):
    def test_partial_compose_start_is_cleaned_when_class_setup_fails(self) -> None:
        calls: list[list[str]] = []
        project_labels: dict[str, str] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[:3] == ["docker", "compose", "version"]:
                return subprocess.CompletedProcess(command, 0, "Docker Compose\n", "")
            if command and command[0] == "openssl":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["docker", "compose"] and "up" in command:
                return subprocess.CompletedProcess(command, 1, "", "simulated partial startup")
            if command[:2] == ["docker", "compose"] and "ps" in command:
                project = command[command.index("--project-name") + 1]
                project_labels["project"] = project
                return subprocess.CompletedProcess(command, 0, "container-id\n", "")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, project_labels["project"] + "\n", ""
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        def setup_failure(self) -> None:
            return None

        probe = type(
            "NginxSetupFailureProbe",
            (NginxRoutingIntegrationTests,),
            {"test_setup_failure_marker": setup_failure},
        )
        suite = unittest.TestSuite([probe("test_setup_failure_marker")])
        result = unittest.TestResult()
        with patch("shutil.which", return_value="/usr/bin/docker"), patch(
            "subprocess.run", side_effect=fake_run
        ):
            try:
                suite.run(result)
            finally:
                if hasattr(probe, "temp"):
                    probe.temp.cleanup()

        self.assertEqual(len(result.errors), 1)
        cleanups = [call for call in calls if call[:2] == ["docker", "compose"] and "down" in call]
        self.assertEqual(len(cleanups), 1)
        project = project_labels.get("project", "")
        self.assertRegex(project, r"^ngrt-[0-9a-f]{16}$")
        inspections = [call for call in calls if call[:2] == ["docker", "inspect"]]
        self.assertEqual(len(inspections), 1)
        self.assertEqual(cleanups[0][cleanups[0].index("--project-name") + 1], project)
        self.assertIn("--volumes", cleanups[0])
        self.assertLess(calls.index(inspections[0]), calls.index(cleanups[0]))


if __name__ == "__main__":
    unittest.main()
