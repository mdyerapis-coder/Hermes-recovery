# Hermes Station disaster-recovery bootstrap v1.2.0

Mobile-first, production-oriented recovery tooling for rebuilding a clean
`hermes-station` Fedora/Ubuntu host or safely resuming/rerunning the existing
station. It installs Hermes, restores HADA and Agent Forge source, configures
Tailscale/OpenSSH/Docker, installs Claude Code and Codex, and validates the
Hermes gateway without granting autonomous merge, deployment, repair, secret or
infrastructure authority.

> **Release candidate:** do not install this branch on the healthy live Hermes
> Station and do not merge it until the live regression gates in
> `REGRESSION-MATRIX.md` are complete.

## Loader

```bash
curl -fsSLO https://raw.githubusercontent.com/mdyerapis-coder/Hermes-recovery/main/install.sh
chmod +x install.sh
sudo ./install.sh
```

The loader reconstructs the versioned archive from its ordered payload parts
(or uses an explicitly supplied archive URL), verifies the published archive
SHA-256, verifies the internal per-file manifest, installs under
`/opt/hermes-recovery-kit/releases/1.2.0`, switches the `current` symlink, and
then invokes the verified bootstrap. Do not use `curl | sudo bash`.

## v1.2.0 remediation scope

- Uses Fedora DNF5-compatible repository handling; it does not invoke the old
  DNF4 `config-manager --add-repo` form.
- Repairs execute/traversal permissions for `/opt/nodejs`, its parents and the
  npm prefix used by `bobthabuilda`.
- Installs Claude Code globally with the explicit npm package allow-list
  `--allow-scripts=@anthropic-ai/claude-code`.
- Returns Git repository ownership to `bobthabuilda` and configures
  `safe.directory` in that user's Git configuration.
- Runs Claude, Codex, GitHub CLI and Hermes authentication as
  `bobthabuilda`; the command runner refuses user-scoped operations as root.
- Uses `systemd-logind` for lid-close behaviour and a Fedora-compatible
  `systemd-inhibit --what=sleep:idle` service.
- Persists SELinux executable labels for the UV Python tree with
  `semanage fcontext`, followed by `restorecon`.
- Validates the Hermes executable, version command, gateway command, systemd
  unit and active service before recording the gateway stage complete.
- Installs and verifies the native Bitwarden CLI, requires the exact unique
  folder `AI & LLM/API Keys/Active`, accepts only approved credential names and
  aliases, rejects duplicate values, and supports resumable focused sync.
- Classifies stages as completed, incomplete, failed, stale, running or skipped.
  A stage is reused only when controller version, configuration hash and its
  optional live validator still match.
- Preserves different existing credentials on a full rebuild. Focused
  interactive Bitwarden sync asks before replacement; non-interactive runs do
  not replace a different value.
- Detects Codex `HTTP 429 usage_limit_reached`. An installer cannot repair that
  account limit, so it selects the first configured fallback with a healthy
  credential, preferring a Bitwarden-sourced OpenRouter key when available.

## Bitwarden smart credential sourcing

Interactive recovery offers Bitwarden by default. An existing healthy `bw`
executable is reused. Otherwise, the controller selects the latest stable CLI
release from Bitwarden's official GitHub repository, requires a GitHub-published
SHA-256 asset digest, verifies the archive, validates the extracted executable,
and installs it at `/opt/hermes-tools/bitwarden/bw`.

Focused credential sync:

```bash
sudo /opt/hermes-recovery-kit/current/bootstrap.sh --bitwarden-sync
```

The operation runs `bw` as `bobthabuilda`, reuses an existing login, requests an
unlock when needed, searches the exact configured folder, merges only approved
keys into the encrypted canonical secret store, materialises the runtime
`.hermes/.env`, locks the vault, and discards the session value. Bitwarden master
passwords and session keys are never written to installer state or normal logs.

## Interactive recovery

```bash
sudo ./bootstrap.sh
```

Secret prompts use no-echo input. OAuth/browser authorisation remains a human
step. Claude, Codex and GitHub CLI authentication are never executed as root.

## Non-interactive recovery

```bash
sudo cp templates/config.example.yaml /root/hermes-recovery.yaml
sudo chmod 600 /root/hermes-recovery.yaml
sudoedit /root/hermes-recovery.yaml

sudo --preserve-env=OPENROUTER_API_KEY,GITHUB_TOKEN,TELEGRAM_BOT_TOKEN,TELEGRAM_ALLOWED_USERS \
  ./bootstrap.sh --config /root/hermes-recovery.yaml --non-interactive
```

Configuration files may reference secrets only through `env:NAME`,
`env?:NAME`, `file:/protected/path`, `file?:/protected/path`, `prompt`, or a
blank optional value. Literal secrets in JSON/YAML are rejected.

For unattended OAuth completion, run later as the runtime user:

```bash
sudo -iu bobthabuilda claude
sudo -iu bobthabuilda codex login
sudo -iu bobthabuilda gh auth login
```

## State, resume and rollback

```bash
sudo hermes-rebuildctl status
sudo hermes-rebuildctl health
sudo hermes-rebuildctl state
sudo hermes-rebuildctl rollback-list
sudo hermes-rebuildctl rollback <rollback-id>
```

State is stored atomically at `/etc/hermes-rebuild/state.json`. A prior stage
left `running` after process death becomes `stale`; an interrupted recoverable
stage becomes `incomplete`; an exception becomes `failed`. Successful reruns
skip only matching completed stages that still pass their validator. A run with
unresolved stages exits with status 2 rather than being reported as healthy.

Rollback restores protected Hermes state and encrypted secrets from the selected
pre-run snapshot. It deliberately does not remove shared host packages such as
Docker, Node.js or GPU tooling.

## Host grounding and safety boundaries

The controller enforces the target `hermes-station`, timezone
`Australia/Melbourne`, runtime user `bobthabuilda`, and the retirement of
`hada-control`. HADA source restoration is not reported as a deployed HADA
service. HADA deployment remains fail-closed and subject to its repository-native
approval/evidence gates. Agent Forge remains subordinate to Hermes and receives
no merge or deployment authority.

## Validation

```bash
./tests/run.sh
```

The candidate suite covers shell/Python syntax, isolated-root E2E operation,
DNF5 command construction, traversal permissions, npm script approval, Git
ownership/authentication boundaries, Fedora lid handling, persistent SELinux
rules, gateway validation, explicit stage-state classification, reuse over a
working installation, fault injection and resume at all 24 major stages,
Bitwarden exact-folder/duplicate/preservation/replacement logic, and Codex 429
fallback selection.

These automated tests simulate the four required regression paths. They do not
replace the live disposable-Fedora and controlled-current-station gates described
in `REGRESSION-MATRIX.md`.
