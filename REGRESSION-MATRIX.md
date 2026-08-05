# Hermes rebuild v1.2.0 regression matrix

Release status: **candidate only — do not merge or install on Hermes Station until the live gates are completed.**

## Encoded prior failures

| Prior failure | v1.2.0 control | Automated evidence |
|---|---|---|
| Fedora DNF5 rejected DNF4 `config-manager --add-repo` syntax | Repository files are written directly under `/etc/yum.repos.d`; DNF5 only installs packages | Static guard and clean-Fedora command-path simulation |
| `/opt/nodejs` and npm prefix were not traversable by `bobthabuilda` | Parent directories are repaired to executable/traversable mode; npm prefix is runtime-user owned | Permission regression |
| Claude Code install scripts were blocked | Global install uses the explicit npm package allow-list `--allow-scripts=@anthropic-ai/claude-code` | Command-construction regression |
| Git rejected root-owned repositories as unsafe | Repository trees are returned to `bobthabuilda`; `safe.directory` is configured in that user's Git config | Ownership/auth static regression |
| Claude, Codex or GitHub authentication ran as root | User-scoped commands are executed with `runuser`; the runner rejects `user=root` | Authentication regression |
| Fedora lid inhibitor used an unsupported inhibitor category | `systemd-logind` owns lid behaviour; inhibitor uses stable `sleep:idle` categories | Fedora lid static regression |
| UV-managed Python lost SELinux executable labels | Persistent `semanage fcontext` rule plus `restorecon` for `/opt/hermes-tools/uv-python` | SELinux static regression |
| Gateway service existed without a valid Hermes executable | Validate executable bit, `hermes --version`, gateway help, systemd unit and active service | Gateway regression and health checks |
| Bitwarden sourcing was absent or not resumable | Verified native CLI, exact unique folder, approved aliases, duplicate rejection, missing-only full run, focused replacement prompts | Bitwarden parser/merge regressions |
| Interrupted stages could be mistaken for complete | State schema classifies completed, incomplete, failed, stale and running stages; only matching validated stages are reused | Stage-state and every-stage resume regressions |
| Working credentials could be overwritten | Full rebuild preserves differing existing values; focused interactive sync requires confirmation; non-interactive replacement is refused | Credential replacement regression |
| Codex 429 stopped Hermes without a fallback | Detect `HTTP 429 usage_limit_reached`; choose first configured healthy fallback, preferring Bitwarden-sourced OpenRouter | Provider fallback regression |

## Required regression paths

| Path | Automated candidate test | Live release gate |
|---|---|---|
| Clean Fedora installation | DNF5 package/repository command simulation and isolated-root E2E | Run on a disposable clean Fedora host/VM and preserve the complete log |
| Rerun over current working Hermes Station | Completed-stage reuse with validation; credential preservation | Snapshot/backup current station, run plan and controlled rerun, verify no working auth or services regress |
| Resume after interruption at every major stage | Fault injection at all 24 major stages, followed by resume | At minimum, interrupt representative destructive/long-running stages on a disposable Fedora host and verify state/evidence |
| Deliberate stale/invalid credential replacement | Invalid existing value, confirmed focused replacement, declined/non-interactive preservation, duplicate rejection | Use a disposable Bitwarden test item/folder; verify prompt, encrypted state and rollback without exposing values |

## Release decision

The automated suite is necessary but not sufficient. The branch may be opened as a draft pull request after payload and loader validation, but must remain unmerged and must not be installed on the healthy live Hermes Station until the four live release gates above have recorded evidence.
