# Archived realizer prompts

These four files were the original *intent-specific* prompts the realizer used before the goal-driven refactor:

- `focus_nudge_v1.md`
- `offer_research_v1.md`
- `surface_open_thread_v1.md`
- `summarize_session_v1.md`

The active path now loads **one** prompt — `prompts/realizer/goal_aware_v1.md` — regardless of any "intent" tag on the decision. The realizer reads the `reason_codes`, the user's `daily_goal`, and the screenshot, and decides the message shape itself.

Keeping these here as references in case the goal-aware prompt drifts and someone wants to compare against the old intent-specific tone. `realizer._load_prompt()` will fall back to the intent-named file if `goal_aware_v1.md` is ever missing, so moving these out of the parent directory was deliberate — we don't want them shadowing the goal-aware prompt by accident.

If you genuinely want to revert to intent-specific prompts, move one back to `prompts/realizer/` AND delete `goal_aware_v1.md`.
