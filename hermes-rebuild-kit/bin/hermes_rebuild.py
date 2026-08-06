#!/usr/bin/env python3
"""Production-oriented Hermes disaster-recovery controller.

Design goals:
- repeatable/idempotent execution
- atomic, root-owned state
- secret redaction and encrypted master secret storage
- pinned Git checkouts with rollback points
- explicit human gates for OAuth and HADA deployment
- generic project manifest support
- dedicated Hermes skills/adapters for HADA and Agent Forge

The controller intentionally does not auto-install an NVIDIA kernel driver.
Driver selection is GPU/OS specific and may require a reboot. It installs and
validates NVIDIA Container Toolkit only when a working driver is already present.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import getpass
import hashlib
import json
import os
import pathlib
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"PyYAML is required: {exc}")

VERSION = "1.1.0"
DEFAULT_STATE_DIR = pathlib.Path("/etc/hermes-rebuild")
DEFAULT_INSTALL_ROOT = pathlib.Path("/opt/hermes-stack")
DEFAULT_BACKUP_ROOT = pathlib.Path("/var/backups/hermes-rebuild")
SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL|WEBHOOK)", re.I)
REDACT_RE = re.compile(r"(?i)(key|token|secret|password)=([^\s]+)")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: pathlib.Path, data: str, mode: int = 0o600, owner: tuple[int, int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if owner:
            os.chown(tmp, owner[0], owner[1])
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact(value: str) -> str:
    return REDACT_RE.sub(lambda m: f"{m.group(1)}=<redacted>", value)


class Log:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def _emit(self, level: str, message: str) -> None:
        print(f"[{level}] {redact(message)}", flush=True)

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._emit("DEBUG", message)


class Runner:
    def __init__(self, log: Log, dry_run: bool = False) -> None:
        self.log = log
        self.dry_run = dry_run

    def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        quiet: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        if user and os.geteuid() == 0:
            user_info = pwd.getpwnam(user)
            command = [
                "runuser", "-u", user, "--", "env",
                f"HOME={user_info.pw_dir}",
                f"USER={user}",
                f"LOGNAME={user}",
                f"PATH=/opt/hermes-tools/npm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                *command,
            ]
        if not quiet:
            self.log.debug("run: " + " ".join(shlex.quote(part) for part in command))
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            stdout = redact((result.stdout or "")[-2000:])
            stderr = redact((result.stderr or "")[-2000:])
            raise RuntimeError(
                f"command failed ({result.returncode}): {shlex.join(argv)}\n{stdout}\n{stderr}".strip()
            )
        return result

    def output(self, argv: list[str], **kwargs: Any) -> str:
        result = self.run(argv, capture=True, **kwargs)
        return (result.stdout or "").strip()


class State:
    def __init__(self, state_dir: pathlib.Path, log: Log) -> None:
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.log = log
        self.data: dict[str, Any] = {
            "schema": 1,
            "controller_version": VERSION,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "host": socket.gethostname(),
            "components": {},
            "runs": [],
            "rollback_points": [],
            "manual_actions": [],
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception as exc:
                raise RuntimeError(f"state file is unreadable: {self.path}: {exc}") from exc

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n", 0o600)

    def component(self, name: str, status: str, detail: str = "", **metadata: Any) -> None:
        safe_metadata = {
            key: ("<redacted>" if SECRET_KEY_RE.search(key) else value)
            for key, value in metadata.items()
        }
        self.data.setdefault("components", {})[name] = {
            "status": status,
            "detail": detail,
            "updated_at": utc_now(),
            "metadata": safe_metadata,
        }
        self.save()

    def manual(self, action: str) -> None:
        items = self.data.setdefault("manual_actions", [])
        if action not in items:
            items.append(action)
        self.save()

    def rollback_point(self, payload: dict[str, Any]) -> None:
        self.data.setdefault("rollback_points", []).append(payload)
        self.save()

    def begin_run(self, mode: str, config_hash: str) -> str:
        run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        self.data.setdefault("runs", []).append({
            "id": run_id,
            "started_at": utc_now(),
            "mode": mode,
            "config_sha256": config_hash,
            "status": "running",
        })
        self.save()
        return run_id

    def end_run(self, run_id: str, status: str) -> None:
        for run in reversed(self.data.get("runs", [])):
            if run.get("id") == run_id:
                run["finished_at"] = utc_now()
                run["status"] = status
                break
        self.save()

    def summary(self) -> str:
        lines = [f"Hermes rebuild state: {self.path}", f"Updated: {self.data.get('updated_at', 'unknown')}"]
        components = self.data.get("components", {})
        for name in sorted(components):
            item = components[name]
            lines.append(f"  {name:24} {item.get('status','unknown'):16} {item.get('detail','')}")
        actions = self.data.get("manual_actions", [])
        if actions:
            lines.append("Manual actions:")
            lines.extend(f"  - {action}" for action in actions)
        return "\n".join(lines)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def default_config() -> dict[str, Any]:
    sudo_user = os.environ.get("SUDO_USER")
    runtime_user = sudo_user if sudo_user and sudo_user != "root" else "bobthabuilda"
    return {
        "server": {
            "type": "auto",
            "ram_gb": "auto",
            "vram_gb": "auto",
            "deployment_mode": "production",
            "timezone": "Australia/Melbourne",
            "expected_hostname": "hermes-station",
            "enforce_grounding": True,
            "allow_hostname_change": True,
            "forbidden_hostnames": ["hada-control"],
        },
        "runtime": {
            "user": runtime_user,
            "install_root": str(DEFAULT_INSTALL_ROOT),
            "state_dir": str(DEFAULT_STATE_DIR),
            "backup_root": str(DEFAULT_BACKUP_ROOT),
        },
        "versions": {
            "python": "auto",
            "node_major": "auto",
            "nvidia_container_toolkit": "1.19.1-1",
            "claude_code": "latest-on-first-install",
            "codex_cli": "latest-on-first-install",
        },
        "repositories": {
            "hermes": {
                "url": "https://github.com/mdyerapis-coder/hermes-agent.git",
                "ref": "d71033a4077a6dfdcdb42c9e9eeab4c41e4a7012",
            },
            "hada": {
                "url": "https://github.com/mdyerapis-coder/hada.git",
                "ref": "9e1b3d0e098bc46d6d866b1ce8395cda3dde063d",
            },
            "agent_forge": {
                "url": "https://github.com/mdyerapis-coder/agent-forge-e2e.git",
                "ref": "main",
                "allow_empty": True,
                "allow_mutable_ref": True,
                "command": [],
            },
        },
        "components": {
            "host_grounding": True,
            "openssh": True,
            "firewall": True,
            "tailscale": True,
            "laptop_lid_protection": True,
            "docker": True,
            "nvidia_container_toolkit": "auto",
            "hermes": True,
            "hada": True,
            "agent_forge": True,
            "claude_code": True,
            "codex_cli": True,
            "gateway_service": True,
        },
        "network": {
            "tailscale": {
                "enable_ssh": True,
                "hostname": "hermes-station",
                "operator": runtime_user,
                "accept_dns": True,
                "accept_routes": False,
            },
            "ssh": {
                "tailscale_only": True,
                "password_authentication": "preserve",
                "permit_root_login": "prohibit-password",
            },
        },
        "power": {
            "always_on": True,
            "laptop_lid_policy": "auto",
            "install_inhibitor_service": True,
        },
        "auth": {
            "claude_code_oauth": True,
            "chatgpt_codex_oauth": True,
            "run_oauth_during_interactive_recovery": True,
        },
        "hermes": {
            "home": "auto",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "gateway_watchdog_seconds": 120,
            "enable_telegram": True,
        },
        "hada": {
            "run_local_tests": True,
            "run_preflight": True,
            "allow_deployment": False,
        },
        "agent_forge": {
            "timeout_seconds": 3600,
            "max_parallel": 1,
            "require_git_worktree": True,
        },
        "projects_manifest": "auto",
        "projects": [],
        "secrets": {},
    }


def load_mapping(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return data


def ensure_secure_config_file(path: pathlib.Path) -> None:
    st = path.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(f"config/manifest must not be group/world writable: {path}")


def detect_ram_gb() -> float:
    meminfo = pathlib.Path("/proc/meminfo").read_text(encoding="utf-8")
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo, re.M)
    return round(int(match.group(1)) / 1024 / 1024, 1) if match else 0.0


def detect_vram_gb(runner: Runner) -> float:
    if not shutil.which("nvidia-smi"):
        return 0.0
    result = runner.run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        check=False, capture=True, quiet=True,
    )
    if result.returncode != 0:
        return 0.0
    values = []
    for line in (result.stdout or "").splitlines():
        with contextlib.suppress(ValueError):
            values.append(float(line.strip()) / 1024)
    return round(sum(values), 1)


def detect_server_type(runner: Runner) -> str:
    if shutil.which("systemd-detect-virt"):
        value = runner.output(["systemd-detect-virt"], check=False, quiet=True)
        if value and value != "none":
            return "cloud-vm" if value in {"kvm", "xen", "amazon", "microsoft", "google"} else "vps"
    product = pathlib.Path("/sys/class/dmi/id/product_name")
    if product.exists():
        text = product.read_text(encoding="utf-8", errors="ignore").lower()
        if any(term in text for term in ("compute engine", "amazon ec2", "virtual machine", "openstack")):
            return "cloud-vm"
    return "bare-metal"


LAPTOP_CHASSIS_TYPES = {8, 9, 10, 11, 14, 30, 31, 32}


def read_optional(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def detect_laptop(sys_root: pathlib.Path = pathlib.Path("/")) -> dict[str, Any]:
    """Detect portable/laptop chassis using DMI plus battery/lid evidence.

    DMI is authoritative when present. Battery and ACPI lid evidence are used as
    fallbacks for hardware whose firmware reports an incomplete chassis type.
    """
    dmi = sys_root / "sys/class/dmi/id"
    raw_type = read_optional(dmi / "chassis_type")
    with contextlib.suppress(ValueError):
        chassis_type = int(raw_type)
        if chassis_type in LAPTOP_CHASSIS_TYPES:
            return {
                "is_laptop": True,
                "chassis_type": chassis_type,
                "product_name": read_optional(dmi / "product_name"),
                "evidence": ["dmi-chassis"],
            }
    batteries = list((sys_root / "sys/class/power_supply").glob("BAT*"))
    lid_paths = list((sys_root / "proc/acpi/button/lid").glob("*"))
    evidence = []
    if batteries:
        evidence.append("battery")
    if lid_paths:
        evidence.append("acpi-lid")
    return {
        "is_laptop": bool(batteries and lid_paths),
        "chassis_type": int(raw_type) if raw_type.isdigit() else None,
        "product_name": read_optional(dmi / "product_name"),
        "evidence": evidence,
    }


def validate_identity_config(config: dict[str, Any], current_hostname: str) -> None:
    server = config.get("server", {})
    expected = str(server.get("expected_hostname", "hermes-station")).strip().lower()
    current = current_hostname.split(".", 1)[0].strip().lower()
    forbidden = {str(item).strip().lower() for item in server.get("forbidden_hostnames", [])}
    forbidden.add("hada-control")
    if expected in forbidden:
        raise RuntimeError(f"refusing forbidden target hostname: {expected}")
    if current in forbidden:
        raise RuntimeError(f"refusing to operate on retired/forbidden host: {current}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", expected):
        raise ValueError(f"invalid expected hostname: {expected!r}")
    for name, repo in config.get("repositories", {}).items():
        url = str(repo.get("url", "")) if isinstance(repo, dict) else ""
        if re.search(r"(?:^|[/:@.])hada-control(?:[/:.]|$)", url, re.I):
            raise RuntimeError(f"repository {name} targets retired/forbidden hada-control: {url}")
    for project in config.get("projects", []):
        if not isinstance(project, dict):
            continue
        source = project.get("source", {})
        url = str(source.get("url", "")) if isinstance(source, dict) else ""
        if re.search(r"(?:^|[/:@.])hada-control(?:[/:.]|$)", url, re.I):
            raise RuntimeError(f"project {project.get('name', '<unnamed>')} targets retired/forbidden hada-control")


def prompt_choice(label: str, choices: list[str], default: str) -> str:
    prompt = f"{label} [{'/'.join(choices)}] (default {default}): "
    while True:
        value = input(prompt).strip() or default
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(choices)}")


def prompt_bool(label: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{label} [{suffix}]: ").strip().lower()
    return default if not value else value in {"y", "yes", "true", "1"}


def interactive_config(config: dict[str, Any], runner: Runner) -> tuple[dict[str, Any], dict[str, str]]:
    print("\nAll-in-One Hermes Rebuild — interactive setup\n")
    detected_ram = detect_ram_gb()
    detected_vram = detect_vram_gb(runner)
    detected_type = detect_server_type(runner)
    config["server"]["type"] = prompt_choice(
        "Server type", ["auto", "bare-metal", "vps", "cloud-vm"], "auto"
    )
    if config["server"]["type"] == "auto":
        config["server"]["type"] = detected_type
    ram = input(f"Total RAM in GB (detected {detected_ram}): ").strip()
    vram = input(f"Total VRAM in GB (detected {detected_vram}): ").strip()
    config["server"]["ram_gb"] = float(ram) if ram else detected_ram
    config["server"]["vram_gb"] = float(vram) if vram else detected_vram
    config["server"]["deployment_mode"] = prompt_choice(
        "Deployment mode", ["development", "production"], "production"
    )
    config["runtime"]["user"] = input(
        f"Runtime Linux user (default {config['runtime']['user']}): "
    ).strip() or config["runtime"]["user"]
    expected = input(
        f"Target hostname (default {config['server']['expected_hostname']}): "
    ).strip() or config["server"]["expected_hostname"]
    config["server"]["expected_hostname"] = expected
    config["network"]["tailscale"]["hostname"] = expected
    config["server"]["allow_hostname_change"] = prompt_bool(
        f"Allow this recovery to set the hostname to {expected}", True
    )
    config["power"]["always_on"] = prompt_bool(
        "Keep Hermes Station running continuously, including when a laptop lid is closed", True
    )

    config["auth"]["claude_code_oauth"] = prompt_bool("Install Claude Code and perform OAuth login", True)
    config["auth"]["chatgpt_codex_oauth"] = prompt_bool("Install Codex CLI and perform ChatGPT OAuth login", True)
    config["hada"]["allow_deployment"] = prompt_bool(
        "Authorise HADA deployment phases during this recovery", False
    )

    secret_prompts = [
        ("OPENAI_API_KEY", "OpenAI API key (optional when using ChatGPT/Codex OAuth)"),
        ("ANTHROPIC_API_KEY", "Anthropic API key (optional when using Claude OAuth)"),
        ("OPENROUTER_API_KEY", "OpenRouter API key"),
        ("HF_TOKEN", "Hugging Face token"),
        ("GITHUB_TOKEN", "GitHub token for private/rate-limited repository access"),
        ("TELEGRAM_BOT_TOKEN", "Telegram bot token"),
        ("TELEGRAM_ALLOWED_USERS", "Telegram allowed user IDs (comma-separated)"),
        ("TAILSCALE_AUTH_KEY", "Tailscale one-off or reusable auth key (optional; browser login when blank)"),
        ("SSH_AUTHORIZED_KEY", "SSH public key for the runtime user (optional)"),
        ("POSTGRES_PASSWORD", "PostgreSQL password (generated when blank)"),
        ("HERMES_WEBHOOK_TOKEN", "Webhook token (generated when blank)"),
    ]
    secrets: dict[str, str] = {}
    print("\nSecrets are entered with terminal echo disabled and are never printed.")
    for key, label in secret_prompts:
        value = getpass.getpass(f"{label} (blank to keep existing/skip): ").strip()
        if value:
            secrets[key] = value
    extra_names = input("Additional required secret variable names, comma-separated (blank for none): ").strip()
    for key in [item.strip().upper() for item in extra_names.split(",") if item.strip()]:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid additional secret variable name: {key}")
        value = getpass.getpass(f"{key} (blank to skip): ").strip()
        if value:
            secrets[key] = value

    inline_projects: list[dict[str, Any]] = []
    while prompt_bool("Add another project not already in projects.manifest.yaml", False):
        name = input("Project name (letters, numbers, dot, dash, underscore): ").strip()
        url = input("Git clone URL: ").strip()
        ref = input("Pinned branch/tag/commit (default main): ").strip() or "main"
        commands: list[list[str]] = []
        print("Enter install commands as JSON argv arrays, one per line; blank ends the list.")
        while True:
            raw = input("install argv JSON: ").strip()
            if not raw:
                break
            parsed = json.loads(raw)
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError("install command must be a JSON string array")
            commands.append(parsed)
        inline_projects.append({
            "name": name,
            "enabled": True,
            "source": {"type": "git", "url": url, "ref": ref},
            "install": commands,
        })
    config["projects"] = inline_projects
    return config, secrets


def resolve_secret_spec(spec: Any) -> str:
    if spec is None:
        return ""
    if not isinstance(spec, str):
        raise ValueError("secret values must be env:NAME, file:/path, prompt, or empty")
    if spec.startswith("env?:"):
        name = spec[5:]
        return os.environ.get(name, "")
    if spec.startswith("env:"):
        name = spec[4:]
        if not name or name not in os.environ:
            raise ValueError(f"required secret environment variable is unset: {name}")
        return os.environ[name]
    if spec.startswith("file?:"):
        path = pathlib.Path(spec[6:])
        if not path.exists():
            return ""
        st = path.stat()
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"secret file permissions are too broad: {path}")
        return path.read_text(encoding="utf-8").strip()
    if spec.startswith("file:"):
        path = pathlib.Path(spec[5:])
        st = path.stat()
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"secret file permissions are too broad: {path}")
        return path.read_text(encoding="utf-8").strip()
    if spec == "prompt":
        if not sys.stdin.isatty():
            raise ValueError("secret spec 'prompt' requires an interactive terminal")
        return getpass.getpass("Secret value: ").strip()
    if spec == "":
        return ""
    raise ValueError("literal secrets in config are rejected; use env:NAME, env?:NAME, file:/secure/path, or file?:/secure/path")


def resolve_secrets(config: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, spec in config.get("secrets", {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(key)):
            raise ValueError(f"invalid environment variable name in secrets: {key}")
        value = resolve_secret_spec(spec)
        if value:
            result[str(key)] = value
    return result



def load_existing_secrets(state_dir: pathlib.Path, runner: Runner) -> dict[str, str]:
    """Decrypt the existing master secret file without logging its contents."""
    identity = state_dir / "age/identity.txt"
    encrypted = state_dir / "secrets.env.age"
    if not (identity.exists() and encrypted.exists()):
        return {}
    if not shutil.which("age"):
        raise RuntimeError("encrypted secrets exist but the age binary is unavailable; refusing to rotate generated secrets")
    with tempfile.NamedTemporaryFile(prefix="existing-hermes-secrets-", delete=False) as handle:
        temp_path = pathlib.Path(handle.name)
    os.chmod(temp_path, 0o600)
    temp_path.unlink()
    try:
        result = runner.run(["age", "-d", "-i", str(identity), "-o", str(temp_path), str(encrypted)], check=False, quiet=True)
        if result.returncode != 0:
            return {}
        values: dict[str, str] = {}
        for raw in temp_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            parsed = shlex.split(raw, posix=True)
            if len(parsed) != 1 or "=" not in parsed[0]:
                continue
            key, value = parsed[0].split("=", 1)
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                values[key] = value
        return values
    finally:
        runner.run(["shred", "-u", str(temp_path)], check=False, quiet=True)


def hardware_profile(config: dict[str, Any], runner: Runner) -> dict[str, Any]:
    server = config["server"]
    ram = detect_ram_gb() if server.get("ram_gb") == "auto" else float(server["ram_gb"])
    vram = detect_vram_gb(runner) if server.get("vram_gb") == "auto" else float(server["vram_gb"])
    server_type = detect_server_type(runner) if server.get("type") == "auto" else server["type"]
    memory_class = "minimal" if ram < 8 else "standard" if ram < 32 else "high-memory"
    gpu_class = "cpu-only" if vram <= 0 else "small-gpu" if vram < 12 else "medium-gpu" if vram < 24 else "large-gpu"
    local_model_class = "remote-only" if vram <= 0 else "3b-7b" if vram < 12 else "7b-14b" if vram < 24 else "14b-32b"
    portable = detect_laptop()
    return {
        "server_type": server_type,
        "is_laptop": portable["is_laptop"],
        "chassis_type": portable["chassis_type"],
        "product_name": portable["product_name"],
        "portable_evidence": portable["evidence"],
        "ram_gb": ram,
        "vram_gb": vram,
        "memory_class": memory_class,
        "gpu_class": gpu_class,
        "local_model_class": local_model_class,
    }


def apply_version_policy(config: dict[str, Any], profile: dict[str, Any]) -> None:
    """Resolve compatibility-driven runtime versions from hardware policy.

    RAM/VRAM control optional workload breadth and local-model sizing. Python is
    constrained by Hermes' declared >=3.11,<3.14 range. Node uses a lower LTS
    line on minimal hosts and the newer configured LTS line elsewhere.
    """
    versions = config.setdefault("versions", {})
    if str(versions.get("python", "auto")) == "auto":
        versions["python"] = "3.13"
    if str(versions.get("node_major", "auto")) == "auto":
        versions["node_major"] = 22 if profile["memory_class"] == "minimal" else 24


@dataclass
class Context:
    config: dict[str, Any]
    log: Log
    runner: Runner
    state: State
    kit_root: pathlib.Path
    profile: dict[str, Any]
    runtime_user: str
    runtime_home: pathlib.Path
    install_root: pathlib.Path
    backup_root: pathlib.Path
    secrets: dict[str, str]
    interactive: bool
    system_root: pathlib.Path = pathlib.Path("/")

    def system_path(self, absolute: str | pathlib.Path) -> pathlib.Path:
        path = pathlib.Path(absolute)
        if not path.is_absolute():
            raise ValueError(f"system path must be absolute: {path}")
        if self.system_root == pathlib.Path("/"):
            return path
        return self.system_root / path.relative_to("/")


def user_ids(username: str) -> tuple[int, int]:
    entry = pwd.getpwnam(username)
    return entry.pw_uid, entry.pw_gid


def ensure_runtime_user(ctx: Context) -> None:
    try:
        entry = pwd.getpwnam(ctx.runtime_user)
        ctx.runtime_home = pathlib.Path(entry.pw_dir)
        ctx.state.component("runtime-user", "ok", f"existing user {ctx.runtime_user}")
        return
    except KeyError:
        pass
    ctx.runner.run(["useradd", "--create-home", "--shell", "/bin/bash", ctx.runtime_user])
    entry = pwd.getpwnam(ctx.runtime_user)
    ctx.runtime_home = pathlib.Path(entry.pw_dir)
    ctx.state.component("runtime-user", "installed", f"created user {ctx.runtime_user}")


def package_manager() -> str:
    for name in ("apt-get", "dnf", "yum"):
        if shutil.which(name):
            return name
    raise RuntimeError("supported package manager not found (apt, dnf, yum)")


def install_base_packages(ctx: Context) -> None:
    pm = package_manager()
    common = ["curl", "git", "jq", "rsync", "unzip", "tar", "openssl", "ca-certificates", "age", "rclone"]
    if pm == "apt-get":
        ctx.runner.run(["apt-get", "update", "-qq"])
        ctx.runner.run(["apt-get", "install", "-y", "--no-install-recommends", *common, "xz-utils", "gnupg", "build-essential", "shellcheck", "lsof", "ripgrep", "ffmpeg", "tmux", "pkg-config", "libffi-dev", "libssl-dev", "libsqlite3-dev", "openssh-server", "firewalld"])
    else:
        ctx.runner.run([pm, "install", "-y", *common, "xz", "gcc", "gcc-c++", "make", "gnupg2", "ShellCheck", "lsof", "ripgrep", "tmux", "pkgconf-pkg-config", "libffi-devel", "openssl-devel", "sqlite-devel", "openssh-server", "firewalld", "dnf-plugins-core"])
        ctx.runner.run([pm, "install", "-y", "ffmpeg"], check=False)
    ctx.state.component("base-packages", "ok", f"installed via {pm}")



def _command_exists(ctx: Context, name: str) -> bool:
    if ctx.system_root != pathlib.Path("/"):
        return True
    return shutil.which(name) is not None


def ensure_host_grounding(ctx: Context) -> None:
    """Establish and persist the Hermes Station machine identity.

    Fresh Fedora installs may begin with a generic hostname. The configured
    target is set only when explicitly permitted; the retired `hada-control`
    identity is always rejected. Grounding is written both system-wide and into
    Hermes' context directory so the host role survives application rebuilds.
    """
    if not ctx.config["components"].get("host_grounding", True):
        ctx.state.component("host-grounding", "skipped", "disabled by config")
        return
    server = ctx.config["server"]
    expected = str(server.get("expected_hostname", "hermes-station")).split(".", 1)[0].lower()
    current = ctx.runner.output(["hostnamectl", "--static"], check=False, quiet=True) or socket.gethostname()
    current = current.split(".", 1)[0].lower()
    validate_identity_config(ctx.config, current)
    if current != expected:
        if not bool(server.get("allow_hostname_change", False)):
            raise RuntimeError(
                f"host identity mismatch: current={current}, expected={expected}; "
                "hostname change is not authorised"
            )
        ctx.runner.run(["hostnamectl", "set-hostname", expected])
        verified = ctx.runner.output(["hostnamectl", "--static"], check=False, quiet=True)
        if ctx.system_root == pathlib.Path("/") and verified and verified.split(".", 1)[0].lower() != expected:
            raise RuntimeError(f"hostname change did not take effect: {verified}")
        current = expected

    timezone = str(server.get("timezone", "Australia/Melbourne"))
    ctx.runner.run(["timedatectl", "set-timezone", timezone], check=False)
    identity = {
        "schema": 1,
        "hostname": expected,
        "role": "Hermes Station",
        "timezone": timezone,
        "runtime_user": ctx.runtime_user,
        "deployment_mode": server.get("deployment_mode", "production"),
        "server_type": ctx.profile.get("server_type"),
        "is_laptop": bool(ctx.profile.get("is_laptop")),
        "forbidden_hosts": sorted({"hada-control", *server.get("forbidden_hostnames", [])}),
        "safeguards": {
            "human_approval_required": [
                "merge", "deployment", "repair", "secret rotation", "infrastructure change"
            ],
            "hada_source_is_not_deployment": True,
            "agent_forge_is_subordinate_to_hermes": True,
            "telegram_first_operations": True,
        },
    }
    grounding_json = ctx.system_path("/etc/hermes-rebuild/grounding.json")
    atomic_write(grounding_json, json.dumps(identity, indent=2, sort_keys=True) + "\n", 0o600)
    grounding_md = f"""# Hermes Station Grounding

- Host identity: `{expected}`
- Runtime user: `{ctx.runtime_user}`
- Timezone: `{timezone}`
- Role: always-on Hermes orchestration station, Telegram-first.
- `hada-control` is retired and forbidden. Never resolve, contact, recreate, deploy to, or delegate to it.
- HADA source, `/opt/hada`, `/var/lib/hada`, releases, or evidence do **not** prove HADA is deployed.
- Human approval is mandatory for merges, deployments, repairs, secrets, and infrastructure changes.
- Agent Forge, Claude Code, and Codex are subordinate workers; Hermes remains the orchestrator.
"""
    atomic_write(ctx.system_path("/etc/hermes-rebuild/GROUNDING.md"), grounding_md, 0o644)
    atomic_write(
        ctx.system_path("/etc/profile.d/hermes-station-grounding.sh"),
        f"export HERMES_STATION_HOST={shlex.quote(expected)}\nexport TZ={shlex.quote(timezone)}\n",
        0o644,
    )
    motd = (
        f"Hermes Station: {expected} | {timezone}\n"
        "Governance: human approval required for merge/deploy/repair/secrets/infrastructure.\n"
        "Retired host: hada-control (forbidden).\n"
    )
    atomic_write(ctx.system_path("/etc/motd.d/60-hermes-station"), motd, 0o644)
    uid, gid = user_ids(ctx.runtime_user)
    hermes_home = ctx.runtime_home / ".hermes"
    context_dir = hermes_home / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    for owned in (hermes_home, context_dir):
        os.chown(owned, uid, gid)
    context_path = context_dir / "HERMES-STATION.md"
    atomic_write(context_path, grounding_md, 0o644, (uid, gid))
    ctx.state.data["host"] = expected
    ctx.state.component("host-grounding", "ok", f"grounded as {expected}", identity_file=str(grounding_json))


def configure_laptop_lid_policy(ctx: Context) -> None:
    """Keep an always-on laptop awake when its lid closes.

    A systemd-logind drop-in is the durable policy. A systemd-inhibit service
    makes the protection effective immediately and also blocks desktop idle
    suspend. The policy is installed only after laptop detection succeeds.
    """
    if not ctx.config["components"].get("laptop_lid_protection", True):
        ctx.state.component("laptop-lid", "skipped", "disabled by config")
        return
    portable = detect_laptop(ctx.system_root)
    if not portable["is_laptop"]:
        ctx.state.component("laptop-lid", "skipped", "host is not detected as a laptop", evidence=portable["evidence"])
        return
    power = ctx.config.get("power", {})
    policy = str(power.get("laptop_lid_policy", "auto"))
    if not bool(power.get("always_on", True)) or policy in {"unchanged", "suspend"}:
        ctx.state.component("laptop-lid", "unchanged", "always-on lid protection not requested")
        return
    if policy not in {"auto", "ignore"}:
        raise ValueError(f"unsupported laptop_lid_policy: {policy}")
    dropin = ctx.system_path("/etc/systemd/logind.conf.d/60-hermes-station-lid.conf")
    content = """# Managed by Hermes rebuild. Keep the always-on station awake with the lid closed.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
"""
    atomic_write(dropin, content, 0o644)
    unit_path = ctx.system_path("/etc/systemd/system/hermes-station-awake.service")
    unit = """[Unit]
Description=Hermes Station always-on sleep and lid inhibitor
After=systemd-logind.service
Wants=systemd-logind.service

[Service]
Type=simple
ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle:handle-lid-switch --who=HermesStation --why=Always-on orchestration host --mode=block /usr/bin/sleep infinity
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
    atomic_write(unit_path, unit, 0o644)
    if ctx.system_root == pathlib.Path("/"):
        ctx.runner.run(["systemctl", "daemon-reload"])
        if bool(power.get("install_inhibitor_service", True)):
            ctx.runner.run(["systemctl", "enable", "--now", "hermes-station-awake.service"])
    ctx.state.component(
        "laptop-lid",
        "protected",
        "lid-close suspend disabled for always-on laptop",
        dropin=str(dropin),
        inhibitor_service=str(unit_path),
        evidence=portable["evidence"],
    )


def _valid_ssh_public_key(value: str) -> bool:
    return bool(re.fullmatch(r"(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh.com) [A-Za-z0-9+/=]+(?: .*)?", value.strip()))


def install_openssh(ctx: Context) -> None:
    if not ctx.config["components"].get("openssh", True):
        ctx.state.component("openssh", "skipped", "disabled by config")
        return
    config = ctx.config.get("network", {}).get("ssh", {})
    dropin = ctx.system_path("/etc/ssh/sshd_config.d/60-hermes-rebuild.conf")
    lines = [
        "# Managed by Hermes rebuild",
        "PubkeyAuthentication yes",
        f"PermitRootLogin {config.get('permit_root_login', 'prohibit-password')}",
    ]
    password_mode = str(config.get("password_authentication", "preserve"))
    if password_mode in {"yes", "no"}:
        lines.append(f"PasswordAuthentication {password_mode}")
    atomic_write(dropin, "\n".join(lines) + "\n", 0o644)
    public_key = ctx.secrets.get("SSH_AUTHORIZED_KEY", "").strip()
    if public_key:
        if not _valid_ssh_public_key(public_key):
            raise ValueError("SSH_AUTHORIZED_KEY is not a recognised OpenSSH public key")
        uid, gid = user_ids(ctx.runtime_user)
        ssh_dir = ctx.runtime_home / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        os.chown(ssh_dir, uid, gid)
        auth = ssh_dir / "authorized_keys"
        existing = auth.read_text(encoding="utf-8").splitlines() if auth.exists() else []
        if public_key not in existing:
            existing.append(public_key)
        atomic_write(auth, "\n".join(line for line in existing if line.strip()) + "\n", 0o600, (uid, gid))
    if ctx.system_root == pathlib.Path("/"):
        verify = ctx.runner.run(["sshd", "-t"], check=False, capture=True, quiet=True)
        if verify.returncode != 0:
            raise RuntimeError(f"sshd configuration validation failed: {redact(verify.stderr or '')}")
        ctx.runner.run(["systemctl", "enable", "--now", "sshd.service"])
        ctx.runner.run(["systemctl", "reload", "sshd.service"], check=False)
    ctx.secrets.pop("SSH_AUTHORIZED_KEY", None)
    ctx.state.component("openssh", "ok", "sshd configured; password policy preserved unless explicitly set")
    ctx.state.manual("Fedora reinstall creates new SSH host keys; remove the old hermes-station key from mobile known_hosts only after verifying the new fingerprint.")


def _tailscale_connected(ctx: Context) -> bool:
    result = ctx.runner.run(["tailscale", "status", "--json"], check=False, capture=True, quiet=True)
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return str(data.get("BackendState", "")).lower() == "running"


def tailscale_ipv4(ctx: Context) -> str:
    result = ctx.runner.run(["tailscale", "ip", "-4"], check=False, capture=True, quiet=True)
    if result.returncode != 0:
        return ""
    return next((line.strip() for line in (result.stdout or "").splitlines() if line.strip()), "")


def install_tailscale(ctx: Context) -> None:
    if not ctx.config["components"].get("tailscale", True):
        ctx.state.component("tailscale", "skipped", "disabled by config")
        return
    pm = package_manager()
    if ctx.system_root == pathlib.Path("/") and not shutil.which("tailscale"):
        if pm in {"dnf", "yum"}:
            repo = pathlib.Path("/etc/yum.repos.d/tailscale.repo")
            ctx.runner.run(["curl", "-fsSL", "https://pkgs.tailscale.com/stable/fedora/tailscale.repo", "-o", str(repo)])
            ctx.runner.run([pm, "install", "-y", "tailscale"])
        elif pm == "apt-get":
            with tempfile.NamedTemporaryFile(prefix="tailscale-install-", suffix=".sh", delete=False) as handle:
                installer = pathlib.Path(handle.name)
            try:
                ctx.runner.run(["curl", "-fsSL", "https://tailscale.com/install.sh", "-o", str(installer)])
                os.chmod(installer, 0o700)
                ctx.runner.run(["bash", str(installer)])
            finally:
                installer.unlink(missing_ok=True)
    if ctx.system_root == pathlib.Path("/"):
        ctx.runner.run(["systemctl", "enable", "--now", "tailscaled.service"])
    ts = ctx.config.get("network", {}).get("tailscale", {})
    hostname = str(ts.get("hostname") or ctx.config["server"].get("expected_hostname", "hermes-station"))
    args = [
        "tailscale", "up",
        f"--hostname={hostname}",
        f"--operator={ts.get('operator') or ctx.runtime_user}",
        f"--accept-dns={'true' if ts.get('accept_dns', True) else 'false'}",
        f"--accept-routes={'true' if ts.get('accept_routes', False) else 'false'}",
    ]
    if ts.get("enable_ssh", True):
        args.append("--ssh")
    connected = _tailscale_connected(ctx)
    if not connected:
        auth_key = ctx.secrets.get("TAILSCALE_AUTH_KEY", "")
        if auth_key:
            auth_file = ctx.state.dir / ".tailscale-auth-key"
            atomic_write(auth_file, auth_key.strip() + "\n", 0o600)
            try:
                ctx.runner.run([*args, f"--auth-key=file:{auth_file}"])
            finally:
                ctx.runner.run(["shred", "-u", str(auth_file)], check=False, quiet=True)
                auth_file.unlink(missing_ok=True)
        elif ctx.interactive:
            ctx.log.info("Tailscale will print a login URL. Open it on your mobile and approve hermes-station.")
            ctx.runner.run([*args, "--qr"])
        else:
            ctx.state.component("tailscale", "manual-action", "installed but authentication is required")
            ctx.state.manual(f"Run: sudo tailscale up --ssh --hostname={hostname} --operator={ctx.runtime_user}")
            return
    else:
        set_args = [
            "tailscale", "set",
            f"--hostname={hostname}",
            f"--operator={ts.get('operator') or ctx.runtime_user}",
            f"--accept-dns={'true' if ts.get('accept_dns', True) else 'false'}",
            f"--accept-routes={'true' if ts.get('accept_routes', False) else 'false'}",
            f"--ssh={'true' if ts.get('enable_ssh', True) else 'false'}",
        ]
        ctx.runner.run(set_args, check=False)
    connected = _tailscale_connected(ctx)
    ip = tailscale_ipv4(ctx) if connected else ""
    # Provisioning keys are bootstrap-only and must not be retained in Hermes' runtime environment.
    ctx.secrets.pop("TAILSCALE_AUTH_KEY", None)
    ctx.state.component("tailscale", "connected" if connected else "degraded", ip or "no Tailscale IPv4 address")


def configure_firewall(ctx: Context) -> None:
    if not ctx.config["components"].get("firewall", True):
        ctx.state.component("firewall", "skipped", "disabled by config")
        return
    if ctx.system_root != pathlib.Path("/"):
        ctx.state.component("firewall", "planned", "sandbox root")
        return
    ctx.runner.run(["systemctl", "enable", "--now", "firewalld.service"], check=False)
    ssh_config = ctx.config.get("network", {}).get("ssh", {})
    if bool(ssh_config.get("tailscale_only", True)) and _tailscale_connected(ctx):
        ctx.runner.run(["firewall-cmd", "--permanent", "--zone=trusted", "--add-interface=tailscale0"], check=False)
        ctx.runner.run(["firewall-cmd", "--permanent", "--zone=public", "--remove-service=ssh"], check=False)
        ctx.runner.run(["firewall-cmd", "--reload"], check=False)
        ctx.state.component("firewall", "restricted", "SSH limited to trusted Tailscale interface")
    else:
        ctx.state.component("firewall", "preserved", "public SSH rules left unchanged until Tailscale is connected")
        if bool(ssh_config.get("tailscale_only", True)):
            ctx.state.manual("After Tailscale connects, rerun recovery to restrict OpenSSH to tailscale0.")


def send_telegram_recovery_notice(ctx: Context, failures: int, checks: list[dict[str, str]]) -> None:
    token = ctx.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
    recipients = [item.strip() for item in ctx.secrets.get("TELEGRAM_ALLOWED_USERS", "").split(",") if item.strip()]
    if not token or not recipients:
        return
    status = "READY" if failures == 0 else f"COMPLETED WITH {failures} FAILURE(S)"
    ip = tailscale_ipv4(ctx)
    host = str(ctx.config["server"].get("expected_hostname", "hermes-station"))
    failing = ", ".join(item["name"] for item in checks if item["status"] == "FAIL") or "none"
    text = (
        f"Hermes Station recovery: {status}\n"
        f"Host: {host}\n"
        f"Tailscale IP: {ip or 'not connected'}\n"
        f"SSH: ssh {ctx.runtime_user}@{ip or host}\n"
        f"Failed checks: {failing}\n"
        "Human approval remains required for HADA deployment, merges, repairs, secrets and infrastructure changes."
    )
    for chat_id in recipients:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}")
        except Exception as exc:
            ctx.log.warn(f"Telegram recovery notice failed for chat {chat_id}: {exc}")
            ctx.state.component("telegram-recovery-notice", "degraded", "notification failed")
            return
    ctx.state.component("telegram-recovery-notice", "sent", f"sent to {len(recipients)} allowed recipient(s)")


def print_mobile_access_summary(ctx: Context) -> None:
    ip = tailscale_ipv4(ctx)
    host = str(ctx.config["server"].get("expected_hostname", "hermes-station"))
    print("\nMobile recovery access")
    print(f"  Host identity: {host}")
    print(f"  Tailscale IPv4: {ip or 'not connected yet'}")
    print(f"  SSH command: ssh {ctx.runtime_user}@{ip or host}")
    if ctx.profile.get("is_laptop") and ctx.config.get("power", {}).get("always_on", True):
        print("  Lid policy: protected; closing the lid will not suspend the station")


def backup_existing(ctx: Context, run_id: str) -> pathlib.Path:
    target = ctx.backup_root / run_id
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    records: dict[str, Any] = {"id": run_id, "created_at": utc_now(), "paths": {}, "git_commits": {}}
    candidates = [
        ctx.runtime_home / ".hermes",
        ctx.state.dir / "secrets.env.age",
        ctx.state.dir / "agent-forge.json",
        pathlib.Path("/etc/systemd/system/hermes-gateway-rebuild.service"),
    ]
    for index, source in enumerate(candidates):
        if source.exists():
            destination = target / f"{index:02d}-{source.name}"
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
            records["paths"][str(source)] = str(destination)
    for relative in ("hermes-agent", "hada", "agent-forge"):
        repo = ctx.install_root / relative
        if (repo / ".git").exists():
            result = ctx.runner.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, check=False, capture=True, quiet=True)
            if result.returncode == 0:
                records["git_commits"][str(repo)] = (result.stdout or "").strip()
    atomic_write(target / "rollback.json", json.dumps(records, indent=2) + "\n", 0o600)
    ctx.state.rollback_point(records)
    return target


def install_node(ctx: Context) -> None:
    desired = int(ctx.config["versions"]["node_major"])
    if shutil.which("node"):
        result = ctx.runner.output(["node", "--version"], check=False)
        match = re.match(r"v(\d+)", result)
        if match and int(match.group(1)) >= desired:
            ctx.state.component("nodejs", "ok", result)
            return
    arch_map = {"x86_64": "x64", "aarch64": "arm64"}
    machine = os.uname().machine
    if machine not in arch_map:
        raise RuntimeError(f"unsupported Node.js architecture: {machine}")
    base = f"https://nodejs.org/dist/latest-v{desired}.x"
    with tempfile.TemporaryDirectory(prefix="node-install-") as tmp:
        tmp_path = pathlib.Path(tmp)
        sums = tmp_path / "SHASUMS256.txt"
        ctx.runner.run(["curl", "-fsSLo", str(sums), f"{base}/SHASUMS256.txt"])
        pattern = re.compile(rf"^([0-9a-f]{{64}})\s+(node-v[^\s]+-linux-{arch_map[machine]}\.tar\.xz)$", re.M)
        match = pattern.search(sums.read_text(encoding="utf-8"))
        if not match:
            raise RuntimeError(f"could not resolve Node.js {desired} binary")
        expected, filename = match.groups()
        archive = tmp_path / filename
        ctx.runner.run(["curl", "-fsSLo", str(archive), f"{base}/{filename}"])
        if sha256_file(archive) != expected:
            raise RuntimeError("Node.js archive checksum mismatch")
        destination = pathlib.Path("/opt/nodejs") / filename.removesuffix(".tar.xz")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            ctx.runner.run(["tar", "-xJf", str(archive), "-C", str(destination.parent)])
        for binary in ("node", "npm", "npx", "corepack"):
            link = pathlib.Path("/usr/local/bin") / binary
            with contextlib.suppress(FileNotFoundError):
                link.unlink()
            link.symlink_to(destination / "bin" / binary)
    ctx.state.component("nodejs", "installed", ctx.runner.output(["node", "--version"]))


def install_uv(ctx: Context) -> None:
    if not shutil.which("uv"):
        ctx.runner.run([
            "bash", "-c",
            "curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh",
        ])
    ctx.runner.run(["uv", "python", "install", str(ctx.config["versions"]["python"])])
    ctx.state.component("uv-python", "ok", ctx.runner.output(["uv", "--version"]))


def install_docker(ctx: Context) -> None:
    if not ctx.config["components"].get("docker", True):
        ctx.state.component("docker", "skipped", "disabled by config")
        return
    if shutil.which("docker") and ctx.runner.run(["docker", "version"], check=False, quiet=True).returncode == 0:
        ctx.runner.run(["usermod", "-aG", "docker", ctx.runtime_user], check=False)
        ctx.state.component("docker", "ok", ctx.runner.output(["docker", "--version"]))
        return
    pm = package_manager()
    if pm == "apt-get":
        os_release = {}
        for line in pathlib.Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip('"')
        distro = "debian" if os_release.get("ID") == "debian" else "ubuntu"
        ctx.runner.run(["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
        ctx.runner.run(["curl", "-fsSL", f"https://download.docker.com/linux/{distro}/gpg", "-o", "/etc/apt/keyrings/docker.asc"])
        os.chmod("/etc/apt/keyrings/docker.asc", 0o644)
        arch = ctx.runner.output(["dpkg", "--print-architecture"])
        codename = os_release.get("VERSION_CODENAME")
        repo_line = f"deb [arch={arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/{distro} {codename} stable\n"
        atomic_write(pathlib.Path("/etc/apt/sources.list.d/docker.list"), repo_line, 0o644)
        ctx.runner.run(["apt-get", "update", "-qq"])
        ctx.runner.run(["apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
    else:
        ctx.runner.run([pm, "install", "-y", "dnf-plugins-core"], check=False)
        repo_url = "https://download.docker.com/linux/fedora/docker-ce.repo" if pathlib.Path("/etc/fedora-release").exists() else "https://download.docker.com/linux/centos/docker-ce.repo"
        ctx.runner.run(["dnf", "config-manager", "--add-repo", repo_url])
        ctx.runner.run([pm, "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
    ctx.runner.run(["systemctl", "enable", "--now", "docker"])
    ctx.runner.run(["usermod", "-aG", "docker", ctx.runtime_user], check=False)
    ctx.state.component("docker", "installed", ctx.runner.output(["docker", "--version"]))


def install_nvidia_container_toolkit(ctx: Context) -> None:
    mode = ctx.config["components"].get("nvidia_container_toolkit", "auto")
    has_driver = shutil.which("nvidia-smi") and ctx.runner.run(["nvidia-smi"], check=False, quiet=True).returncode == 0
    if mode is False or (mode == "auto" and not has_driver):
        reason = "disabled" if mode is False else "no working NVIDIA driver detected"
        ctx.state.component("nvidia-container-toolkit", "skipped", reason)
        if ctx.profile["vram_gb"] > 0 and not has_driver:
            ctx.state.manual("Install a supported NVIDIA driver, reboot, then rerun the bootstrap for container GPU support.")
        return
    if not has_driver:
        raise RuntimeError("NVIDIA toolkit requested but nvidia-smi is not functional")
    if not shutil.which("nvidia-ctk"):
        pm = package_manager()
        version = str(ctx.config["versions"].get("nvidia_container_toolkit", "")).strip()
        if pm == "apt-get":
            ctx.runner.run(["bash", "-c", "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"])
            ctx.runner.run(["bash", "-c", "curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list"])
            ctx.runner.run(["apt-get", "update", "-qq"])
            package = f"nvidia-container-toolkit={version}" if version else "nvidia-container-toolkit"
            result = ctx.runner.run(["apt-get", "install", "-y", package], check=False)
            if result.returncode != 0 and version:
                ctx.log.warn(f"Pinned NVIDIA toolkit {version} unavailable; installing repository stable")
                ctx.runner.run(["apt-get", "install", "-y", "nvidia-container-toolkit"])
        else:
            ctx.runner.run(["bash", "-c", "curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo > /etc/yum.repos.d/nvidia-container-toolkit.repo"])
            ctx.runner.run([pm, "install", "-y", "nvidia-container-toolkit"])
    ctx.runner.run(["nvidia-ctk", "runtime", "configure", "--runtime=docker"])
    ctx.runner.run(["systemctl", "restart", "docker"])
    driver_text = ctx.runner.output(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], check=False, quiet=True).splitlines()[0]
    driver_major = int(driver_text.split(".", 1)[0]) if driver_text and driver_text.split(".", 1)[0].isdigit() else 0
    candidates = (
        ["nvidia/cuda:13.0.1-cudnn-runtime-ubuntu24.04", "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04"]
        if driver_major >= 580 else
        ["nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04", "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04"]
        if driver_major >= 525 else
        ["nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04"]
    )
    selected = ""
    test = subprocess.CompletedProcess([], 1)
    for image in candidates:
        probe = ctx.runner.run(["docker", "manifest", "inspect", image], check=False, capture=True, quiet=True, timeout=120)
        if probe.returncode != 0:
            continue
        selected = image
        test = ctx.runner.run(["docker", "run", "--rm", "--gpus", "all", image, "nvidia-smi"], check=False, capture=True, quiet=True, timeout=600)
        if test.returncode == 0:
            break
    status = "ok" if test.returncode == 0 else "degraded"
    detail = f"GPU container probe passed with {selected}" if test.returncode == 0 else "toolkit installed but no compatible CUDA/cuDNN image probe passed"
    ctx.state.component("nvidia-container-toolkit", status, detail, driver_version=driver_text, cuda_cudnn_image=selected)


def git_environment(ctx: Context) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = ctx.secrets.get("GITHUB_TOKEN", "")
    if token:
        helper = pathlib.Path("/run/hermes-rebuild/git-askpass")
        helper.parent.mkdir(parents=True, exist_ok=True)
        script = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *) printf '%s\n' "$GITHUB_TOKEN" ;;
esac
"""
        atomic_write(helper, script, 0o700)
        env["GIT_ASKPASS"] = str(helper)
        env["GITHUB_TOKEN"] = token
    return env


def git_checkout(ctx: Context, name: str, spec: dict[str, Any]) -> pathlib.Path:
    destination = ctx.install_root / name
    url = str(spec["url"])
    ref = str(spec.get("ref", "main"))
    allow_empty = bool(spec.get("allow_empty", False))
    allow_mutable = bool(spec.get("allow_mutable_ref", False))
    if ctx.config["server"].get("deployment_mode") == "production" and not re.fullmatch(r"[0-9a-fA-F]{40}", ref) and not allow_mutable:
        raise RuntimeError(f"production repository ref must be a full commit SHA for {name}: {ref}")
    git_env = git_environment(ctx)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").exists():
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"destination exists and is not a Git repository: {destination}")
        ctx.runner.run(["git", "clone", "--filter=blob:none", url, str(destination)], env=git_env)
    current_url = ctx.runner.output(["git", "remote", "get-url", "origin"], cwd=destination, env=git_env)
    if current_url != url:
        raise RuntimeError(f"repository origin mismatch for {name}: {current_url}")
    previous_result = ctx.runner.run(["git", "rev-parse", "--verify", "HEAD"], cwd=destination, env=git_env, check=False, capture=True, quiet=True)
    previous = (previous_result.stdout or "").strip() if previous_result.returncode == 0 else ""
    ctx.runner.run(["git", "fetch", "--prune", "origin"], cwd=destination, env=git_env)
    target = ref
    if not re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        remote_probe = ctx.runner.run(["git", "rev-parse", "--verify", f"origin/{ref}^{{commit}}"], cwd=destination, env=git_env, check=False, capture=True, quiet=True)
        if remote_probe.returncode == 0:
            target = f"origin/{ref}"
    checkout = ctx.runner.run(["git", "checkout", "--detach", target], cwd=destination, env=git_env, check=False, capture=True)
    if checkout.returncode != 0:
        if not allow_empty:
            raise RuntimeError(f"unable to checkout {ref} for {name}: {(checkout.stderr or '').strip()}")
        resolved = ""
        ctx.state.component(name, "source-empty", "repository has no checkoutable commit", repository=url)
    else:
        resolved = ctx.runner.output(["git", "rev-parse", "HEAD"], cwd=destination, env=git_env)
        ctx.state.component(name, "source-ready", f"checkout {resolved[:12]}", repository=url, commit=resolved, previous_commit=previous)
    uid, gid = user_ids(ctx.runtime_user)
    for root, dirs, files in os.walk(destination):
        os.chown(root, uid, gid)
        for item in dirs:
            os.chown(os.path.join(root, item), uid, gid)
        for item in files:
            with contextlib.suppress(FileNotFoundError):
                os.chown(os.path.join(root, item), uid, gid)
    return destination


def install_hermes(ctx: Context) -> pathlib.Path:
    if not ctx.config["components"].get("hermes", True):
        ctx.state.component("hermes", "skipped", "disabled by config")
        return ctx.install_root / "hermes-agent"
    repo = git_checkout(ctx, "hermes-agent", ctx.config["repositories"]["hermes"])
    env = os.environ.copy()
    env["UV_PYTHON"] = str(ctx.config["versions"]["python"])
    profile = ctx.profile["memory_class"]
    extras = ["--extra", "messaging", "--extra", "cli", "--extra", "mcp"]
    if profile != "minimal":
        extras += ["--extra", "web"]
    ctx.runner.run(["uv", "sync", "--locked", *extras], cwd=repo, env=env, user=ctx.runtime_user, timeout=1800)
    hermes_bin = repo / ".venv/bin/hermes"
    if not hermes_bin.exists():
        raise RuntimeError("Hermes executable was not created")
    wrapper = pathlib.Path("/usr/local/bin/hermes")
    wrapper_text = f"""#!/bin/sh
if [ "$(id -un)" = {shlex.quote(ctx.runtime_user)} ]; then
  exec env HOME={shlex.quote(str(ctx.runtime_home))} HERMES_HOME={shlex.quote(str(ctx.runtime_home / '.hermes'))} {shlex.quote(str(hermes_bin))} "$@"
elif [ "$(id -u)" -eq 0 ]; then
  exec runuser -u {shlex.quote(ctx.runtime_user)} -- env HOME={shlex.quote(str(ctx.runtime_home))} HERMES_HOME={shlex.quote(str(ctx.runtime_home / '.hermes'))} {shlex.quote(str(hermes_bin))} "$@"
else
  echo "Hermes is managed as {ctx.runtime_user}; run: sudo -iu {ctx.runtime_user} hermes ..." >&2
  exit 77
fi
"""
    atomic_write(wrapper, wrapper_text, 0o755)
    configure_hermes(ctx, hermes_bin)
    ctx.state.component("hermes", "installed", ctx.runner.output([str(hermes_bin), "--version"], user=ctx.runtime_user, check=False) or "installed")
    return repo


def configure_hermes(ctx: Context, hermes_bin: pathlib.Path) -> None:
    uid, gid = user_ids(ctx.runtime_user)
    hermes_home = ctx.runtime_home / ".hermes" if ctx.config["hermes"].get("home") == "auto" else pathlib.Path(ctx.config["hermes"]["home"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    os.chown(hermes_home, uid, gid)
    os.chmod(hermes_home, 0o700)
    config_path = hermes_home / "config.yaml"
    existing: dict[str, Any] = {}
    if config_path.exists():
        with contextlib.suppress(Exception):
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
    desired = {
        "model": {
            "provider": ctx.config["hermes"].get("provider", "openai-codex"),
            "default": ctx.config["hermes"].get("model", "gpt-5.6-sol"),
        },
        "terminal": {"cwd": str(ctx.install_root)},
        "gateway": {"systemd_watchdog_seconds": int(ctx.config["hermes"].get("gateway_watchdog_seconds", 120))},
        "display": {"file_mutation_verifier": True},
    }
    merged = deep_merge(existing, desired)
    atomic_write(config_path, yaml.safe_dump(merged, sort_keys=False), 0o600, (uid, gid))


def secret_env_text(secrets: dict[str, str]) -> str:
    lines = []
    for key in sorted(secrets):
        value = secrets[key]
        if "\n" in value or "\x00" in value:
            raise ValueError(f"secret {key} contains unsupported control characters")
        escaped = value.replace("'", "'\"'\"'")
        lines.append(f"{key}='{escaped}'")
    return "\n".join(lines) + "\n"


def store_secrets(ctx: Context) -> None:
    if not ctx.secrets:
        ctx.state.component("secrets", "skipped", "no secrets provided")
        return
    identity_dir = ctx.state.dir / "age"
    identity = identity_dir / "identity.txt"
    encrypted = ctx.state.dir / "secrets.env.age"
    identity_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(identity_dir, 0o700)
    if not identity.exists():
        ctx.runner.run(["age-keygen", "-o", str(identity)])
        os.chmod(identity, 0o600)
    recipient = ctx.runner.output(["age-keygen", "-y", str(identity)], quiet=True)
    with tempfile.NamedTemporaryFile("w", prefix="hermes-secrets-", delete=False, encoding="utf-8") as temp:
        temp_path = pathlib.Path(temp.name)
        os.chmod(temp_path, 0o600)
        temp.write(secret_env_text(ctx.secrets))
    encrypted_tmp = encrypted.with_name(f".{encrypted.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        encrypted_tmp.unlink()
    try:
        ctx.runner.run(["age", "-r", recipient, "-o", str(encrypted_tmp), str(temp_path)], quiet=True)
        os.chmod(encrypted_tmp, 0o600)
        os.replace(encrypted_tmp, encrypted)
    finally:
        ctx.runner.run(["shred", "-u", str(temp_path)], check=False, quiet=True)
        with contextlib.suppress(FileNotFoundError):
            encrypted_tmp.unlink()
    materialize_runtime_env(ctx)
    ctx.state.component("secrets", "ok", f"encrypted master at {encrypted}", count=len(ctx.secrets))


def materialize_runtime_env(ctx: Context) -> None:
    identity = ctx.state.dir / "age/identity.txt"
    encrypted = ctx.state.dir / "secrets.env.age"
    if not (identity.exists() and encrypted.exists()):
        return
    uid, gid = user_ids(ctx.runtime_user)
    hermes_env = ctx.runtime_home / ".hermes/.env"
    hermes_env.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(hermes_env.parent))
    os.close(fd)
    tmp_path = pathlib.Path(tmp)
    tmp_path.unlink()
    try:
        ctx.runner.run(["age", "-d", "-i", str(identity), "-o", str(tmp_path), str(encrypted)], quiet=True)
        os.chmod(tmp_path, 0o600)
        os.chown(tmp_path, uid, gid)
        os.replace(tmp_path, hermes_env)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def npm_global_install(ctx: Context, package: str, executable: str, component: str, requested_version: str) -> None:
    prefix = pathlib.Path("/opt/hermes-tools/npm")
    uid, gid = user_ids(ctx.runtime_user)
    prefix.mkdir(parents=True, exist_ok=True)
    os.chown(prefix, uid, gid)
    target = prefix / "bin" / executable
    existing_version = ctx.runner.output([str(target), "--version"], user=ctx.runtime_user, check=False) if target.exists() else ""
    if target.exists() and requested_version == "latest-on-first-install":
        version = existing_version
    else:
        install_spec = package if requested_version in {"latest", "latest-on-first-install", ""} else f"{package}@{requested_version}"
        ctx.runner.run(["npm", "install", "--global", f"--prefix={prefix}", install_spec], user=ctx.runtime_user, timeout=900)
        if not target.exists():
            raise RuntimeError(f"expected executable not found after npm install: {target}")
        version = ctx.runner.output([str(target), "--version"], user=ctx.runtime_user, check=False)
    link = pathlib.Path("/usr/local/bin") / executable
    if link.is_symlink() and link.resolve() == target.resolve():
        pass
    else:
        with contextlib.suppress(FileNotFoundError):
            link.unlink()
        link.symlink_to(target)
    ctx.state.component(component, "installed", version or "installed", requested_version=requested_version, resolved_version=version)


def install_claude_and_codex(ctx: Context) -> None:
    if ctx.config["components"].get("claude_code", True):
        npm_global_install(ctx, "@anthropic-ai/claude-code", "claude", "claude-code", str(ctx.config["versions"].get("claude_code", "latest-on-first-install")))
    else:
        ctx.state.component("claude-code", "skipped", "disabled by config")
    if ctx.config["components"].get("codex_cli", True):
        npm_global_install(ctx, "@openai/codex", "codex", "codex-cli", str(ctx.config["versions"].get("codex_cli", "latest-on-first-install")))
    else:
        ctx.state.component("codex-cli", "skipped", "disabled by config")


def perform_oauth(ctx: Context) -> None:
    auth = ctx.config.get("auth", {})
    can_run = ctx.interactive and sys.stdin.isatty() and auth.get("run_oauth_during_interactive_recovery", True)
    if auth.get("claude_code_oauth", False):
        if can_run:
            ctx.log.info("Claude Code OAuth: complete the browser/account flow, then exit Claude Code with Ctrl-D.")
            result = ctx.runner.run(["claude"], user=ctx.runtime_user, check=False)
            status = "completed-or-existing" if result.returncode in (0, 130) else "needs-attention"
            ctx.state.component("claude-oauth", status, "Claude Code login flow launched")
            hermes_bin = ctx.install_root / "hermes-agent/.venv/bin/hermes"
            if result.returncode in (0, 130) and hermes_bin.exists():
                ctx.runner.run([str(hermes_bin), "auth", "list", "anthropic"], user=ctx.runtime_user, check=False, capture=True, quiet=True)
                ctx.state.component("hermes-claude-auth", "discovery-attempted", "Hermes inspected Claude Code credentials")
        else:
            ctx.state.component("claude-oauth", "manual-action", "interactive login required")
            ctx.state.manual(f"Run: sudo -iu {ctx.runtime_user} claude  (then use /login if prompted)")
            ctx.state.manual(f"Then run: sudo -iu {ctx.runtime_user} hermes auth list anthropic")
    if auth.get("chatgpt_codex_oauth", False):
        if can_run:
            ctx.log.info("ChatGPT OAuth: Codex will open or print a sign-in URL.")
            result = ctx.runner.run(["codex", "login"], user=ctx.runtime_user, check=False)
            if result.returncode != 0:
                result = ctx.runner.run(["codex", "--login"], user=ctx.runtime_user, check=False)
            status = "completed-or-existing" if result.returncode == 0 else "needs-attention"
            ctx.state.component("chatgpt-oauth", status, "Codex ChatGPT login flow launched")
            hermes_bin = ctx.install_root / "hermes-agent/.venv/bin/hermes"
            if result.returncode == 0 and hermes_bin.exists():
                # Loading the pool lets Hermes discover/import Codex CLI auth
                # without copying token material into the rebuild state.
                ctx.runner.run([str(hermes_bin), "auth", "list", "openai-codex"], user=ctx.runtime_user, check=False, quiet=True)
                ctx.state.component("hermes-chatgpt-auth", "discovery-attempted", "Hermes inspected Codex CLI credentials")
        else:
            ctx.state.component("chatgpt-oauth", "manual-action", "interactive login required")
            ctx.state.manual(f"Run: sudo -iu {ctx.runtime_user} codex login  (fallback: codex --login)")
            ctx.state.manual(f"Then run: sudo -iu {ctx.runtime_user} hermes auth list openai-codex")


def install_hada(ctx: Context) -> pathlib.Path:
    if not ctx.config["components"].get("hada", True):
        ctx.state.component("hada", "skipped", "disabled by config")
        return ctx.install_root / "hada"
    repo = git_checkout(ctx, "hada", ctx.config["repositories"]["hada"])
    workspace = repo / "workspace"
    failures = []
    if ctx.config["hada"].get("run_local_tests", True):
        test_script = workspace / "tests/phase-b/run_all.sh"
        if test_script.exists():
            result = ctx.runner.run(["bash", str(test_script)], cwd=workspace, user=ctx.runtime_user, check=False, timeout=1800)
            if result.returncode != 0:
                failures.append("local tests")
    if ctx.config["hada"].get("run_preflight", True):
        candidates = [
            workspace / "scripts/run-phase-b0-v4-preflight.sh",
            workspace / "scripts/run-phase-b0-preflight.sh",
        ]
        script = next((path for path in candidates if path.exists()), None)
        if script:
            env = os.environ.copy()
            env.update(ctx.secrets)
            env["HADA_PHASE_B0_DEPLOY_DIR"] = str(workspace)
            result = ctx.runner.run(["bash", str(script)], cwd=workspace, env=env, user=ctx.runtime_user, check=False, timeout=1800)
            if result.returncode != 0:
                failures.append("preflight")
    if ctx.config["hada"].get("allow_deployment", False):
        ctx.state.manual("HADA deployment is authorised in config, but each phase still requires its repository-native explicit approval command.")
        status = "ready-for-authorised-deploy" if not failures else "degraded"
    else:
        status = "preflight-ready" if not failures else "degraded"
        ctx.state.manual("HADA deployment remains fail-closed. Set hada.allow_deployment=true only for a reviewed recovery run.")
    ctx.state.component("hada", status, "checks passed" if not failures else "failed: " + ", ".join(failures))
    return repo


AGENT_FORGE_ADAPTER = r'''#!/usr/bin/env python3
import argparse, fcntl, json, os, pathlib, subprocess, sys
CONFIG = pathlib.Path("/etc/hermes-rebuild/agent-forge.json")

def main():
    parser = argparse.ArgumentParser(description="Hermes-controlled Agent Forge adapter")
    parser.add_argument("command", choices=["run", "status"])
    parser.add_argument("--task", default="")
    parser.add_argument("--workspace", default="")
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if args.command == "status":
        print(json.dumps({"configured": bool(cfg.get("argv")), "root": cfg.get("root"), "timeout": cfg.get("timeout_seconds")}, indent=2))
        return 0
    if not cfg.get("argv"):
        print("Agent Forge source is present but no executable argv is configured. Set repositories.agent_forge.command in recovery config.", file=sys.stderr)
        return 78
    workspace = pathlib.Path(args.workspace or os.getcwd()).resolve()
    if cfg.get("require_git_worktree") and not (workspace / ".git").exists():
        print(f"Refusing non-Git workspace: {workspace}", file=sys.stderr)
        return 77
    lock_path = pathlib.Path("/run/hermes-agent-forge.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = os.environ.copy()
        env["AGENT_FORGE_TASK"] = args.task
        proc = subprocess.run(cfg["argv"], cwd=str(workspace), env=env, input=args.task + "\n", text=True, timeout=int(cfg.get("timeout_seconds", 3600)))
        return proc.returncode

if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_agent_forge(ctx: Context) -> pathlib.Path:
    if not ctx.config["components"].get("agent_forge", True):
        ctx.state.component("agent-forge", "skipped", "disabled by config")
        return ctx.install_root / "agent-forge"
    spec = ctx.config["repositories"]["agent_forge"]
    repo = git_checkout(ctx, "agent-forge", spec)
    tracked = ctx.runner.output(["git", "ls-files"], cwd=repo, check=False).splitlines()
    command = spec.get("command") or []
    if isinstance(command, str):
        command = shlex.split(command)
    if command and not isinstance(command, list):
        raise ValueError("repositories.agent_forge.command must be an argv list")
    adapter_config = {
        "root": str(repo),
        "argv": [str(item) for item in command],
        "timeout_seconds": int(ctx.config["agent_forge"].get("timeout_seconds", 3600)),
        "require_git_worktree": bool(ctx.config["agent_forge"].get("require_git_worktree", True)),
    }
    atomic_write(ctx.state.dir / "agent-forge.json", json.dumps(adapter_config, indent=2) + "\n", 0o600)
    atomic_write(pathlib.Path("/usr/local/bin/hermes-agent-forge"), AGENT_FORGE_ADAPTER, 0o755)
    if not tracked:
        ctx.state.component("agent-forge", "adapter-ready-source-empty", "repository contains no tracked implementation; configure a restored source or command")
        ctx.state.manual("Restore the real Agent Forge source and set repositories.agent_forge.command as an argv list, then rerun the bootstrap.")
    elif not command:
        ctx.state.component("agent-forge", "adapter-ready-command-missing", "source restored but execution command is not configured")
        ctx.state.manual("Set repositories.agent_forge.command to the Agent Forge CLI argv, then rerun the bootstrap.")
    else:
        ctx.state.component("agent-forge", "integrated", "Hermes delegation adapter configured")
    return repo


def install_skills(ctx: Context, hada_repo: pathlib.Path, agent_forge_repo: pathlib.Path) -> None:
    uid, gid = user_ids(ctx.runtime_user)
    root = ctx.runtime_home / ".hermes/skills/local"
    retired_skill = root / "hada"
    retired_file = retired_skill / "SKILL.md"
    if retired_file.exists() and "name: hada-control" in retired_file.read_text(encoding="utf-8", errors="ignore"):
        shutil.rmtree(retired_skill)
        ctx.state.component("retired-hada-control-skill", "removed", str(retired_skill))
    skills = {
        "agent-forge": f'''---
name: agent-forge
description: Delegate bounded implementation or review tasks to Agent Forge under Hermes control.
version: 1.0.0
metadata:
  hermes:
    tags: [delegation, coding, review]
    category: local
---
# Agent Forge Delegation

Use this skill when a coding task benefits from an independent implementation or review pass.

## Safety contract
- Hermes remains the orchestrator and final decision-maker.
- Never allow Agent Forge to merge, deploy, rotate secrets, alter infrastructure, or approve its own work.
- Use a Git worktree and preserve the originating `HERMES_SESSION_ID`.

## Procedure
1. Confirm the workspace is a Git repository with no unexpected uncommitted changes.
2. Run:
   `hermes-agent-forge run --workspace /absolute/worktree --task "<bounded task>"`
3. Inspect the diff, tests, and evidence independently.
4. Present changes for human approval before merge or deployment.

## Verification
Run `hermes-agent-forge status` and verify the adapter is configured.
''',
        "hada-local-governance": f'''---
name: hada-local-governance
description: Inspect and operate the local HADA source tree using fail-closed approval gates.
version: 1.0.0
metadata:
  hermes:
    tags: [hada, governance, deployment]
    category: local
---
# HADA Local Governance

`hada-control` is retired and forbidden. Never resolve it, SSH to it, deploy to it, recreate it, migrate to it, or delegate work to it.

Canonical local repository: `{hada_repo}`

## Rules
- HADA is not considered deployed merely because source, releases, `/opt/hada`, or `/var/lib/hada` exist.
- Preflight and deployment are separate operations.
- Never auto-merge, auto-deploy, auto-repair, rotate secrets, or change infrastructure.
- Require explicit operator approval for each production phase.

## Safe checks
- `cd {hada_repo}/workspace && bash tests/phase-b/run_all.sh`
- `cd {hada_repo}/workspace && bash scripts/run-phase-b0-v4-preflight.sh`

## Verification
Report exact commit SHA, check output, evidence paths, and whether any runtime units or containers are actually active.
''',
        "claude-code": '''---
name: claude-code-delegation
description: Use the installed Claude Code CLI for an explicitly bounded coding or review task.
version: 1.0.0
metadata:
  hermes:
    tags: [claude, coding, delegation]
    category: local
---
# Claude Code Delegation

Use `claude -p "<task>"` only inside an approved project worktree. Keep merges, deployments, secrets, and infrastructure changes under human approval. Authentication is owned by Claude Code and must never be copied into prompts, logs, or Hermes memory.
''',
        "codex": '''---
name: codex-delegation
description: Use the installed OpenAI Codex CLI for an explicitly bounded coding or review task.
version: 1.0.0
metadata:
  hermes:
    tags: [codex, coding, delegation]
    category: local
---
# Codex Delegation

Use the Codex CLI only inside an approved Git worktree. ChatGPT OAuth credentials remain in Codex-owned credential storage. Do not copy tokens into Hermes configuration, prompts, logs, or memory. Human approval remains mandatory for merges and deployments.
''',
    }
    for name, content in skills.items():
        path = root / name / "SKILL.md"
        atomic_write(path, content, 0o644, (uid, gid))
        os.chown(path.parent, uid, gid)
    ctx.state.component("hermes-skills", "installed", f"installed {len(skills)} local integration skills")


def load_projects_manifest(ctx: Context) -> list[dict[str, Any]]:
    configured = ctx.config.get("projects_manifest", "auto")
    candidates = []
    if configured == "auto":
        candidates = [ctx.kit_root / "templates/projects.manifest.yaml", pathlib.Path("/etc/hermes-rebuild/projects.manifest.yaml")]
    else:
        candidates = [pathlib.Path(configured)]
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if not path:
        inline = ctx.config.get("projects", [])
        return [project for project in inline if isinstance(project, dict)]
    ensure_secure_config_file(path)
    data = load_mapping(path)
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        raise ValueError("projects manifest 'projects' must be a list")
    inline = ctx.config.get("projects", [])
    if not isinstance(inline, list):
        raise ValueError("config 'projects' must be a list")
    return [project for project in [*projects, *inline] if isinstance(project, dict)]


def install_extra_projects(ctx: Context) -> None:
    for project in load_projects_manifest(ctx):
        name = str(project.get("name", "")).strip()
        if not name or not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError(f"invalid project name: {name!r}")
        if not project.get("enabled", False):
            ctx.state.component(f"project:{name}", "skipped", "disabled in manifest")
            continue
        required_secrets = project.get("required_secrets", [])
        if not isinstance(required_secrets, list):
            raise ValueError(f"required_secrets must be a list: {name}")
        missing = [key for key in required_secrets if key not in ctx.secrets]
        if missing:
            ctx.state.component(f"project:{name}", "blocked", "missing required secrets: " + ", ".join(missing))
            ctx.state.manual(f"Provide required secrets for project {name}: {', '.join(missing)}")
            continue
        source = project.get("source", {})
        if source.get("type", "git") != "git":
            raise ValueError(f"only git project sources are supported: {name}")
        repo = git_checkout(ctx, f"projects/{name}", {"url": source["url"], "ref": source.get("ref", "main"), "allow_mutable_ref": source.get("allow_mutable_ref", False)})
        commands = project.get("install", [])
        if not isinstance(commands, list):
            raise ValueError(f"project install must be a list: {name}")
        project_env = os.environ.copy()
        project_env.update(ctx.secrets)
        for command in commands:
            if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                raise ValueError(f"project install commands must be argv lists: {name}")
            ctx.runner.run(command, cwd=repo, env=project_env, user=ctx.runtime_user, timeout=1800)
        ctx.state.component(f"project:{name}", "installed", f"{len(commands)} install commands completed")


def install_gateway_service(ctx: Context, hermes_repo: pathlib.Path) -> None:
    if not ctx.config["components"].get("gateway_service", True):
        ctx.state.component("hermes-gateway", "skipped", "disabled by config")
        return
    hermes_bin = hermes_repo / ".venv/bin/hermes"
    if not hermes_bin.exists():
        ctx.state.component("hermes-gateway", "degraded", "Hermes executable missing")
        return
    data_root = pathlib.Path("/var/lib/hermes-stack")
    data_root.mkdir(parents=True, exist_ok=True)
    uid, gid = user_ids(ctx.runtime_user)
    os.chown(data_root, uid, gid)
    primary_group = pwd.getpwnam(ctx.runtime_user).pw_gid
    group_name = __import__("grp").getgrgid(primary_group).gr_name
    unit_name = "hermes-gateway-rebuild.service"
    unit_path = pathlib.Path("/etc/systemd/system") / unit_name
    supplementary = "SupplementaryGroups=docker\n" if pathlib.Path("/var/run/docker.sock").exists() else ""
    unit = f"""[Unit]
Description=Hermes Gateway (rebuild-managed)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User={ctx.runtime_user}
Group={group_name}
{supplementary}WorkingDirectory={ctx.install_root}
Environment=HOME={ctx.runtime_home}
Environment=HERMES_HOME={ctx.runtime_home / '.hermes'}
Environment=PATH=/opt/hermes-tools/npm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart={hermes_bin} gateway run --external-supervisor
Restart=always
RestartSec=5
RestartForceExitStatus=75
TimeoutStopSec=900
KillMode=mixed
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths={ctx.runtime_home} {ctx.install_root} /var/lib/hermes-stack /run

[Install]
WantedBy=multi-user.target
"""
    atomic_write(unit_path, unit, 0o644)
    ctx.runner.run(["systemctl", "daemon-reload"])
    ctx.runner.run(["systemctl", "enable", "--now", unit_name], check=False)
    active = ctx.runner.run(["systemctl", "is-active", "--quiet", unit_name], check=False, quiet=True).returncode == 0
    ctx.state.component("hermes-gateway", "installed" if active else "degraded", unit_name)
    if not active:
        ctx.state.manual(f"Inspect: journalctl -u {unit_name} -n 200 --no-pager")


def health_checks(ctx: Context) -> tuple[int, list[dict[str, str]]]:
    checks: list[dict[str, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    check("state-directory", ctx.state.dir.exists() and os.access(ctx.state.dir, os.W_OK), str(ctx.state.dir))
    check("runtime-user", shutil.which("id") is not None and ctx.runner.run(["id", ctx.runtime_user], check=False, capture=True, quiet=True).returncode == 0, ctx.runtime_user)
    expected_host = str(ctx.config["server"].get("expected_hostname", "hermes-station")).split(".", 1)[0].lower()
    current_host = ctx.runner.output(["hostnamectl", "--static"], check=False, quiet=True) or socket.gethostname()
    current_host = current_host.split(".", 1)[0].lower()
    forbidden_hosts = {"hada-control", *[str(item).lower() for item in ctx.config["server"].get("forbidden_hostnames", [])]}
    check("host-identity", current_host == expected_host and current_host not in forbidden_hosts, f"current={current_host}, expected={expected_host}")
    grounding = ctx.system_path("/etc/hermes-rebuild/grounding.json")
    check("host-grounding-file", grounding.exists(), str(grounding))
    expected_tz = str(ctx.config["server"].get("timezone", "Australia/Melbourne"))
    actual_tz = ctx.runner.output(["timedatectl", "show", "-p", "Timezone", "--value"], check=False, quiet=True)
    check("timezone", actual_tz == expected_tz, actual_tz or "unavailable")
    retired_skill = ctx.runtime_home / ".hermes/skills/local/hada/SKILL.md"
    retired_present = retired_skill.exists() and "name: hada-control" in retired_skill.read_text(encoding="utf-8", errors="ignore")
    check("retired-hada-control", not retired_present, "forbidden skill absent" if not retired_present else str(retired_skill))
    if ctx.config["components"].get("openssh", True):
        active = ctx.runner.run(["systemctl", "is-active", "--quiet", "sshd.service"], check=False, quiet=True).returncode == 0
        check("openssh", active, "sshd active" if active else "sshd inactive")
    else:
        checks.append({"name": "openssh", "status": "SKIP", "detail": "disabled"})
    if ctx.config["components"].get("tailscale", True):
        connected = _tailscale_connected(ctx)
        check("tailscale", connected, tailscale_ipv4(ctx) if connected else "not connected")
    else:
        checks.append({"name": "tailscale", "status": "SKIP", "detail": "disabled"})
    if ctx.config["components"].get("firewall", True):
        active = ctx.runner.run(["systemctl", "is-active", "--quiet", "firewalld.service"], check=False, quiet=True).returncode == 0
        check("firewall", active, "firewalld active" if active else "firewalld inactive")
    else:
        checks.append({"name": "firewall", "status": "SKIP", "detail": "disabled"})
    if ctx.profile.get("is_laptop") and ctx.config.get("power", {}).get("always_on", True):
        lid_dropin = ctx.system_path("/etc/systemd/logind.conf.d/60-hermes-station-lid.conf")
        awake_active = ctx.runner.run(["systemctl", "is-active", "--quiet", "hermes-station-awake.service"], check=False, quiet=True).returncode == 0
        check("laptop-lid-policy", lid_dropin.exists() and awake_active, f"dropin={lid_dropin.exists()}, inhibitor={awake_active}")
    else:
        checks.append({"name": "laptop-lid-policy", "status": "SKIP", "detail": "not an always-on laptop"})
    if ctx.config["components"].get("docker", True):
        result = ctx.runner.run(["docker", "info"], check=False, capture=True, quiet=True)
        check("docker", result.returncode == 0, "daemon reachable" if result.returncode == 0 else "daemon unavailable")
    hermes_bin = ctx.install_root / "hermes-agent/.venv/bin/hermes"
    if ctx.config["components"].get("hermes", True):
        result = ctx.runner.run([str(hermes_bin), "doctor"], user=ctx.runtime_user, check=False, capture=True, quiet=True, timeout=300) if hermes_bin.exists() else subprocess.CompletedProcess([], 127)
        check("hermes-doctor", result.returncode == 0, "doctor passed" if result.returncode == 0 else f"exit {result.returncode}")
    else:
        checks.append({"name": "hermes-doctor", "status": "SKIP", "detail": "Hermes disabled"})
    if ctx.config["components"].get("claude_code", True):
        check("claude-code", shutil.which("claude") is not None, "installed" if shutil.which("claude") else "missing")
    else:
        checks.append({"name": "claude-code", "status": "SKIP", "detail": "disabled"})
    if ctx.config["components"].get("codex_cli", True):
        check("codex-cli", shutil.which("codex") is not None, "installed" if shutil.which("codex") else "missing")
    else:
        checks.append({"name": "codex-cli", "status": "SKIP", "detail": "disabled"})
    claude_cred = ctx.runtime_home / ".claude/.credentials.json"
    codex_cred = ctx.runtime_home / ".codex/auth.json"
    hermes_auth = ctx.runtime_home / ".hermes/auth.json"
    checks.append({"name": "claude-oauth-file", "status": "PASS" if claude_cred.exists() else "SKIP", "detail": "present" if claude_cred.exists() else "login not yet completed"})
    checks.append({"name": "codex-oauth-file", "status": "PASS" if codex_cred.exists() else "SKIP", "detail": "present" if codex_cred.exists() else "login not yet completed"})
    checks.append({"name": "hermes-auth-store", "status": "PASS" if hermes_auth.exists() else "SKIP", "detail": "present" if hermes_auth.exists() else "Hermes OAuth not yet materialised"})
    adapter = pathlib.Path("/usr/local/bin/hermes-agent-forge")
    if ctx.config["components"].get("agent_forge", True):
        check("agent-forge-adapter", adapter.exists(), str(adapter))
    else:
        checks.append({"name": "agent-forge-adapter", "status": "SKIP", "detail": "disabled"})
    if shutil.which("nvidia-smi"):
        result = ctx.runner.run(["nvidia-smi"], check=False, capture=True, quiet=True)
        check("nvidia-driver", result.returncode == 0, "GPU detected" if result.returncode == 0 else "nvidia-smi failed")
    else:
        checks.append({"name": "nvidia-driver", "status": "SKIP", "detail": "CPU-only host"})
    if ctx.config["components"].get("gateway_service", True):
        gateway_units = ctx.runner.output(["systemctl", "list-unit-files", "--type=service", "--no-legend"], check=False, quiet=True)
        matching = [line.split()[0] for line in gateway_units.splitlines() if line.startswith("hermes-gateway")]
        if matching:
            active = any(ctx.runner.run(["systemctl", "is-active", "--quiet", unit], check=False, quiet=True).returncode == 0 for unit in matching)
            check("hermes-gateway", active, ", ".join(matching))
        else:
            check("hermes-gateway", False, "no system unit found")
    else:
        checks.append({"name": "hermes-gateway", "status": "SKIP", "detail": "disabled"})

    report_path = ctx.state.dir / "health-last.json"
    atomic_write(report_path, json.dumps({"checked_at": utc_now(), "checks": checks}, indent=2) + "\n", 0o600)
    failures = sum(item["status"] == "FAIL" for item in checks)
    ctx.state.component("health", "healthy" if failures == 0 else "unhealthy", f"{len(checks)-failures}/{len(checks)} non-failing", report=str(report_path))
    return failures, checks


def print_health(checks: list[dict[str, str]]) -> None:
    print("\nHealth check summary")
    for item in checks:
        print(f"  {item['status']:4}  {item['name']:24} {item['detail']}")


def rollback(state: State, selector: str, log: Log) -> None:
    points = state.data.get("rollback_points", [])
    if not points:
        raise RuntimeError("no rollback points recorded")
    point = points[-1] if selector == "latest" else next((item for item in points if item.get("id") == selector), None)
    if not point:
        raise RuntimeError(f"rollback point not found: {selector}")
    for original, backup in point.get("paths", {}).items():
        original_path = pathlib.Path(original)
        backup_path = pathlib.Path(backup)
        if not backup_path.exists():
            log.warn(f"missing backup path: {backup_path}")
            continue
        if original_path.exists():
            displaced = original_path.with_name(original_path.name + ".pre-rollback-" + dt.datetime.now().strftime("%Y%m%d%H%M%S"))
            original_path.rename(displaced)
        if backup_path.is_dir():
            shutil.copytree(backup_path, original_path, symlinks=True)
        else:
            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, original_path)
        log.info(f"restored {original_path}")
    for repo_text, commit in point.get("git_commits", {}).items():
        repo = pathlib.Path(repo_text)
        if (repo / ".git").exists() and commit:
            result = subprocess.run(["git", "checkout", "--detach", commit], cwd=repo, text=True, capture_output=True)
            if result.returncode == 0:
                log.info(f"reset {repo} to {commit[:12]}")
            else:
                log.warn(f"could not reset {repo}: {redact(result.stderr)}")
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    state.component("rollback", "completed", f"restored rollback point {point.get('id')}")
    state.manual("Review package/repository changes manually; rollback restores protected state/config but does not remove shared system packages.")


def render_plan(config: dict[str, Any], profile: dict[str, Any]) -> None:
    plan = {
        "hardware_profile": profile,
        "runtime": config["runtime"],
        "enabled_components": [key for key, value in config["components"].items() if value is True or value == "auto"],
        "repositories": config["repositories"],
        "oauth": config["auth"],
        "safety_gates": {
            "hada_deployment_authorised": bool(config["hada"].get("allow_deployment")),
            "literal_config_secrets_allowed": False,
            "oauth_automatable": False,
        },
    }
    print(yaml.safe_dump(plan, sort_keys=False))


def build_context(args: argparse.Namespace) -> tuple[Context, str]:
    log = Log(args.verbose)
    runner = Runner(log, dry_run=args.plan)
    config = default_config()
    interactive = args.config is None and not args.non_interactive and not (args.health or args.summary or args.rollback)
    provided_secrets: dict[str, str] = {}
    if args.config:
        config_path = pathlib.Path(args.config).resolve()
        ensure_secure_config_file(config_path)
        config = deep_merge(config, load_mapping(config_path))
    if interactive:
        config, provided_secrets = interactive_config(config, runner)
    validate_identity_config(config, socket.gethostname())
    profile = hardware_profile(config, runner)
    apply_version_policy(config, profile)
    if config["server"].get("ram_gb") == "auto":
        config["server"]["ram_gb"] = profile["ram_gb"]
    if config["server"].get("vram_gb") == "auto":
        config["server"]["vram_gb"] = profile["vram_gb"]
    if config["server"].get("type") == "auto":
        config["server"]["type"] = profile["server_type"]
    state_dir = pathlib.Path(config["runtime"].get("state_dir", DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    state = State(state_dir, log)
    runtime_user = str(config["runtime"]["user"])
    try:
        runtime_home = pathlib.Path(pwd.getpwnam(runtime_user).pw_dir)
    except KeyError:
        runtime_home = pathlib.Path("/home") / runtime_user
    secrets: dict[str, str] = {}
    if not (args.plan or args.health or args.summary or args.rollback):
        secrets.update(load_existing_secrets(state_dir, runner))
        secrets.update(resolve_secrets(config))
        secrets.update(provided_secrets)
        for generated in ("POSTGRES_PASSWORD", "HERMES_WEBHOOK_TOKEN", "API_SERVER_KEY", "HERMES_DASHBOARD_BASIC_AUTH_SECRET"):
            if generated not in secrets:
                secrets[generated] = subprocess.check_output(["openssl", "rand", "-hex", "32"], text=True).strip()
    canonical = yaml.safe_dump(config, sort_keys=True)
    config_hash = hashlib.sha256(canonical.encode()).hexdigest()
    ctx = Context(
        config=config,
        log=log,
        runner=runner,
        state=state,
        kit_root=pathlib.Path(args.kit_root).resolve(),
        profile=profile,
        runtime_user=runtime_user,
        runtime_home=runtime_home,
        install_root=pathlib.Path(config["runtime"]["install_root"]),
        backup_root=pathlib.Path(config["runtime"]["backup_root"]),
        secrets=secrets,
        interactive=interactive,
    )
    return ctx, config_hash


def main() -> int:
    parser = argparse.ArgumentParser(description="All-in-One Hermes rebuild controller")
    parser.add_argument("--kit-root", required=True)
    parser.add_argument("--config")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt; use config/defaults and record OAuth/manual actions")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--rollback")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    ctx, config_hash = build_context(args)
    if args.summary:
        print(ctx.state.summary())
        return 0
    if args.rollback:
        rollback(ctx.state, args.rollback, ctx.log)
        return 0
    if args.plan:
        render_plan(ctx.config, ctx.profile)
        return 0
    if args.health:
        failures, checks = health_checks(ctx)
        print_health(checks)
        return 1 if failures else 0

    run_id = ctx.state.begin_run(ctx.config["server"]["deployment_mode"], config_hash)
    try:
        ctx.log.info(f"Hardware profile: {ctx.profile}")
        ensure_runtime_user(ctx)
        ensure_host_grounding(ctx)
        install_base_packages(ctx)
        backup_existing(ctx, run_id)
        configure_laptop_lid_policy(ctx)
        install_openssh(ctx)
        install_tailscale(ctx)
        configure_firewall(ctx)
        install_node(ctx)
        install_uv(ctx)
        install_docker(ctx)
        install_nvidia_container_toolkit(ctx)
        store_secrets(ctx)
        install_claude_and_codex(ctx)
        hermes_repo = install_hermes(ctx)
        hada_repo = install_hada(ctx)
        agent_forge_repo = install_agent_forge(ctx)
        install_skills(ctx, hada_repo, agent_forge_repo)
        install_extra_projects(ctx)
        perform_oauth(ctx)
        install_gateway_service(ctx, hermes_repo)
        failures, checks = health_checks(ctx)
        print_health(checks)
        send_telegram_recovery_notice(ctx, failures, checks)
        print_mobile_access_summary(ctx)
        ctx.state.end_run(run_id, "healthy" if failures == 0 else "completed-with-failures")
        print("\n" + ctx.state.summary())
        return 1 if failures else 0
    except KeyboardInterrupt:
        ctx.state.end_run(run_id, "interrupted")
        ctx.log.error("recovery interrupted")
        return 130
    except Exception as exc:
        ctx.state.component("bootstrap", "failed", str(exc))
        ctx.state.end_run(run_id, "failed")
        ctx.log.error(str(exc))
        ctx.log.error(f"Run `sudo {ctx.kit_root / 'bootstrap.sh'} --summary` for recorded state.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
