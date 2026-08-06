# Hermes rebuild v1.2.0 live regression runbook

Status: **release-candidate validation only**. Do not merge PR #3 and do not run the installer on the healthy Hermes Station until the disposable-host gate passes and a fresh backup/snapshot has been verified.

## Fixed candidate identifiers

- Branch: `agent/bitwarden-smart-resume-v1.2.0-final`
- Version: `1.2.0`
- Reconstructed archive SHA-256: `73a8ec4fdadee8a40c5c782b83ecf22efb87ff249eb2ec3e174e55b2d323d1a6`
- Loader SHA-256: `f9c863b8f18e73998b80eebccd99fe8837f4a31b9d4641270b2f0bd9ddeee634`

Never paste secret values into test logs, pull-request comments or screenshots. Record key names, source, health result and replacement decision only.

## Evidence directory

Use a new directory for every run:

```bash
export TEST_ID="$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE="$HOME/hermes-v120-evidence/$TEST_ID"
mkdir -p "$EVIDENCE"
exec > >(tee -a "$EVIDENCE/session.log") 2>&1
```

Record host facts before any change:

```bash
date --iso-8601=seconds
hostnamectl
cat /etc/os-release
uname -a
id
free -h
lsblk
getenforce 2>/dev/null || true
dnf --version 2>/dev/null || true
```

## Gate 1 — clean disposable Fedora installation

Use a disposable Fedora VM or host with at least 2 vCPU, 8 GiB RAM and 40 GiB storage. Take a VM snapshot before beginning. This gate must not use the live Hermes Station.

Download and pin the candidate loader:

```bash
curl -fsSLo install.sh \
  https://raw.githubusercontent.com/mdyerapis-coder/Hermes-recovery/agent/bitwarden-smart-resume-v1.2.0-final/install.sh
printf '%s  %s\n' \
  f9c863b8f18e73998b80eebccd99fe8837f4a31b9d4641270b2f0bd9ddeee634 \
  install.sh | sha256sum --check --strict -
chmod 0755 install.sh
```

Run a plan first:

```bash
sudo env \
  HERMES_RECOVERY_REF=agent/bitwarden-smart-resume-v1.2.0-final \
  HERMES_RECOVERY_EXPECTED_SHA256=73a8ec4fdadee8a40c5c782b83ecf22efb87ff249eb2ec3e174e55b2d323d1a6 \
  ./install.sh --plan | tee "$EVIDENCE/plan.log"
```

For the full disposable test, use test-only credentials and a test Bitwarden folder. Do not reuse production Telegram, GitHub, OpenRouter or OAuth credentials. Run the installer, then capture:

```bash
sudo hermes-rebuildctl status | tee "$EVIDENCE/status.txt"
sudo hermes-rebuildctl health | tee "$EVIDENCE/health.txt"
sudo hermes-rebuildctl state > "$EVIDENCE/state.json"
sudo systemctl status hermes-gateway-rebuild.service --no-pager \
  | tee "$EVIDENCE/gateway-status.txt"
sudo journalctl -u hermes-gateway-rebuild.service -n 200 --no-pager \
  | tee "$EVIDENCE/gateway-journal.txt"
```

Acceptance criteria:

- Fedora uses DNF5 without the old DNF4 `--add-repo` form.
- `bobthabuilda` can traverse `/opt`, `/opt/nodejs` and the npm prefix.
- Claude Code and Codex commands execute as `bobthabuilda`.
- Git repositories are owned by `bobthabuilda` and accepted by that user's `safe.directory` configuration.
- SELinux remains enforcing and the UV Python interpreter executes after reboot.
- Lid policy validates on a laptop target or is correctly skipped on a VM.
- The Hermes gateway executable, unit and active service all validate.
- No stage is left `failed`, `incomplete`, `running` or `stale` unless it is an explicitly documented manual OAuth action.

## Gate 2 — rerun over the working Hermes Station

This gate requires a fresh cloud/provider snapshot plus verified file-level backups. Do not proceed from `--plan` to installation without explicit approval.

Before the rerun, capture:

```bash
sudo systemctl status hermes-gateway-personal.service --no-pager || true
sudo -iu bobthabuilda hermes --version
sudo -iu bobthabuilda claude --version || true
sudo -iu bobthabuilda codex --version || true
sudo -iu bobthabuilda gh auth status || true
sudo tar -C / -czf "$EVIDENCE/hermes-protected-state.tar.gz" \
  etc/hermes-rebuild \
  home/bobthabuilda/.hermes \
  var/lib/hermes-stack 2>/dev/null || true
sha256sum "$EVIDENCE/hermes-protected-state.tar.gz" \
  | tee "$EVIDENCE/hermes-protected-state.sha256"
```

Run only the pinned plan first and compare it with the current working state. The rerun passes only when existing healthy credentials remain unchanged, completed stages are reused after validation, the gateway remains active and the post-run health report has no unresolved stages.

## Gate 3 — interruption and resume

Perform this gate only on the disposable Fedora host. Keep the pre-test VM snapshot.

Interrupt representative long-running or state-changing stages with `Ctrl-C`, including at least:

- package installation;
- Node/npm installation;
- repository checkout;
- Bitwarden unlock/import;
- Hermes installation;
- gateway service activation.

After each interruption:

```bash
sudo hermes-rebuildctl status
sudo hermes-rebuildctl state > "$EVIDENCE/state-after-interrupt.json"
```

The interrupted stage must be `incomplete` or become `stale` after a simulated abandoned run. A rerun must resume at that stage, preserve earlier validated stages and finish without silently marking unresolved work successful.

## Gate 4 — stale or invalid credential replacement

Use a disposable Bitwarden account or test collection and a test-only exact folder configured for the run.

Exercise all four decisions:

1. Missing local value is imported.
2. Identical value is retained as unchanged.
3. Different healthy value is preserved during a normal rebuild.
4. Stale/invalid value is replaced only after explicit confirmation during focused `--bitwarden-sync`.

Also verify that a declined replacement remains unchanged, a non-interactive focused sync cannot replace a differing value, duplicate candidate values fail closed and the vault is locked after the operation.

Evidence must show only key names and decision labels such as `imported`, `unchanged`, `preserved-existing`, `preserved-declined` or `replaced-stale`. Never record raw values.

## Release decision

PR #3 may leave draft status only after all four gates have attached evidence and an independent review confirms:

- archive and loader hashes match this runbook;
- no unresolved stage remains;
- no working credential was lost;
- Codex `usage_limit_reached` selects a verified configured fallback rather than being reported as repaired;
- the live Hermes gateway and existing user-scoped authentication remain healthy.
