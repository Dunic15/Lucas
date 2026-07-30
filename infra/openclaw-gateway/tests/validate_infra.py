#!/usr/bin/env python3
"""Key-free static validation for the OpenClaw dev-gateway IaC.

Runs with the Python stdlib only — no AWS, no network, no keys. It enforces the
security invariants the deployment depends on, so a regression in the templates
or scripts fails locally before anyone touches AWS.

Run standalone:   python3 tests/validate_infra.py
Run via pytest:   pytest infra/openclaw-gateway/tests/ -q
Exit 0 = all invariants hold; non-zero prints every failure.
"""
from __future__ import annotations

import os
import json
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INFRA = os.path.dirname(HERE)
TEMPLATE = os.path.join(INFRA, "cloudformation", "openclaw-gateway.yaml")
COMPOSE = os.path.join(INFRA, "compose", "docker-compose.yml")
CADDYFILE = os.path.join(INFRA, "compose", "Caddyfile")
USER_DATA = os.path.join(INFRA, "scripts", "user-data.sh")
OPENCLAW_CONFIG = os.path.join(INFRA, "config", "openclaw.json")

TEXT_EXTS = (".yaml", ".yml", ".sh", ".py", ".json", ".env", ".md", "")
# Never scan a real (gitignored) params file or a live env file.
SKIP_NAMES = {"params.env", ".env"}

# Concrete key shapes (mirror of .claude/hooks/guard.py) + generic assignment.
SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"\bgsk_[A-Za-z0-9]{20,}",
    r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}",
    r"\bAIza[0-9A-Za-z_\-]{35}",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"(?i)\b(?:api[_-]?key|client[_-]?secret|refresh[_-]?token|password)\b"
    r"['\"]?\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{16,}['\"]",
]


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _iter_files():
    for root, _dirs, files in os.walk(INFRA):
        for name in files:
            if name in SKIP_NAMES:
                continue
            if os.path.splitext(name)[1] in TEXT_EXTS:
                yield os.path.join(root, name)


def check_no_secrets_or_account_ids() -> list[str]:
    fails: list[str] = []
    for path in _iter_files():
        text = _read(path)
        rel = os.path.relpath(path, INFRA)
        for pat in SECRET_PATTERNS:
            m = re.search(pat, text)
            if m:
                fails.append(f"{rel}: key-shaped string '{m.group(0)[:14]}…'")
        # Strip hex runs (digests) before hunting for a 12-digit AWS account id.
        stripped = re.sub(r"[0-9a-fA-F]{12,}", "", text)
        m = re.search(r"(?<!\d)\d{12}(?!\d)", stripped)
        if m:
            fails.append(f"{rel}: looks like a 12-digit AWS account id '{m.group(0)}'")
    return fails


def _resource_block(text: str, name: str) -> str:
    """The YAML block of one top-level (2-space indented) resource by exact name."""
    m = re.search(rf"^  {re.escape(name)}:\n(?:.*?\n)*?(?=^  \S|\Z)", text, re.M)
    return m.group(0) if m else ""


def check_template() -> list[str]:
    fails: list[str] = []
    t = _read(TEMPLATE)

    def need(cond: bool, msg: str):
        if not cond:
            fails.append(f"template: {msg}")

    # Pinned image: the AllowedPattern forces @sha256 and (being anchored) forbids
    # a bare :latest / :main tag by construction.
    need("@sha256:[a-f0-9]{64}" in t,
         "OpenClawImage AllowedPattern must require an @sha256 digest")
    need("AllowedValues: [t4g.small, t4g.medium]" in t and "t3.small" not in t,
         "arm64 AMI must only allow matching t4g instance types")
    need("AllowedValues: [tunnel, caddy]" in t,
         "FrontDoorMode must be explicit and closed to known values")
    # IMDSv2 required.
    need(re.search(r"HttpTokens:\s*required", t) is not None,
         "MetadataOptions.HttpTokens must be 'required' (IMDSv2)")
    # No SSH: no key pair property, no port 22.
    need(re.search(r"^\s*KeyName:", t, re.M) is None,
         "instance must not set a KeyName (SSM-only admin)")
    need(re.search(r"FromPort:\s*'?22'?\b", t) is None,
         "no SSH (port 22) ingress allowed")
    # Log retention set.
    need(re.search(r"RetentionInDays:\s*!Ref\s+LogRetentionInDays", t) is not None,
         "LogGroup must set RetentionInDays")
    # Dev-only enforcement via AllowedPatterns.
    need('AllowedPattern: "^dev' in t, "EnvironmentName must be pinned to a dev pattern")
    need("^laura-openclaw-gw-dev" in t, "NamePrefix must be pinned to the dev prefix")
    need("^/laura/dev/" in t, "SecretsPathPrefix must be pinned under /laura/dev/")
    # No public INGRESS. The only ingress is a separate conditional resource that
    # uses the parameterised CIDR; there must be no inline ingress and no open
    # CidrIp on an ingress rule. (Egress legitimately uses 0.0.0.0/0.)
    need(re.search(r"^\s*SecurityGroupIngress:", t, re.M) is None,
         "no inline SecurityGroupIngress (use the conditional HttpsIngress resource)")
    ingress = _resource_block(t, "HttpsIngress")
    need("CidrIp: !Ref AllowedHttpsCidr" in ingress,
         "HttpsIngress must use !Ref AllowedHttpsCidr")
    need("0.0.0.0/0" not in ingress,
         "HttpsIngress must never use a literal open CIDR")
    # Every open CidrIp must be an egress rule inside the SecurityGroup.
    sg = _resource_block(t, "SecurityGroup")
    open_cidrs = re.findall(r"CidrIp:\s*0\.0\.0\.0/0", t)
    open_in_sg = re.findall(r"CidrIp:\s*0\.0\.0\.0/0", sg)
    need(len(open_cidrs) == len(open_in_sg) and open_in_sg,
         "open CidrIp allowed only on SecurityGroup egress")
    need(re.search(r"IpProtocol:\s*tcp\s+FromPort:\s*7844\s+ToPort:\s*7844", sg) is not None,
         "Cloudflare Tunnel requires TCP egress on 7844")
    need(re.search(r"IpProtocol:\s*udp\s+FromPort:\s*7844\s+ToPort:\s*7844", sg) is not None,
         "Cloudflare Tunnel requires UDP egress on 7844")
    # Least-privilege: SSM read scoped to the secrets prefix, not '*'.
    need("parameter${SecretsPathPrefix}/*" in t,
         "IAM ssm read must be scoped to the secrets prefix")
    need("AmazonSSMManagedInstanceCore" in t,
         "instance role should use SSM Session Manager (no inbound SSH)")
    # Data volume is encrypted and snapshotted, not silently destroyed.
    need(re.search(r"DeletionPolicy:\s*Snapshot", t) is not None,
         "DataVolume must snapshot on delete/replace")
    need(re.search(r"Encrypted:\s*true", t) is not None,
         "EBS volumes must be encrypted")
    # UserData injection placeholder present (deploy.sh fills it).
    need("@@USER_DATA_B64@@" in t, "UserData placeholder must be present")
    return fails


def _check_compose_text(text: str, label: str) -> list[str]:
    fails: list[str] = []

    def need(cond: bool, msg: str):
        if not cond:
            fails.append(f"{label}: {msg}")

    need("cap_drop: [NET_RAW, NET_ADMIN]" in text or
         re.search(r"cap_drop:\s*\[NET_RAW,\s*NET_ADMIN\]", text) is not None,
         "both services must cap_drop NET_RAW, NET_ADMIN")
    need("no-new-privileges:true" in text, "must set no-new-privileges")
    # Only inspect actual image references, not prose that mentions the tags.
    image_refs = re.findall(r"^\s*image:\s*(\S+)", text, re.M)
    bad = [r for r in image_refs if r.endswith(":latest") or r.endswith(":main")]
    need(not bad, f"no :latest / :main image tags (found {bad})")
    literals = [r for r in image_refs if not r.startswith("${")]
    need(all("@sha256:" in r for r in literals),
         f"every literal helper image must be digest-pinned (found {literals})")
    # Gateway image must come from the pinned env indirection, not a literal tag.
    need("${OPENCLAW_IMAGE" in text, "gateway image must use the pinned ${OPENCLAW_IMAGE} ref")
    # The gateway's 18789 must NOT be published to the host — only `expose`d.
    need(re.search(r'"?18789:18789"?', text) is None,
         "gateway port 18789 must NOT be published to the host")
    need('expose: ["18789"]' in text, "gateway port 18789 must be internal-only (expose)")
    need("awslogs" in text, "containers must ship logs to CloudWatch (awslogs)")
    # Only 443 is published.
    need(re.search(r'"?443:443"?', text) is not None, "proxy must publish 443")
    return fails


def check_compose() -> list[str]:
    fails = _check_compose_text(_read(COMPOSE), "compose")
    # The embedded copy inside user-data must keep the same hardening.
    fails += _check_compose_text(_read(USER_DATA), "user-data/compose")
    return fails


def check_caddyfile() -> list[str]:
    fails: list[str] = []
    c = _read(CADDYFILE)
    if "Strict-Transport-Security" not in c:
        fails.append("Caddyfile: must set HSTS")
    if "reverse_proxy gateway:18789" not in c:
        fails.append("Caddyfile: must reverse_proxy to gateway:18789")
    # TLS on: a real hostname site block (auto-HTTPS), never internal/self-signed
    # or disabled — Laura verifies certificates.
    if "{$OPENCLAW_PUBLIC_HOSTNAME}" not in c:
        fails.append("Caddyfile: must serve the public hostname (auto-HTTPS)")
    # Anchor to real directives, not prose comments that mention them.
    if re.search(r"^\s*tls\s+internal\b", c, re.M) or \
       re.search(r"^\s*auto_https\s+off\b", c, re.M):
        fails.append("Caddyfile: must not disable/self-sign TLS")
    return fails


def check_tunnel_overlay() -> list[str]:
    """The Cloudflare-tunnel overlay must stay hardened and pinned too."""
    fails: list[str] = []
    path = os.path.join(INFRA, "compose", "docker-compose.tunnel.yml")
    t = _read(path)
    image_refs = re.findall(r"^\s*image:\s*(\S+)", t, re.M)
    bad = [r for r in image_refs if r.endswith(":latest") or r.endswith(":main")]
    if bad:
        fails.append(f"tunnel: no :latest / :main image tags (found {bad})")
    if not image_refs or any("@sha256:" not in r for r in image_refs):
        fails.append("tunnel: cloudflared image must be digest-pinned")
    if "cap_drop: [NET_RAW, NET_ADMIN]" not in t:
        fails.append("tunnel: cloudflared must cap_drop NET_RAW, NET_ADMIN")
    if "no-new-privileges:true" not in t:
        fails.append("tunnel: must set no-new-privileges")
    # The tunnel must not open a host port (it is outbound-only).
    if re.search(r"^\s*ports:", t, re.M):
        fails.append("tunnel: must not publish host ports (outbound-only)")
    if "env_file: [/opt/openclaw/tunnel.env]" not in t:
        fails.append("tunnel: must receive only its dedicated tunnel env file")
    if "gateway.env" in t:
        fails.append("tunnel: must never receive the gateway/LLM secret env file")
    return fails


def check_user_data() -> list[str]:
    fails: list[str] = []
    u = _read(USER_DATA)

    def need(cond: bool, msg: str):
        if not cond:
            fails.append(f"user-data: {msg}")

    need("X-aws-ec2-metadata-token" in u, "must use IMDSv2 (token header)")
    need("--with-decryption" in u, "must pull secrets with SSM decryption")
    need("chmod 0600" in u and "gateway.env" in u,
         "must write a root-only 0600 env file")
    need("@sha256" in u, "must refuse an unpinned image")
    need('chmod 0700 "${DATA_MOUNT}/auth"' in u or "chmod 0700" in u,
         "auth secret store must not be world/group readable")
    need('chown -R 1000:1000 "${DATA_MOUNT}/auth" "${DATA_MOUNT}/config"' in u,
         "node container user must own persistent auth/config directories")
    need("persistent data volume did not attach" in u and
         'mount "${DATA_MOUNT}"' in u and
         "persistence is degraded" not in u,
         "bootstrap must wait for EBS and fail closed instead of using root disk")
    need("COMPOSE_SHA256_ARM64=\"REPLACE" not in u and
         "sha256sum -c -" in u,
         "Docker Compose download must fail closed on a pinned checksum")
    need('env_file: [/opt/openclaw/tunnel.env]' in u and
         "TUNNEL_TOKEN=${TUNNEL_TOKEN}" not in u,
         "cloudflared must not receive the gateway/LLM env file")
    need('FRONT_DOOR_MODE}" = "tunnel"' in u and
         "-f docker-compose.tunnel.yml up -d" in u,
         "selected tunnel front door must start automatically")

    config_match = re.search(
        r"cat > \"\$\{CONFIG_TMP\}\" <<'JSONEOF'\n(.*?)\nJSONEOF",
        u,
        re.S,
    )
    if not config_match:
        fails.append("user-data: managed OpenClaw JSON config is missing")
    else:
        try:
            config = json.loads(config_match.group(1))
        except json.JSONDecodeError as exc:
            fails.append(f"user-data: managed OpenClaw config is invalid JSON: {exc}")
        else:
            canonical = json.loads(_read(OPENCLAW_CONFIG))
            need(config == canonical,
                 "embedded OpenClaw config must match config/openclaw.json")
            gateway = config.get("gateway", {})
            need(gateway.get("mode") == "local", "gateway.mode must be local")
            need(gateway.get("bind") == "lan", "container gateway must bind to lan")
            need(gateway.get("auth", {}).get("mode") == "token",
                 "gateway auth must explicitly use token mode")
            need(gateway.get("http", {}).get("endpoints", {}).get("responses", {}).get("enabled") is True,
                 "/v1/responses must be explicitly enabled")
            need(gateway.get("controlUi", {}).get("enabled") is False,
                 "public Control UI must be disabled")
            need(gateway.get("terminal", {}).get("enabled") is False,
                 "host terminal must be disabled")
            need(config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary")
                 == "anthropic/claude-opus-5",
                 "gateway primary model must be anthropic/claude-opus-5")
            need(config.get("tools", {}).get("profile") == "minimal",
                 "built-in gateway tools must use the minimal profile")
            need(config.get("tools", {}).get("elevated", {}).get("enabled") is False,
                 "elevated host tools must be disabled")
            need(config.get("browser", {}).get("enabled") is False,
                 "gateway browser runtime must be disabled")
            need(config.get("discovery", {}).get("mdns", {}).get("mode") == "off",
                 "public gateway must not advertise itself over mDNS")
            need(config.get("logging", {}).get("audit", {}).get("messages") == "off",
                 "audit must not persist message bodies")
            maintenance = config.get("session", {}).get("maintenance", {})
            need(maintenance.get("mode") == "enforce" and maintenance.get("pruneAfter"),
                 "session retention must be bounded and enforced")
    return fails


CHECKS = [
    ("secrets/account-ids", check_no_secrets_or_account_ids),
    ("cloudformation", check_template),
    ("compose", check_compose),
    ("caddyfile", check_caddyfile),
    ("tunnel-overlay", check_tunnel_overlay),
    ("user-data", check_user_data),
]


def run() -> list[str]:
    all_fails: list[str] = []
    for _name, fn in CHECKS:
        all_fails += fn()
    return all_fails


# ---- pytest entry points ----------------------------------------------------
def test_no_secrets_or_account_ids():
    assert check_no_secrets_or_account_ids() == []


def test_cloudformation_hardening():
    assert check_template() == []


def test_compose_hardening():
    assert check_compose() == []


def test_caddyfile():
    assert check_caddyfile() == []


def test_user_data():
    assert check_user_data() == []


if __name__ == "__main__":
    fails = run()
    if fails:
        print("FAIL — infra validation:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("OK — all infra invariants hold")
