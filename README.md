# Hermes Station Disaster-Recovery Bootstrap

Mobile-first, production-oriented recovery tooling for rebuilding a clean
`hermes-station` Fedora/Ubuntu host with Hermes, HADA, Agent Forge delegation,
Tailscale, OpenSSH, Docker, Claude Code OAuth and ChatGPT/Codex OAuth.

## Start from a clean Fedora installation

From the new Hermes Station terminal:

```bash
curl -fsSLO https://raw.githubusercontent.com/mdyerapis-coder/Hermes-recovery/main/install.sh
chmod +x install.sh
sudo ./install.sh
```

The loader downloads the versioned kit, verifies the public SHA-256 checksum,
verifies the archive's internal per-file manifest, installs it under
`/opt/hermes-recovery-kit`, and launches the interactive recovery controller.
Do not use `curl | sudo bash`; download, inspect and execute the loader.

## Recovery scope

- Enforces the target identity `hermes-station` and timezone
  `Australia/Melbourne`.
- Rejects the retired hostname `hada-control` in host and repository targets.
- Installs and configures Tailscale, including Tailscale SSH.
- Installs OpenSSH safely without disabling password access until key access is
  deliberately configured and validated.
- Configures `firewalld`; Tailscale-only SSH restriction is applied only after
  Tailscale is confirmed connected, avoiding remote lockout.
- Detects laptop chassis hardware. When always-on mode is enabled, it installs
  both a `systemd-logind` lid policy and a persistent inhibitor service so
  closing the lid does not suspend or stop Hermes Station.
- Restores Hermes from the configured pinned Git commit and runs `hermes doctor`.
- Restores HADA source and evidence while preserving its fail-closed deployment
  and approval gates.
- Installs Agent Forge as a Hermes-controlled subordinate adapter. It receives
  no merge, deployment, secret-rotation or infrastructure authority.
- Installs Claude Code and starts its human OAuth login during interactive
  recovery.
- Installs OpenAI Codex and starts ChatGPT OAuth during interactive recovery.
- Supports Telegram-first Hermes gateway configuration.
- Tracks state and rollback points under `/etc/hermes-rebuild`.
- Encrypts stored secrets with `age`; OAuth credentials remain in the native
  Claude Code/Codex credential stores and are never copied into rebuild state.

## Host grounding and safeguards

The controller writes persistent machine grounding that records:

- hostname, runtime user, server type and timezone;
- the role of the machine as Hermes Station;
- `hada-control` as retired and forbidden;
- HADA source as distinct from a deployed HADA service;
- mandatory human approval for merges, deployments, repairs, secrets and
  infrastructure changes;
- Agent Forge as subordinate to Hermes rather than an autonomous authority.

Grounding and health checks fail closed when the current or requested target is
forbidden. A clean Fedora temporary hostname may be changed to
`hermes-station` only when `allow_hostname_change` is enabled.

## Tailscale setup

Interactive recovery installs Tailscale and prints the login URL. Open it from
your phone and approve the new `hermes-station` device.

Unattended recovery may reference a one-use auth key:

```yaml
secrets:
  TAILSCALE_AUTH_KEY: env?:TAILSCALE_AUTH_KEY
```

The key is passed through a temporary root-only file and shredded after use. It
is not written into the state manifest or normal logs.

## Laptop lid protection

On detected laptops, the default production configuration writes:

```text
/etc/systemd/logind.conf.d/60-hermes-station-lid.conf
/etc/systemd/system/hermes-station-awake.service
```

This ignores lid-close suspend and inhibits sleep/idle while the always-on
station role is active. Desktop, VPS and cloud systems are not assigned a laptop
lid policy.

## Interactive recovery

```bash
sudo ./install.sh
```

The setup prompts for server type, RAM, VRAM, deployment mode, runtime user,
repository choices, OAuth choices and required secrets. Secret prompts do not
echo values.

## Non-interactive recovery

After the kit is installed:

```bash
sudo cp /opt/hermes-recovery-kit/current/templates/config.example.yaml /root/hermes-recovery.yaml
sudo chmod 600 /root/hermes-recovery.yaml
sudoedit /root/hermes-recovery.yaml

sudo --preserve-env=TAILSCALE_AUTH_KEY,TELEGRAM_BOT_TOKEN,TELEGRAM_ALLOWED_USERS \
  /opt/hermes-recovery-kit/current/bootstrap.sh \
  --config /root/hermes-recovery.yaml --non-interactive
```

Configuration files may reference secrets only through `env:NAME`,
`env?:NAME`, `file:/protected/path`, `file?:/protected/path`, `prompt`, or a
blank optional value. Literal secrets in JSON/YAML are rejected.

## OAuth steps

Interactive recovery launches:

```bash
claude
codex --login
```

For unattended recovery, complete them later as the Hermes runtime user:

```bash
sudo -iu bobthabuilda claude
sudo -iu bobthabuilda codex --login
```

## State, health and rollback

```bash
sudo hermes-rebuildctl status
sudo hermes-rebuildctl health
sudo hermes-rebuildctl state
sudo hermes-rebuildctl rollback-list
sudo hermes-rebuildctl rollback <rollback-id>
```

State is stored at `/etc/hermes-rebuild/state.json`. Rollback restores protected
Hermes state and encrypted secrets from a selected pre-run snapshot; it does not
blindly remove shared system packages.

## Test coverage

Run the included suite with:

```bash
./tests/run.sh
```

It covers Python and shell syntax, configuration and secret-reference parsing,
secret redaction, fail-closed host rules, the retired-host invariant, Fedora
host grounding, timezone configuration, OpenSSH, Tailscale auth-key handling,
firewall policy, laptop detection, lid/inhibitor configuration, idempotent
reruns, health-summary behaviour, real local HTTP loader download, outer
checksum validation, internal manifest validation and deliberate corruption
rejection.

The E2E suite operates against an isolated test root and deterministic fake
systemd/Tailscale host. A destructive clean-Fedora installation is not run in
the build container; the first real deployment should still use a fresh Fedora
installation or disposable snapshot.
