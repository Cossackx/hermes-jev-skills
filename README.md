# Hermes Jev Skills

Give your agent a fast, cheap second brain for the small decisions.

> **Explicit decision integration (0.4).** Hermes exposes skill advice plus
> profile-gated `jev_memory_filter`, `jev_compact_select`, `jev_choose_action`,
> and `jev_route_model`. These tools return advice and never execute actions or
> rewrite stored history. Automatic model choice is supported before a fresh
> CLI session via `python -m jevkit.launch`; gateway request rewriting is not
> enabled because the host does not expose a complete model-switch/pin contract.

[Jev](https://docs.typesafe.ai) is TypeSafe's decision model. It does not write text. You hand it a state and typed questions (pick one, score this, yes or no) and it answers in about 0.4 seconds for a tiny fraction of a cent, with a calibrated confidence. This repo puts that to work on the decisions an agent makes all day, so your expensive model only does the thinking and writing.

| Skill | What Jev decides | Measured |
|---|---|---|
| **Model routing** | Recommends which eligible model is good enough for this turn | ~0.4 s per recommendation |
| **Memory** | Which retrieved passages are worth reading, and which contain hidden instructions | one request for up to 60 passages |
| **Compaction and handoffs** | Which turns survive word for word, which get summarized, which are dropped | 71 turns in 0.95 s |
| **Skill selection** | Which installed skill this turn needs, or none | 373 skills in ~0.9 s |
| **Computer use** | The next GUI action, from a table of actions you already judged safe | ~0.4 s per step |
| **Browser use** | The next page action, same contract | ~0.4 s per step |

Plus a **model routing dashboard** (`jev dashboard`): every profile's models on one page, an off/on switch for Jev routing advice, and a live view of Jev's recommendations. The plugin is advisory and does not switch the active turn's model. See [router-dashboard](router-dashboard/README.md).

Built for [Hermes](https://github.com/NousResearch/hermes-agent). The skills and the `jev` command also work in Claude Code, Codex and anything else that reads `SKILL.md` files.

## Install

**Point your agent at this branch** and say: *"Install the bounded skill observer
from https://github.com/Cossackx/hermes-jev-skills/tree/hardening/skill-shadow-0.3.3"*.
It will follow [AGENTS.md](AGENTS.md).

**Got it as a zip?** Unzip it anywhere, then run the second and third commands below from that folder.

Or by hand (Python 3.9+, no dependencies):

```bash
git clone --branch hardening/skill-shadow-0.3.3 https://github.com/Cossackx/hermes-jev-skills ~/hermes-jev-skills
```

```bash
python3 ~/hermes-jev-skills/install.py
```

```bash
jev setup-key
```

The installer finds Hermes, Claude Code and Codex on the machine and installs for each one it finds. It writes an installer-owned `jev` launcher that points at the retained checkout, including when Windows cannot create a symlink. `python3 install.py --check` shows what it would do without changing anything; `--uninstall` reverses it.

## Your API key never touches the agent

`jev setup-key` opens a one-time page served only by your own computer. You paste your [TypeSafe key](https://console.typesafe.ai/settings/keys) there. It goes straight into the OS secret store (macOS Keychain, or `secret-tool` on Linux, or a 0600 file as a last resort) and, on a Hermes machine, into each profile's `.env`. The agent that ran the command sees one line: stored, verified, yes or no. Never the key, not even a prefix.

The page lives on an unguessable one-time URL, refuses requests with a foreign `Host` header (DNS rebinding), sends no referrer, logs nothing, and shuts down after one use or ten minutes. On a headless box, run `jev setup-key --tty` yourself for a hidden prompt.

**Do not paste your key into a chat.** If you already did, make a new one.

## On Hermes

The `hermes-jev` plugin uses one public observer hook, explicit advisory tools,
and one status/configuration command. Nothing in Hermes core is patched.

```
/jev                         status
/jev skills shadow           evaluate and log; inject nothing
/jev skills on               attach up to two advisory candidates
/jev skills off              disable skill advice
/jev memory on               enable explicit retrieval-ranking tool
/jev compaction on           enable explicit compaction-selection tool
/jev actions on              enable explicit action-choice tool
/jev routing on              enable routing advice tool
```

New tools become visible in fresh sessions. Their handlers also check the active
profile switch on every call. Settings and credentials are profile-scoped.
The plugin registers no request middleware: it never secretly swaps gateway
models, auto-exports retrieval results, edits stored history, or executes GUI actions.
Keep normal Hermes compression enabled. Jev compaction selection assists explicit
handoffs; it is not a replacement for the host's ContextCompressor.

For automatic selection **before** a fresh CLI session, run from the checkout:
`python -m jevkit.launch --prompt 'Your task'`. Use `--dry-run` to inspect the
selection without launching, or `--model MODEL` to pin a model and skip Jev.
Routing requires a profile-local `jev/routing.json` with explicit pools and model
metadata. Do not interpret a global catalog entry as account entitlement.


### Every model you have

```bash
jev models list              # everything this machine can call: price, context, vision, reasoning
jev models providers         # which providers you hold a key or login for
jev models suggest --write   # first-draft pools from price bands; then edit to taste
```

The catalog is [models.dev](https://models.dev), filtered to providers whose API-key name is set in your environment or Hermes `.env`, or that Hermes holds a login for. Only key *names* are read. Pools are profile-local: the default profile uses `~/.hermes/jev/routing.json`, and a named profile uses `~/.hermes/profiles/<name>/jev/routing.json`. See [skills/jev-model-routing](skills/jev-model-routing/SKILL.md).

## What leaves your machine

Jev is a cloud API, so this is spelled out:

- **Routing**: the user's turn, redacted (emails, phones, tokens, long hex masked), capped at 3,000 characters. Never history, tool results, files or memory. Turns that look like they hold a secret, and any profile you list in `private_profiles`, send only coarse features: length, whether code is present, whether risk words appear.
- **Memory**: the query and up to 900 characters per passage, redacted. Your store's ids, paths and sources are replaced with `P0`, `P1`… and never sent. A passage that looks like a credential is not sent at all.
- **Compaction**: up to 700 characters per turn, redacted. Turns that look sensitive are skipped.
- **Skills**: eligible turns are redacted, then sent with skill names and
  descriptions. Sensitive turns, context-only follow-ups, and Hermes-generated
  control messages are stopped locally before discovery or API use.
- **Computer and browser use**: the goal, short element labels, and your action descriptions. Never screenshots, page text or field values. A goal or label that looks sensitive is refused before sending.

Logs hold decisions only (tier, model, confidence, latency). Never prompt text.

## Everything fails open

No key, timeout, rate limit, malformed reply, low confidence: routing keeps your current model, memory returns the original list, compaction drops nothing, skill selection suggests nothing, and computer use returns `reobserve`. A Jev outage costs you at most the time budget (2.5 s for routing) and never blocks a turn.

Safety rails that do not depend on Jev being right: risk words (production, delete, migration, security, payment, legal…) never route to the cheapest tier; a large context never switches to a cheaper model mid-session; a transcript turn is only dropped on a confident answer; Jev can only ever return an action id you put in the table.

## Layout

```
jevkit/          the library and the `jev` command (stdlib only)
skills/          seven SKILL.md skills, agent-agnostic
hermes/plugin/   the Hermes plugin
router-dashboard/  the model routing page (`jev dashboard`)
install.py       installer / uninstaller
tests/           offline tests, every Jev reply faked
docs/            integration notes
```

```bash
python3 -m unittest discover -s tests
```

## License

MIT. Jev and TypeSafe are products of TypeSafe AI; this project is independent. Optional browser runner wraps [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT), which is not bundled.
