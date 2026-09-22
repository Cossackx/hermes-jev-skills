---
name: jev-model-routing
description: "Use for a bounded advisory recommendation among active-profile, same-provider models for a prospective fresh Hermes session. Jev never changes a live model or gateway route."
version: 0.2.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, model-routing, cost]
---

# Model routing with Jev

Jev reads a turn and answers three questions in one bounded request (latency varies): how hard is it, what kind of work is it, and would a mistake be costly. Code then walks your pool for that tier and specialty and takes the first model that fits (images, context size). This describes the local CLI recommendation algorithm, not automatic model switching in Hermes.

## On Hermes: no automatic routing in the installed plugin

hermes-jev 0.4 provides `/jev routing on|off` and an explicit `jev_route_model` advisory tool. It has no automatic routing middleware and never switches a live gateway/session model. To load changed plugin code, start a new Hermes CLI process or reload/restart the gateway plugin; a fresh Telegram session alone does not reload cached plugin code. After the plugin is loaded, start a fresh session if tool exposure must refresh.

`jev_route_model` accepts `prompt`, `provider`, and `current`, with optional `context_tokens` and `has_images`; it returns advice only. It reads only the active Hermes home's `jev/routing.json`, uses operator-declared model metadata and pools (never a provider catalog or root-profile inheritance), stays within the supplied provider, and fails open to `current`. A disabled tool refuses direct dispatch. Preserve the owner's explicit model and specialist choices; do not silently change the active Astra model.

## Asking directly (any agent)

For a prospective fresh session, from the source checkout so `jevkit` is on `PYTHONPATH`, use:

```bash
python -m jevkit.launch --prompt "Summarize the supplied public release notes." --dry-run
```

It prints the decision and argv for a fresh `hermes ... chat --query` invocation without bypassing approvals. `--model <exact-id>` bypasses Jev; there is no new shell command and no resume/continue. The legacy `jev route` path has different inherited-config/catalog behavior. Treat `model_id` as a recommendation, not an applied route.

## The pools

For the native launcher/tool, pools and model metadata live only in the active home's `jev/routing.json`. Native pools do not discover models: each pool entry must have matching operator-declared `models` metadata. `context` is a conservative routing-eligibility cap, not a provider context maximum; image routing requires `vision: true`. In `features` mode, no raw prompt text goes to Jev.

```json
{
  "mode": "features",
  "models": [
    {
      "model": "gpt-6-astra",
      "provider": "openai-codex",
      "name": "gpt-6-astra",
      "context": 32000,
      "vision": false,
      "reasoning": true
    }
  ],
  "tiers": {
    "hard": {
      "general": ["openai-codex:gpt-6-astra"],
      "coding": ["openai-codex:gpt-6-astra"]
    }
  }
}
```

- Specialties are `general`, `coding`, `writing`, `research`, `vision`. A missing specialty falls back to `general`. A pool never falls down a tier, only up.
- Legacy standalone catalog path: `jev models suggest --write` uses catalog/pricing data to draft legacy inherited-config pools. It is separate from native active-profile pools and does not discover or populate native `models` metadata.
- A catalog entry or `jev models list` result does not prove provider entitlement. Configure only models the active provider account is entitled to call; do not invent IDs.

## Local CLI policy (not active Hermes middleware)

- Risk words (production, delete, migration, security, payment, legal…) never route to `simple`, however short the prompt.
- Unsure about a risky turn: goes up a tier. Unsure about a harmless one: stays on the current model.
- Large context (over ~32k tokens): never switches to a cheaper model, because rebuilding the prompt cache costs more than it saves.
- Turns that look like they contain secrets, and any profile listed in `private_profiles`, send Jev only coarse features (length, code present, risk words), never text.
- Jev down, slow (2.5 s budget) or malformed: current model, no delay beyond the budget.

## Tuning

The installed plugin decision log concerns skill selection; it is not evidence that model routing ran. During a routing assessment, keep a bounded non-sensitive record of recommendations and compare against known tasks before changing pools or thresholds. A fixed one-day shadow period is not required, but measured evidence is. No routing change is made by this skill alone.
