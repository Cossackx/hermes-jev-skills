# Changelog

## 0.5.0 — measured selection and privacy-safe effectiveness

- Add an experimental one-request selector and a conservative `auto` mode that
  uses it only for a single explicitly named, non-compound skill request;
  established two-stage selection remains the control and fallback.
- Cache the native eligible-skill catalog for five seconds, invalidating on
  profile, roots, root metadata, disabled skills, HMAC-bound environment values,
  platform, or bounded expiry without retaining environment secrets.
- Record request bytes, attempts, provider usage, selector strategy, and stage
  latency without changing match or privacy checks.
- Add profile-local, HMAC-correlated effectiveness telemetry for decisions,
  advice, actual skill loads, tool/API outcomes, turn outcomes, usage buckets,
  and loaded-context buckets. Prompt, response, tool payload, error text, paths,
  URLs, and raw correlation IDs are never retained.
- Add a read-only dashboard effectiveness rollup that exposes aggregates only,
  reads complete bounded regular-file segments, and rejects linked storage.
- Add a balanced 120-case held-out dataset and source-bound resumable live
  benchmark whose failed trials remain auditable and retryable.

## 0.4.2 — native skill eligibility and threshold integrity

- Reuse Hermes' native frontmatter parser, scan order, disabled state, and
  platform/environment eligibility for automatic skill advice.
- Parse folded and literal YAML descriptions correctly in the standalone
  fallback instead of exposing block markers such as `>-` as descriptions.
- Require exact-name reserve candidates to meet the configured match threshold;
  lexical recovery can no longer inject a below-threshold suggestion.
- Add offline regressions for YAML block scalars, platform filtering, root
  precedence, disabled skills, threshold enforcement, and native discovery.

## 0.4.1 — credential isolation and pinned decisions

- Propagate a separate copy of caller context into each skill shortlist worker,
  preserving both the host profile and credential resolver. Missing or failing
  profile credentials cannot fall back to an ambient key in worker threads.
- Pin the shared client default to `jev-1.13.0`; explicit model overrides remain
  supported. Jev is the decision model, not the Hermes conversation model.
- Add offline regressions for concurrent profile separation, missing and broken
  credential resolvers, and the model sent on the wire.
- Automatic skill advice remains independently switchable with `/jev skills off`;
  this does not disable the explicit advisory tools or change the Hermes model.

## 0.4.0 — explicit decision integration

- Added profile-gated native routing advice, retrieval ranking, compaction
  selection and browser/desktop action-choice tools, with strict bounded inputs.
- Added automatic same-provider selection before fresh Hermes CLI sessions;
  explicit model arguments bypass routing. No live gateway wire-model rewrite.
- Bound native Jev calls to Hermes' active secret scope; missing profile keys
  cannot fall through to another profile's environment or a shared key store.
- Preserved fail-open selection, privacy checks, normal approvals and the native
  compressor. Added tests for isolation, mode changes and malformed inputs.
- Existing installer fixes and unrelated local changes remain preserved.

## 0.3.6 (2026-09-22)

- Prevented ambiguous Hermes skill names when a configured `skills.external_dirs`
  root already exposes all Jev skills, including categorized recursive roots and
  scalar, flow-list, or block-list configuration forms. Check mode now predicts
  the post-install shared roots instead of reporting the opposite projection.
- Kept a profile-local bundle when an external path resolves through that same
  bundle, and only replaces or uninstalls links, junctions, or byte-identical
  installer bundles. Modified or unrelated local content is preserved and
  reported as a conflict.

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
