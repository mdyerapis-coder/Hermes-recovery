# Hermes Secret Provider (HSP)

HSP v1.2 uses the Bitwarden CLI to resolve recovery credentials from the
`AI & LLM` folder (including path-like descendant folders). It is inserted as a
wrapper around the existing recovery controller, so the controller remains the
single owner of rebuild sequencing and safety gates.

## Security properties

- Existing environment variables are preserved and never overwritten.
- Secret values are never printed in status output.
- Vault matches must be unique; ambiguous matches fail closed.
- The Bitwarden session is passed through `BW_SESSION`, not command arguments.
- Resolved values live only in the wrapper and child-process environment.
- The in-memory cache is cleared on exit; a vault unlocked by HSP is relocked.
- Passive operations (`--help`, `--plan`, `--health`, `--summary`, rollback) do
  not unlock the vault unless `HERMES_HSP_FORCE=1` is set.

## Vault item conventions

Place items in `AI & LLM` or a descendant such as `AI & LLM/API Keys/Active`.
HSP recognises ordinary item names such as `OpenAI API Key`, `Anthropic`,
`OpenRouter`, `GitHub Token`, `Telegram Bot`, and `Tailscale Auth Key`.

For deterministic matching, add a custom field named `hermes_secret` with one
of the canonical names from `manifests/secrets.json`, for example `openai` or
`telegram_bot`. The secret itself may be in a named custom field, the login
password, or a secure note. Named custom fields take precedence.

## Interactive use

Run the normal recovery command. HSP checks `bw status`, offers `bw login` and
`bw unlock` on a real terminal, syncs the vault, and injects resolved values at
known no-echo secret prompts. Other recovery questions remain interactive.

## Non-interactive use

Authenticate first and export the returned session only for the recovery run:

```bash
export BW_SESSION="$(bw unlock --raw)"
sudo --preserve-env=BW_SESSION \
  /opt/hermes-recovery-kit/current/bootstrap.sh \
  --config /root/hermes-recovery.yaml --non-interactive
```

The unattended configuration should continue to reference `env:NAME` or
`env?:NAME`; HSP supplies those environment values before the controller runs.

## Controls

- `HERMES_HSP_DISABLE=1` bypasses HSP.
- `HERMES_HSP_FORCE=1` enables HSP for passive modes.
- `HERMES_HSP_MANIFEST=/protected/path/secrets.json` selects another manifest.
