---
name: jev-compaction
description: "Use for an already-authorized, non-sensitive handoff to label a bounded message list keep, summarize, or drop. The explicit Jev tool never mutates a transcript or replaces Hermes compression."
version: 0.2.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, compaction, handoff, context]
---

# Compaction and handoffs with Jev

Jev cannot write a summary. What it does is read the transcript turn by turn and mark each one:

- **keep**: carries a decision, a constraint, a preference, unfinished work, or an exact value, path, id, command or error that later work depends on. Survives word for word.
- **summarize**: background whose gist matters.
- **drop**: chatter, superseded attempts, repeated output.

You then summarize a fraction of the transcript, with the lines that must not be paraphrased already flagged. Handoffs get shorter and stop losing the one line that mattered.

## Do this

1. Use a task-local list of `{role, content}` messages from an already-authorized non-sensitive handoff. Preserve the original before selection. Do not export or send private session history merely because this skill is loaded; verify the supported export command if export is needed.
2. Select:

   - hermes-jev 0.4: enable `/jev compaction on`. To load changed plugin code, start a new Hermes CLI process or reload/restart the gateway plugin; a fresh Telegram session alone does not reload cached plugin code. In a session with the exposed tool, call `jev_compact_select`. Its explicit advisory schema accepts 1–120 `{role, content}` messages and optional `keep_last` (0–12, default 6). It labels only; it does not export, mutate, or replace Hermes' ContextCompressor. A disabled tool refuses direct dispatch.
   - Anywhere else:

     ```bash
     jev compact-select --digest < transcript.json      # {"messages":[...]} or a bare list
     ```

3. Use `digest` as a draft aid for the handoff, not the sole evidence source. Check it against the preserved original for decisions, exact identifiers, errors, unresolved work, and verification evidence:
   - Every `[KEEP VERBATIM]` line goes in unchanged, grouped under Decisions, Open work, or Pointers (paths, ids, commands).
   - `[background]` lines become at most one short paragraph of context.
   - Restore any required original evidence omitted by selection; do not invent missing facts or discard failure history.
4. Prefer a concise handoff, but do not impose a 400-word cap when it would lose required evidence. Jev may assist an already-requested handoff without a separate approval; it does not authorize retention, egress, or replacement of Hermes context compression.

## CLI behavior and limits

- The last six messages are always kept; system messages are always kept.
- Nothing is dropped unless Jev was confident (0.7+). An unjudged turn is summarized, never dropped.
- Turns that look like they hold a secret are not sent; they default to summarize.
- Jev down: every turn comes back `summarize`, which is exactly what you would have done without it.

## When to compact at all

Context occupancy is arithmetic, not a model judgment. Hermes owns its compressor trigger and token budget; do not replace its settings with this library's 60%/85% heuristics.
