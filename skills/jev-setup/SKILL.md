---
name: jev-setup
description: Set up or repair Jev credentials privately in the active profile. Use when Jev reports `no_key` or `auth_failed`; never ask for, read, or handle the API key.
version: 0.2.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, setup, credentials]
---

# Connect Jev (the key never passes through you)

Jev needs a TypeSafe API key. Never ask the person to paste it into chat, read a secret store or `.env` file, or put a key in a command line, URL, written config, or log. If a key is pasted into chat, do not repeat/store it; advise replacement and use this flow.

## Active-profile private setup

1. Run `jev doctor` in the active profile. It is the only check for credential presence/reachability; report its non-secret result.
2. Read live command help: `jev setup-key --help`. Do not infer storage behavior or supported flags from this skill.
3. Run the private setup flow only for the active Hermes home. Use the documented `--hermes-home` value for that profile when needed, and do not propagate a credential to other profiles or their environment files.
4. The person enters the key only into the local private browser page, or runs `jev setup-key --tty` themselves in their own terminal when no local browser is available. Do not run a hidden TTY prompt through a captured agent terminal.
5. After setup finishes, rerun `jev doctor` in the same active profile and report only whether the key is present and Jev is reachable.

For a remote private network path, use only flags currently shown by live help, keep binding non-public, and explain any transport limitation shown by that help. Do not claim a particular OS secret store, fallback file, or cross-profile propagation unless current tool output documents it.

Credential isolation is active-profile-only: do not copy, borrow, discover, or default-propagate credentials between Hermes profiles. Do not restart gateways or other services unless separately requested.