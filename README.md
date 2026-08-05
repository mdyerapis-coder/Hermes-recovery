# All-in-One Hermes Rebuild Bootstrap

A disaster-recovery kit for rebuilding Hermes, HADA, Agent Forge integration,
Claude Code, OpenAI Codex CLI, Bitwarden credential sourcing, Docker and optional NVIDIA container support on
a systemd-based Linux host.

## Supported recovery target

- Ubuntu/Debian or Fedora/RHEL-family Linux
- systemd
- x86_64 or ARM64 for Node.js
- root access
- internet access to the configured Git repositories and package registries

The controller installs a tested Python runtime with `uv`, verifies Node.js
archives against the official SHA-256 manifest, installs Docker from Docker's
signed repository, and installs NVIDIA Container Toolkit only when a working
NVIDIA driver already exists. It deliberately does not choose or install a
kernel GPU driver automatically because driver selection and reboots are
hardware/provider specific.

## File layout after installation

```text
/etc/hermes-rebuild/
├── state.json                 # root-only persistent component/run state
├── health-last.json           # latest machine-readable health report
├── secrets.env.age            # encrypted master environment
├── age/identity.txt           # root-only age identity
└── agent-forge.json           # root-only adapter configuration

/opt/hermes-stack/
├── hermes-agent/              # pinned Hermes checkout and uv virtualenv
├── hada/                      # pinned HADA source/evidence tree
├── agent-forge/               # restored Agent Forge source, if available
└── projects/                  # additional manifest-managed projects

/opt/hermes-tools/npm/         # Claude Code and Codex CLI npm prefix
/opt/hermes-tools/bitwarden/   # verified native Bitwarden CLI
/var/backups/hermes-rebuild/   # pre-run rollback points
/usr/local/bin/hermes
/usr/local/bin/hermes-agent-forge
/etc/systemd/system/hermes-gateway-rebuild.service

~RUNTIME_USER/.hermes/
├── config.yaml
├── .env                       # 0600 materialisation of encrypted master
└── skills/local/
    ├── agent-forge/SKILL.md
    ├── hada/SKILL.md
    ├── claude-code/SKILL.md
    └── codex/SKILL.md
```

## Interactive recovery

```bash
chmod +x bootstrap.sh
sudo ./bootstrap.sh
```

The setup prompts for server type, RAM, VRAM, deployment mode, runtime user,
HADA deployment authorisation, OAuth choices, Bitwarden sourcing and secrets. Secret prompts use
`getpass`, so values are not echoed. When Bitwarden is enabled, the exact folder
`AI & LLM/API Keys/Active` is searched before the encrypted master is written.

Claude Code OAuth launches `claude`; complete the account/browser flow and exit
with Ctrl-D. ChatGPT OAuth launches `codex --login`. These logins require human
interaction and are never embedded in the recovery config or state file.

## Bitwarden smart credential sourcing

Interactive recovery offers Bitwarden by default. The controller installs the
official native Linux x64 CLI from the latest non-prerelease Bitwarden CLI
release, verifies the published SHA-256 checksum, and runs it as the configured
runtime user.

```bash
sudo ./bootstrap.sh --bitwarden-sync
# or after installation:
sudo /opt/hermes-recovery-kit/current/bootstrap.sh --bitwarden-sync
```

The sync operation is resumable. Full rebuilds are missing-only by default; a
focused interactive `--bitwarden-sync` asks before replacing each differing
local credential:

- an existing healthy `bw` executable is reused;
- an existing login is reused and only an unlock is requested;
- the folder name must match exactly and uniquely;
- only approved environment-variable names and conservative aliases are read;
- duplicates stop the import instead of choosing silently;
- existing different values are retained during a full rebuild; focused syncs
  ask for explicit confirmation before replacing each one;
- imported values are merged into `/etc/hermes-rebuild/secrets.env.age` and the
  runtime `.hermes/.env`;
- the vault is locked and the in-memory session is discarded after import.

Bitwarden master passwords and session keys are never stored in rebuild state,
logs, command arguments, or Hermes memory. OAuth credentials remain managed by
their native Claude Code and Codex stores.

## Non-interactive recovery

```bash
cp templates/config.example.yaml /root/hermes-recovery.yaml
chmod 600 /root/hermes-recovery.yaml

export OPENROUTER_API_KEY='...'
export GITHUB_TOKEN='...'
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_ALLOWED_USERS='123456789'

sudo --preserve-env=OPENROUTER_API_KEY,GITHUB_TOKEN,TELEGRAM_BOT_TOKEN,TELEGRAM_ALLOWED_USERS \
  ./bootstrap.sh --config /root/hermes-recovery.yaml
```

OAuth is recorded as a manual action in non-interactive mode:

```bash
sudo -iu bobthabuilda claude
# Use /login if prompted.

sudo -iu bobthabuilda codex --login
```

## Plan, health, state and rollback

```bash
sudo ./bootstrap.sh --config /root/hermes-recovery.yaml --plan
sudo ./bootstrap.sh --config /root/hermes-recovery.yaml --health
sudo ./bootstrap.sh --config /root/hermes-recovery.yaml --summary
sudo ./bootstrap.sh --config /root/hermes-recovery.yaml --rollback latest
```

Rollback restores protected Hermes state and encrypted secrets from the selected
pre-run snapshot. It does not remove shared host packages such as Docker,
Node.js or GPU tooling; removing those automatically could damage unrelated
workloads.

## Idempotency model

- Git repositories are reused, fetched, and detached at the configured ref.
- Existing generated passwords/tokens are decrypted and reused on later runs.
- Node, Docker, uv and CLI installs are version/health checked before changes.
- Bitwarden import records folder/import state and resumes by importing only missing approved keys.
- Systemd units and configuration are atomically replaced.
- State writes are atomic and root-only.
- Existing Hermes configuration is merged, not blindly discarded.
- A rollback point is created before protected state is changed.

## Secret handling

1. Interactive values are read without terminal echo.
2. Config files may only use `env:NAME`, `file:/path`, `prompt`, or blank values.
   Literal secret strings are rejected.
3. The canonical master is encrypted with an `age` identity stored root-only.
4. Hermes receives a 0600 `.env` materialisation because Hermes reads its own
   environment file directly.
5. OAuth credentials stay in Claude Code/Codex-owned credential storage. The
   bootstrap records only whether login was attempted or remains outstanding.
6. Bitwarden sessions exist only in process memory and are invalidated with `bw lock`.
7. Commands and state metadata are redacted by key name and never contain raw
   secret values.

## HADA safety boundary

The rebuild restores and verifies HADA source but does not claim that HADA is
running or deployed. Deployment stays fail-closed by default. Even when
`hada.allow_deployment: true` is selected, each repository-native phase approval
and evidence gate remains required.

## Agent Forge limitation and integration

The configured `mdyerapis-coder/agent-forge-e2e` repository is currently allowed
to be empty. The rebuild still installs the Hermes-controlled adapter and skill,
but records a manual action until the actual Agent Forge source and its tested
CLI argv are supplied. The adapter:

- accepts a bounded task from Hermes;
- requires a Git worktree by default;
- serialises execution with a host lock;
- applies a timeout;
- preserves `HERMES_SESSION_ID`;
- never receives merge/deploy authority.
