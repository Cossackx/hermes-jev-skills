# Model routing dashboard

One page for every Hermes profile's models, with Jev on top.

```bash
jev dashboard
```

Then open http://127.0.0.1:8791/.

- **Jev routing-advice switch**: Off or On. It follows the profile picker: one profile, or **All profiles** with a confirmation. Each selected profile receives its own setting. The tool remains advisory: it recommends a model but does not switch the active turn.
- **Live**: every recorded Jev recommendation across all profiles: tier, kind of work, recommended model, confidence, and Jev latency. Metadata only; the text of a turn is never logged or shown.
- **Models**: the main model and each auxiliary slot (compression, vision, title generation and the rest) per profile, with a searchable list of every model you hold a key or login for. **All profiles** sets a slot for everyone at once, after a confirmation that names how many agents it touches.
- Every write is previewed, backed up beside the config, and read back to verify. It never restarts a gateway.

Needs PyYAML, which Hermes' own Python already has; `jev dashboard` uses that interpreter when it finds it.

**Off your own machine** (Tailscale, VPN): `jev dashboard --host <private-ip>`. It refuses to start without a token off loopback, prints a one-time link carrying it, and swaps it for an HttpOnly cookie. The traffic is plain HTTP, so only do this on a network you trust end to end, and never on a public address.

Tests: `python -m unittest discover -s router-dashboard/tests` (with PyYAML installed).
