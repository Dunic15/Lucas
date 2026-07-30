"""Static validation of the development-only OpenClaw gateway package.

Every requirement that can be checked without AWS, without network and
without deploying is checked here, so the package cannot silently drift out
of compliance: pinned images, mandatory auth, explicit primary model,
persistent state, secrets-from-SSM, TLS, health/backup/restore/teardown, and
the development-only guard that must refuse customer resources.

Key-free by construction — this suite reads files and calls pure functions.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATEWAY = REPO / "gateway"
sys.path.insert(0, str(GATEWAY))

pytestmark = pytest.mark.skipif(
    not GATEWAY.exists(), reason="gateway package absent"
)


def _read(name: str) -> str:
    return (GATEWAY / name).read_text()


# ── package shape ───────────────────────────────────────────────────────────

def test_every_expected_artifact_exists():
    for name in ("README.md", "docker-compose.yml", "openclaw.json",
                 "Caddyfile", "guards.py", "bin/bootstrap.sh", "bin/health.sh",
                 "bin/backup.sh", "bin/restore.sh", "bin/teardown.sh"):
        assert (GATEWAY / name).exists(), f"missing {name}"


# ── requirement: pinned versions, no floating latest ────────────────────────

def test_images_are_digest_pinned_and_never_latest():
    compose = _read("docker-compose.yml")
    images = re.findall(r"^\s*image:\s*(\S+)", compose, re.MULTILINE)
    assert images, "no images declared"
    for image in images:
        assert "@sha256:" in image, f"{image} is not digest-pinned"
        assert not image.endswith(":latest"), f"{image} floats on :latest"


def test_downloaded_tooling_is_version_pinned():
    boot = _read("bin/bootstrap.sh")
    for url in re.findall(r"https://\S+", boot):
        if "docker/compose" in url:
            assert re.search(r"/v\d+\.\d+\.\d+/", url), f"unpinned: {url}"


# ── requirement: explicit primary model ─────────────────────────────────────

def test_primary_model_is_explicitly_claude_opus_5():
    config = json.loads(_read("openclaw.json"))
    assert config["agents"]["defaults"]["model"] == "anthropic/claude-opus-5"
    # The agent Laura actually addresses must not inherit a different default.
    for name, agent in config["agents"].items():
        if name == "defaults":
            continue
        assert agent.get("model") == "anthropic/claude-opus-5", (
            f"agent {name} does not pin the primary model"
        )
    assert "anthropic/claude-opus-5" in config["models"]


# ── requirement: mandatory gateway authentication ───────────────────────────

def test_authentication_is_mandatory_at_both_layers():
    config = json.loads(_read("openclaw.json"))
    auth = config["server"]["auth"]
    assert auth["required"] is True, "gateway auth must be mandatory"
    assert auth["scheme"] == "bearer"
    # The token is injected from the environment, never written in config.
    assert auth["tokenEnv"] == "OPENCLAW_GATEWAY_TOKEN"
    # Defence in depth: the proxy rejects unauthenticated requests too.
    caddy = _read("Caddyfile")
    assert "@unauthenticated" in caddy and "401" in caddy


def test_health_check_proves_unauthenticated_requests_are_rejected():
    health = _read("bin/health.sh")
    assert '"401"' in health, "health check must assert the 401 on no-auth"


# ── requirement: no secrets in the package ──────────────────────────────────

def test_no_secret_material_is_committed():
    secretish = re.compile(
        r"(sk-ant-[A-Za-z0-9]|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    )
    for path in GATEWAY.rglob("*"):
        if path.is_file() and path.suffix not in (".png", ".jpg"):
            assert not secretish.search(path.read_text(errors="ignore")), (
                f"possible secret committed in {path}"
            )


def test_secrets_come_from_ssm_with_locked_down_permissions():
    boot = _read("bin/bootstrap.sh")
    assert "aws ssm get-parameter" in boot and "--with-decryption" in boot
    assert "chmod 0600" in boot, "secrets file must be 0600"
    # A missing parameter must fail the bootstrap, not start an unauthenticated
    # gateway with an empty token.
    assert "exit 3" in boot
    compose = _read("docker-compose.yml")
    assert "secrets.env" in compose
    assert "ANTHROPIC_API_KEY:" not in compose, "no inline secrets in compose"


# ── requirement: persistent state ───────────────────────────────────────────

def test_state_is_persisted_across_restarts():
    compose = _read("docker-compose.yml")
    assert "openclaw-state:/state" in compose
    assert re.search(r"^volumes:", compose, re.MULTILINE)
    assert "restart: unless-stopped" in compose
    config = json.loads(_read("openclaw.json"))
    assert config["state"]["persistSessions"] is True


def test_gateway_survives_reboot_without_a_laptop():
    boot = _read("bin/bootstrap.sh")
    assert "systemctl enable --now openclaw-gateway.service" in boot, (
        "the gateway must come back after a reboot on its own"
    )


# ── requirement: HTTPS/TLS ──────────────────────────────────────────────────

def test_tls_terminates_at_caddy_and_upstream_is_not_published():
    caddy = _read("Caddyfile")
    assert "{$GATEWAY_DOMAIN}" in caddy
    assert "Strict-Transport-Security" in caddy
    compose = _read("docker-compose.yml")
    # Only Caddy publishes ports; OpenClaw is expose-only.
    assert re.search(r"ports:\s*\n\s*- \"80:80\"", compose)
    openclaw_block = compose.split("caddy:")[0]
    assert "ports:" not in openclaw_block, (
        "the OpenClaw container must not publish a host port"
    )


def test_only_the_endpoints_laura_calls_are_proxied():
    caddy = _read("Caddyfile")
    assert "/v1/responses" in caddy and "/health" in caddy
    assert "respond 404" in caddy, "unlisted paths must 404"


# ── requirement: health, logs, backup, restore, teardown ────────────────────

def test_healthcheck_targets_the_real_endpoint():
    compose = _read("docker-compose.yml")
    assert "/health" in compose and "healthcheck:" in compose


def test_logging_is_bounded_and_never_records_bodies():
    compose = _read("docker-compose.yml")
    assert "max-size:" in compose and "max-file:" in compose
    config = json.loads(_read("openclaw.json"))
    assert config["logging"]["logRequestBodies"] is False
    assert config["logging"]["logResponseBodies"] is False


def test_backup_excludes_secrets_and_restore_refetches_them():
    backup = _read("bin/backup.sh")
    assert "secrets.env" not in backup, "a backup must not contain secrets"
    restore = _read("bin/restore.sh")
    assert "SSM" in restore or "bootstrap" in restore


def test_teardown_is_guarded_and_backs_up_first():
    teardown = _read("bin/teardown.sh")
    assert "guards.py" in teardown, "teardown must run the dev-only guard"
    assert "backup.sh" in teardown, "teardown must back up before destroying"
    # It must not silently delete AWS resources it cannot verify are dev-only.
    assert "terminate-instances" in teardown


# ── requirement: development-only safeguards ────────────────────────────────

def test_guard_rejects_every_customer_resource():
    import guards

    for target in ("laura-backend",
                   "arn:aws:apprunner:eu-central-1:1:service/laura-backend/x",
                   "frozen/v1", "relay/cedric-voice",
                   "https://app.lauravatar.com",
                   "python create_meeting_agent.py"):
        with pytest.raises(guards.DevOnlyViolation):
            guards.assert_dev_only(target)


def test_guard_allows_the_development_service_despite_the_prefix_trap():
    """`laura-backend` is a strict prefix of `laura-backend-next`: a substring
    check would reject the only service this package may configure."""
    import guards

    guards.assert_dev_only("laura-backend-next")
    guards.assert_dev_only(
        "arn:aws:apprunner:eu-central-1:1:service/laura-backend-next/abc"
    )
    guards.assert_dev_only("relay/cedric-voice-v2")
    guards.assert_dev_only("main", "/laura/next/openclaw")


def test_guard_has_no_override():
    """A flag that disables the guard would defeat its purpose.

    Checked against the parsed CODE, not the raw text: prose explaining that
    no override exists must not itself trip the check.
    """
    import ast

    tree = ast.parse(_read("guards.py"))
    banned = re.compile(r"force|override|bypass|skip|allow_customer|unsafe",
                        re.IGNORECASE)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and banned.search(node.arg):
            offenders.append(f"parameter {node.arg}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) \
                and banned.search(node.id):
            offenders.append(f"variable {node.id}")
        elif isinstance(node, ast.keyword) and node.arg \
                and banned.search(node.arg):
            offenders.append(f"keyword {node.arg}")
    assert not offenders, (
        f"the development-only guard must not be bypassable: {offenders}"
    )


def test_integration_instructions_target_only_the_dev_service():
    readme = _read("README.md")
    assert "laura-backend-next" in readme
    # Any mention of the customer service must be a warning, not an instruction.
    for line in readme.splitlines():
        if re.search(r"(?<![\w-])laura-backend(?![\w-])", line):
            assert re.search(r"never|not|refus|customer|forbidden|⚠", line,
                             re.IGNORECASE), (
                f"README line names the customer service without warning: {line}"
            )
