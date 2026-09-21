# Instructions for an agent installing this repo

A person pointed you here to give their agent Jev. Follow these steps in order. They are the whole job.

## The one rule

**You never see the person's TypeSafe API key.** Do not ask for it in chat. Do not read it from any file or secret store. Do not put it in a command, URL or config. If they paste it to you anyway: do not store or repeat it, tell them to replace that key at https://console.typesafe.ai/settings/keys, and continue with step 3, which is the only way a key gets in.

## Steps

1. **Get the code** (skip if it is already on disk):

   ```bash
   git clone --branch hardening/skill-shadow-0.3.3 https://github.com/Cossackx/hermes-jev-skills ~/hermes-jev-skills
   ```

2. **Preview, then install.** Show the person what `--check` reports before running the real thing. It detects Hermes, Claude Code and Codex and installs for each. It edits one list (`plugins.enabled`) in each Hermes `config.yaml`, with a timestamped backup beside it.

   ```bash
   python3 ~/hermes-jev-skills/install.py --check
   ```

   ```bash
   python3 ~/hermes-jev-skills/install.py
   ```

   To enable the Hermes plugin in only some profiles: `--enable name1,name2`, or `--enable none`.

3. **Connect the key, privately.** Run this and tell the person a page has opened on the computer you are running on, where they paste their key:

   ```bash
   jev setup-key
   ```

   It prints a JSON line with a `url` on stderr. That URL holds no secret, so if the browser did not open, or the person is on another device, send it to them and say it only works on the agent's computer. Wait for `{"status": "stored", "verified": true}`. If there is no browser at all, the person runs `jev setup-key --tty` themselves in their own terminal.

4. **Check:**

   ```bash
   jev doctor
   ```

   `key.present` and `jev.reachable` must both be true.

5. **Hermes only.** Plugins load when a session or gateway starts. Do not restart
   a running gateway unless the person asks. Tell them to run `/jev skills shadow`
   in a new session and inspect `logs/jev-decisions.jsonl`. Enable `/jev skills on`
   only after the observations contain no confidently wrong selections.

6. **Report** in three or four sentences: what was installed where, that the key
   is connected, the skill-observer mode, and what needs a fresh session.

## Using it afterwards

The Hermes plugin in this branch exposes only `jev-skill-select`. Other library
modules remain source material and CLI experiments; they are not registered in
Hermes and must not be treated as enabled. Never block on Jev.

## Keeping it current

```bash
git -C ~/hermes-jev-skills pull && python3 ~/hermes-jev-skills/install.py
```

## Contributing back

If you change how Jev is used in a Hermes setup (a new decision Jev takes over, a changed threshold, a fix to the plugin), the same change belongs in this repo, with a test, in the same piece of work. See [CONTRIBUTING.md](CONTRIBUTING.md).
