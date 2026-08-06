#!/usr/bin/env python3
"""End-to-end recovery policy tests without modifying the host.

The test exercises the integrated grounding, laptop, SSH, Tailscale, firewall,
health and mobile loader paths against an isolated root filesystem. External
commands are represented by a deterministic fake systemd/Tailscale host.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import pwd
import shutil
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin/hermes_rebuild.py"
spec = importlib.util.spec_from_file_location("hermes_rebuild_e2e", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["hermes_rebuild_e2e"] = module
spec.loader.exec_module(module)


class FakeRunner:
    def __init__(self) -> None:
        self.hostname = "fedora"
        self.timezone = "UTC"
        self.tailscale_connected = False
        self.tailscale_ip = "100.109.135.10"
        self.commands: list[list[str]] = []
        self.active_services = {
            "sshd.service",
            "firewalld.service",
            "tailscaled.service",
            "hermes-station-awake.service",
        }

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.commands.append(command)
        stdout = ""
        stderr = ""
        rc = 0
        if command[:2] == ["hostnamectl", "set-hostname"]:
            self.hostname = command[2]
        elif command == ["hostnamectl", "--static"]:
            stdout = self.hostname + "\n"
        elif command[:2] == ["timedatectl", "set-timezone"]:
            self.timezone = command[2]
        elif command[:4] == ["timedatectl", "show", "-p", "Timezone"]:
            stdout = self.timezone + "\n"
        elif command[:3] == ["systemctl", "is-active", "--quiet"]:
            rc = 0 if command[3] in self.active_services else 3
        elif command[:3] == ["tailscale", "status", "--json"]:
            if self.tailscale_connected:
                stdout = json.dumps({"BackendState": "Running"})
            else:
                rc = 1
                stdout = json.dumps({"BackendState": "NeedsLogin"})
        elif command[:3] == ["tailscale", "ip", "-4"]:
            if self.tailscale_connected:
                stdout = self.tailscale_ip + "\n"
            else:
                rc = 1
        elif command[:2] == ["tailscale", "up"]:
            self.tailscale_connected = True
        elif command[:2] == ["tailscale", "set"]:
            self.tailscale_connected = True
        elif command[:2] == ["shred", "-u"]:
            pathlib.Path(command[2]).unlink(missing_ok=True)
        elif command and command[0] == "id":
            stdout = "uid=1000(test) gid=1000(test)\n"
        elif command and command[0] == "sshd":
            rc = 0
        elif command[:2] == ["docker", "info"]:
            rc = 0
        return subprocess.CompletedProcess(command, rc, stdout, stderr)

    def output(self, argv: list[str], **kwargs: Any) -> str:
        return (self.run(argv, **kwargs).stdout or "").strip()


def digest_tree(root: pathlib.Path) -> str:
    h = hashlib.sha256()
    ignored = {"hermes-rebuild/state.json", "hermes-rebuild/health-last.json"}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in ignored:
            continue
        h.update(relative.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def make_context(temp: pathlib.Path) -> tuple[Any, FakeRunner]:
    current = pwd.getpwuid(os.getuid())
    runtime_home = temp / "home" / current.pw_name
    runtime_home.mkdir(parents=True)
    state_dir = temp / "etc/hermes-rebuild"
    config = module.default_config()
    config["runtime"].update({
        "user": current.pw_name,
        "state_dir": str(state_dir),
        "install_root": str(temp / "opt/hermes-stack"),
        "backup_root": str(temp / "var/backups/hermes-rebuild"),
    })
    config["server"].update({
        "expected_hostname": "hermes-station",
        "allow_hostname_change": True,
        "timezone": "Australia/Melbourne",
        "ram_gb": 16,
        "vram_gb": 0,
        "type": "bare-metal",
    })
    config["network"]["tailscale"]["operator"] = current.pw_name
    for key in ("docker", "hermes", "hada", "agent_forge", "claude_code", "codex_cli", "gateway_service"):
        config["components"][key] = False
    # Build a fake laptop root.
    (temp / "sys/class/dmi/id").mkdir(parents=True)
    (temp / "sys/class/dmi/id/chassis_type").write_text("9\n")
    (temp / "sys/class/dmi/id/product_name").write_text("Hermes Test Laptop\n")
    runner = FakeRunner()
    state = module.State(state_dir, module.Log(False))
    profile = {
        "server_type": "bare-metal",
        "is_laptop": True,
        "chassis_type": 9,
        "product_name": "Hermes Test Laptop",
        "portable_evidence": ["dmi-chassis"],
        "ram_gb": 16,
        "vram_gb": 0,
        "memory_class": "standard",
        "gpu_class": "cpu-only",
        "local_model_class": "remote-only",
    }
    ctx = module.Context(
        config=config,
        log=module.Log(False),
        runner=runner,
        state=state,
        kit_root=ROOT,
        profile=profile,
        runtime_user=current.pw_name,
        runtime_home=runtime_home,
        install_root=temp / "opt/hermes-stack",
        backup_root=temp / "var/backups/hermes-rebuild",
        secrets={
            "TAILSCALE_AUTH_KEY": "tskey-auth-test-secret-do-not-log",
            "SSH_AUTHORIZED_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey hermes-mobile",
        },
        interactive=False,
        system_root=temp,
    )
    return ctx, runner


def test_integrated_recovery_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-e2e-root-") as raw:
        temp = pathlib.Path(raw)
        ctx, runner = make_context(temp)
        module.ensure_host_grounding(ctx)
        module.configure_laptop_lid_policy(ctx)
        module.install_openssh(ctx)
        module.install_tailscale(ctx)
        module.configure_firewall(ctx)

        assert runner.hostname == "hermes-station"
        assert runner.timezone == "Australia/Melbourne"
        grounding = temp / "etc/hermes-rebuild/grounding.json"
        assert grounding.exists()
        ground = json.loads(grounding.read_text())
        assert ground["hostname"] == "hermes-station"
        assert "hada-control" in ground["forbidden_hosts"]
        assert ground["safeguards"]["hada_source_is_not_deployment"] is True

        lid = temp / "etc/systemd/logind.conf.d/60-hermes-station-lid.conf"
        assert "HandleLidSwitch=ignore" in lid.read_text()
        inhibitor = temp / "etc/systemd/system/hermes-station-awake.service"
        assert "handle-lid-switch" in inhibitor.read_text()
        assert "sleep:idle" in inhibitor.read_text()

        auth = ctx.runtime_home / ".ssh/authorized_keys"
        assert auth.read_text().count("ssh-ed25519") == 1
        flattened = "\n".join(" ".join(cmd) for cmd in runner.commands)
        assert "tskey-auth-test-secret-do-not-log" not in flattened
        assert "--auth-key=file:" in flattened
        assert not (ctx.state.dir / ".tailscale-auth-key").exists()
        assert "TAILSCALE_AUTH_KEY" not in ctx.secrets
        assert "SSH_AUTHORIZED_KEY" not in ctx.secrets

        first = digest_tree(temp / "etc")
        module.ensure_host_grounding(ctx)
        module.configure_laptop_lid_policy(ctx)
        module.install_openssh(ctx)
        module.install_tailscale(ctx)
        module.configure_firewall(ctx)
        second = digest_tree(temp / "etc")
        assert first == second, "idempotent rerun changed managed configuration"
        assert auth.read_text().count("ssh-ed25519") == 1

        failures, checks = module.health_checks(ctx)
        assert failures == 0, checks
        names = {item["name"]: item["status"] for item in checks}
        assert names["host-identity"] == "PASS"
        assert names["timezone"] == "PASS"
        assert names["tailscale"] == "PASS"
        assert names["laptop-lid-policy"] == "PASS"
        assert names["retired-hada-control"] == "PASS"


def test_fail_closed_identity_rules() -> None:
    config = module.default_config()
    for bad in ("hada-control", "hada-control.example"):
        with contextlib.suppress(Exception):
            pass
        try:
            module.validate_identity_config(config, bad)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"forbidden current host accepted: {bad}")
    config["server"]["expected_hostname"] = "hada-control"
    try:
        module.validate_identity_config(config, "fedora")
    except RuntimeError:
        pass
    else:
        raise AssertionError("forbidden target hostname accepted")
    config = module.default_config()
    config["repositories"]["hada"]["url"] = "ssh://hada-control/opt/hada.git"
    try:
        module.validate_identity_config(config, "fedora")
    except RuntimeError:
        pass
    else:
        raise AssertionError("retired host repository target accepted")


def write_manifest(source: pathlib.Path) -> None:
    entries = []
    for path in sorted(p for p in source.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {path.relative_to(source).as_posix()}")
    (source / "MANIFEST.sha256").write_text("\n".join(entries) + "\n")


def make_archive(source: pathlib.Path, destination: pathlib.Path, corrupt: bool = False) -> None:
    staged = destination.parent / ("corrupt-source" if corrupt else "good-source")
    shutil.copytree(source, staged)
    write_manifest(staged)
    if corrupt:
        with (staged / "bootstrap.sh").open("a") as handle:
            handle.write("\n# corruption after manifest\n")
    with tarfile.open(destination, "w:gz") as tf:
        tf.add(staged, arcname="hermes-rebuild-kit")


def test_mobile_loader_download_and_verification() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-loader-e2e-") as raw:
        temp = pathlib.Path(raw)
        webroot = temp / "web"
        webroot.mkdir()
        good = webroot / "good.tar.gz"
        bad = webroot / "bad.tar.gz"
        good_sums = webroot / "good-SHA256SUMS"
        bad_sums = webroot / "bad-SHA256SUMS"
        make_archive(ROOT, good, False)
        make_archive(ROOT, bad, True)
        archive_name = "hermes-rebuild-kit-v1.2.0.tar.gz"
        good_sums.write_text(f"{hashlib.sha256(good.read_bytes()).hexdigest()}  {archive_name}\n")
        bad_sums.write_text(f"{hashlib.sha256(bad.read_bytes()).hexdigest()}  {archive_name}\n")
        shutil.copy2(good_sums, webroot / "SHA256SUMS")
        payload = webroot / "payload"
        payload.mkdir()
        encoded = base64.b64encode(good.read_bytes()).decode("ascii")
        part_names = []
        for index, start in enumerate(range(0, len(encoded), 257)):
            name = f"{archive_name}.b64.part-{index:03d}"
            (payload / name).write_text(encoded[start:start + 257])
            part_names.append(f"payload/{name}")
        (payload / "parts.txt").write_text("\n".join(part_names) + "\n")

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

        handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(webroot), **kwargs)
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            config = temp / "plan.yaml"
            state_dir = temp / "state"
            current = pwd.getpwuid(os.getuid()).pw_name
            config.write_text(
                "server:\n"
                "  expected_hostname: hermes-station\n"
                "  allow_hostname_change: true\n"
                "  ram_gb: 8\n  vram_gb: 0\n  type: bare-metal\n"
                f"runtime:\n  user: {current}\n  state_dir: {state_dir}\n"
                f"  install_root: {temp / 'stack'}\n  backup_root: {temp / 'backups'}\n"
            )
            config.chmod(0o600)
            env = os.environ.copy()
            env.update({
                "HERMES_RECOVERY_BASE_URL": f"http://127.0.0.1:{port}",
                "HERMES_RECOVERY_INSTALL_BASE": str(temp / "installed"),
                "HERMES_RECOVERY_ALLOW_INSECURE_TEST_URL": "1",
                "HERMES_RECOVERY_ALLOW_UNPRIVILEGED_TEST": "1",
            })
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--config", str(config), "--non-interactive", "--plan"],
                text=True,
                capture_output=True,
                env=env,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Verified Hermes recovery kit" in result.stdout
            assert (temp / "installed/current/bootstrap.sh").exists()

            env["HERMES_RECOVERY_ARCHIVE_URL"] = f"http://127.0.0.1:{port}/bad.tar.gz"
            env["HERMES_RECOVERY_CHECKSUM_URL"] = f"http://127.0.0.1:{port}/bad-SHA256SUMS"
            bad_result = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--config", str(config), "--non-interactive", "--plan"],
                text=True,
                capture_output=True,
                env=env,
            )
            assert bad_result.returncode != 0
            assert "FAILED" in (bad_result.stdout + bad_result.stderr)
            server.shutdown()


def main() -> None:
    test_integrated_recovery_policy()
    test_fail_closed_identity_rules()
    test_mobile_loader_download_and_verification()
    print("Hermes rebuild E2E tests: PASS")


if __name__ == "__main__":
    main()
