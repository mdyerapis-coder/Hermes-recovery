# Planned Bitwarden integration

The recovery controller will support an optional first-run Bitwarden workflow:

- install the native `bw` CLI without relying on global npm permissions;
- log in and unlock interactively without storing the master password or `BW_SESSION`;
- reuse existing vault items by deterministic names;
- offer explicit per-secret save/update confirmation;
- prompt only for missing secrets on later rebuilds;
- retain the local age-encrypted secret file as the runtime/offline cache;
- keep Claude Code, ChatGPT/Codex and other OAuth credentials in their native stores;
- support scoped `bws` machine-account retrieval for unattended recovery where Bitwarden Secrets Manager is configured.

No vault write, replacement or deletion may occur without explicit operator approval.
