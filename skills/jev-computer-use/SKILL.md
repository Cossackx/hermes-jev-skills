---
name: jev-computer-use
description: "Use when operating a desktop GUI through a computer-use driver. You observe and build prevalidated actions; the explicit advisory Jev tool selects one opaque candidate ID. The main model remains responsible for planning and visual interpretation."
version: 0.2.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, computer-use, gui, cua]
    related_skills: [jev-browser-use]
---

# Computer use with Jev

You stay the planner and the hands. Jev is only the fast "which one next?" in the middle. It returns an id from a table **you** built, so it cannot invent coordinates, text, selectors or tool calls. A wrong or stale selection can still cause an incorrect action, so revalidate the target and authorization before execution.

Web pages belong to `jev-browser-use`. This skill is for desktop apps and OS surfaces, driven through whatever computer-use driver you have (CUA Driver over MCP, the platform's native computer-use tool, an accessibility bridge).

## The loop

1. **Observe** with your driver. Prefer accessibility/semantic state over pixels. Every ref, capture id and coordinate is good for this observation only.
2. **Build the candidate table locally.** Each row is an opaque id plus one complete, prevalidated action. Always include:
   - `reobserve`: look again, change nothing
   - `abstain`: stop and ask for help
3. **Privacy gate.** Nothing sensitive goes to Jev: no credentials, tokens, cookies, password-field contents, payment data, customer data, screenshots, files or unbounded page text. If the screen holds such content, abstain or handle it without Jev.
4. **Ask once:**

   ```bash
   # hermes-jev 0.4: enable /jev actions on. To load changed plugin code,
   # start a new Hermes CLI process or reload/restart the gateway plugin; a fresh
   # Telegram session alone does not reload cached plugin code. In a session with
   # the exposed tool, call jev_choose_action with {"request": <this object>}.
   ```

   ```json
   {"schema": "jev.action_choice_request_v1",
    "goal": "Open Settings and select Appearance.",
    "observation_id": "capture-0042",
    "regions": [{"id": "r1", "role": "button", "label": "Appearance", "interactive": true}],
    "history": [{"selected_id": "open-settings", "outcome": "settings window opened"}],
    "candidates": [
      {"id": "select-appearance", "description": "Click the Appearance row in the Settings sidebar."},
      {"id": "reobserve", "description": "Take a fresh observation without changing anything."},
      {"id": "abstain", "description": "Do not act; ask the person for help."}]}
   ```

   For the native Hermes tool, pass this object as the structured `request` argument. JSON on stdin or in a temp file is only for the legacy `jev choose` CLI; never interpolate that JSON into a shell string.
5. **Run exactly the one action** behind the validated `selected_id` when the observation and target are still current and the action is already authorized. The request must use `jev.action_choice_request_v1`, contain 2–32 candidates including `reobserve` and `abstain`, and preserve its observation ID. The native tool is advice only; a disabled tool refuses direct dispatch. Confidence under 0.80, or any Jev failure, comes back as `reobserve`. Never derive an action from anything but the id.
6. **Observe again and verify the postcondition yourself.** A chosen id, a delivered click or a screenshot is not proof. Check application state before the next step. Stop after a bounded number of steps.

## Authority

Driving a GUI gives you no new permissions. Sending, publishing, paying, purchasing, deleting, changing credentials or security settings, and anything touching customer data still need the person's explicit yes, exactly as they would without a GUI. Use your driver's standard permission mode; never an approval-bypass flag. The person does all sign-ins, 2FA and payment prompts themselves.

If the driver, the key or the target is unavailable: stop and say what is missing. Do not improvise another way to control the screen.

`jev choose --mock` answers `reobserve` with no network call, for testing your loop.
