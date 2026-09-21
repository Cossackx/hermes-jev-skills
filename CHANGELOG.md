# Changelog

## 0.3.5 (2026-09-21)

- Fixed installer edits of `plugins.enabled`: block-list members now inherit the
  configured child indentation, non-empty inline lists expand under their
  existing key without losing its comment, and CRLF files retain CRLF endings.
- Replaced the installed `jev` file link with an installer-owned launcher that
  records the retained checkout path, so the command continues to find
  `jevkit` when Windows cannot create a symlink and would otherwise hardlink it.

## 0.3.4 (2026-09-21)

- Hardened `jev-browser-use` for Jev Ultrafast on dedicated Browser Harness/CDP
  automation browsers: Windows virtualenv discovery, explicit HTTP or WebSocket
  endpoint mapping and refusal without one, and activation of only the agent's
  owned target before the bounded decision loop.
- Documented the Chrome 153+ background-target listbox limitation and the
  dedicated-browser launcher/fallback workflow; added offline runner coverage.

## 0.3.3 (2026-09-21) — bounded skill-observer fork

- Narrowed the Hermes plugin to skill observation only: one `pre_llm_call` hook,
  `/jev skills shadow|on|off`, no tools, middleware, model routing, compaction,
  memory filtering, action selection, or prompt section.
- Added a genuine shadow mode that logs only safe decision metadata, up to two
  candidates, and latency while returning no injected context.
- Uses Hermes-native, profile-scoped skill roots and state; sensitive turns are
  rejected before discovery or Jev use.
- Context-only follow-ups and Hermes-generated control messages now defer to
  Hermes before discovery or API use.
- Added a deterministic lexical shortlist and exact-name reserve so high-signal
  requests such as `fix cmc agent codex login` keep `codex` in the final advice.
- The installed plugin bundles only `client`, `keystore`, `privacy`, and
  `skillpick`. The Windows installer falls back to directory junctions when
  symlink privilege is unavailable and removes those junctions safely.
- The installer converts a non-empty inline `plugins.enabled` list to a block
  list without duplicating the YAML key or losing existing entries/comments.
- Added focused offline coverage for privacy, profile isolation, top-two shadow
  decisions, follow-up/control bypasses, lexical recovery, and Windows install;
  the suite runs both with standalone Python and Hermes' runtime Python.

## 0.2.0 (2026-09-18)

- The model routing dashboard ships in the repo (`router-dashboard/`, `jev dashboard`): per-profile models, an All-profiles target with confirmation, an Off / Shadow / On switch for Jev routing, and a live view of decisions.
- `scripts/build_release.sh` builds the shareable zip from the committed tree.

## 0.1.1 (2026-09-18)

- Plugin manifest: `config_schema` in the flat shape Hermes expects (it logged a warning and skipped the old one).
- Key page: no reverse-DNS lookup on bind (stalled for seconds on some Macs).
- Shared `routing.json` / `state.json` in the Hermes root are the default for every profile; `/jev <switch> <value> all`.

## 0.1.0 (2026-09-18)

First release.

- `jevkit`: strict Jev client, key store, private key-entry page, privacy gate, model catalog, router, memory filter, compaction selector, two-stage skill picker, bounded action chooser, `jev` command.
- Hermes plugin `hermes-jev`: per-turn model routing through `llm_request` middleware, per-turn skill suggestion through `pre_llm_call`, three tools, `/jev` with per-profile and all-profile switches, decision log.
- Seven agent-agnostic skills.
- Installer for Hermes, Claude Code and Codex, with `--check` and `--uninstall`.
