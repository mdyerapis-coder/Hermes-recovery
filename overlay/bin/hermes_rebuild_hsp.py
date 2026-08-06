#!/usr/bin/env python3
"""Run the Hermes rebuild controller with session-only Bitwarden secrets."""
from __future__ import annotations

import argparse
import fcntl
import os
import pathlib
import pty
import select
import signal
import shutil
import subprocess
import sys
import termios


PROMPT_ENV = {
    "OpenAI API key": "OPENAI_API_KEY",
    "Anthropic API key": "ANTHROPIC_API_KEY",
    "OpenRouter API key": "OPENROUTER_API_KEY",
    "Hugging Face token": "HF_TOKEN",
    "GitHub token for private/rate-limited repository access": "GITHUB_TOKEN",
    "Telegram bot token": "TELEGRAM_BOT_TOKEN",
    "Telegram allowed user IDs": "TELEGRAM_ALLOWED_USERS",
    "Tailscale one-off or reusable auth key": "TAILSCALE_AUTH_KEY",
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes Secret Provider recovery wrapper")
    parser.add_argument("--kit-root", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--hsp-manifest")
    parser.add_argument("controller_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.controller_args and args.controller_args[0] == "--":
        args.controller_args = args.controller_args[1:]
    return args


def bypass_hsp(controller_args: list[str]) -> bool:
    passive = {"-h", "--help", "--health", "--summary", "--rollback", "--plan"}
    return os.environ.get("HERMES_HSP_DISABLE") == "1" or (
        os.environ.get("HERMES_HSP_FORCE") != "1" and any(arg in passive for arg in controller_args)
    )


def _copy_window_size(master_fd: int) -> None:
    if not sys.stdin.isatty():
        return
    try:
        size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def run_interactive(command: list[str], env: dict[str, str]) -> int:
    """Relay a PTY and inject resolved values only at known secret prompts."""
    pid, master_fd = pty.fork()
    if pid == 0:
        os.execve(command[0], command, env)

    injected: set[str] = set()
    window = ""
    old_settings = termios.tcgetattr(sys.stdin.fileno()) if sys.stdin.isatty() else None
    previous_winch = signal.getsignal(signal.SIGWINCH)

    def resize(_signum: int | None = None, _frame: object | None = None) -> None:
        _copy_window_size(master_fd)

    try:
        resize()
        signal.signal(signal.SIGWINCH, resize)
        if old_settings is not None:
            tty_settings = termios.tcgetattr(sys.stdin.fileno())
            tty_settings[3] &= ~(termios.ICANON | termios.ECHO)
            tty_settings[6][termios.VMIN] = 1
            tty_settings[6][termios.VTIME] = 0
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tty_settings)

        while True:
            readers = [master_fd]
            if sys.stdin.isatty():
                readers.append(sys.stdin.fileno())
            ready, _, _ = select.select(readers, [], [])
            if master_fd in ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                window = (window + data.decode("utf-8", errors="ignore"))[-8192:]
                for prompt, env_name in PROMPT_ENV.items():
                    if prompt in injected:
                        continue
                    value = env.get(env_name)
                    if value and prompt in window:
                        os.write(master_fd, value.encode("utf-8") + b"\n")
                        injected.add(prompt)
                        window = ""
                        break
            if sys.stdin.isatty() and sys.stdin.fileno() in ready:
                data = os.read(sys.stdin.fileno(), 1024)
                if not data:
                    break
                os.write(master_fd, data)
    finally:
        if old_settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, old_settings)
        signal.signal(signal.SIGWINCH, previous_winch)
        try:
            os.close(master_fd)
        except OSError:
            pass

    _, status = os.waitpid(pid, 0)
    return _exit_code(status)


def run_controller(command: list[str], env: dict[str, str], controller_args: list[str]) -> int:
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and "--non-interactive" not in controller_args
    if interactive:
        return run_interactive(command, env)
    return subprocess.run(command, env=env, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    kit_root = pathlib.Path(args.kit_root).resolve()
    controller = pathlib.Path(args.controller).resolve()
    command = [sys.executable, str(controller), "--kit-root", str(kit_root), *args.controller_args]

    if bypass_hsp(args.controller_args):
        return run_controller(command, os.environ.copy(), args.controller_args)

    if shutil.which("bw") is None and os.environ.get("HERMES_HSP_INSTALL_BW", "1") != "0":
        installer = kit_root / "bin" / "install_bw_cli.py"
        try:
            subprocess.run([sys.executable, str(installer)], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR: unable to install verified Bitwarden CLI: {exc}", file=sys.stderr)
            return 69

    sys.path.insert(0, str(kit_root / "lib"))
    from hermes_hsp import BitwardenProvider, HSPError, load_manifest, resolve_environment

    manifest_path = pathlib.Path(
        args.hsp_manifest
        or os.environ.get("HERMES_HSP_MANIFEST", "")
        or kit_root / "manifests" / "secrets.json"
    )
    if not manifest_path.is_file():
        print(f"HSP: manifest not found; continuing without Bitwarden: {manifest_path}", file=sys.stderr)
        return run_controller(command, os.environ.copy(), args.controller_args)

    config = load_manifest(manifest_path)
    provider = BitwardenProvider(config)
    try:
        child_env, events = resolve_environment(config, os.environ.copy(), provider)
        print("Hermes Secret Provider")
        for event in events:
            print(f"  {event.status:<8} {event.name:<24} {event.detail}")
        return run_controller(command, child_env, args.controller_args)
    except HSPError as exc:
        print(f"ERROR: Hermes Secret Provider: {exc}", file=sys.stderr)
        return 78
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
