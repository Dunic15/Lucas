#!/usr/bin/env python3
"""Browser B1 Visual Eyes: REAL-provider smoke (credential-gated).

Runs the full visual loop against a REAL remote browser + a REAL multimodal
planner on a PUBLIC demo page. It is the ONLY acceptance gate for real pixels;
fake-provider results NEVER substitute for it.

Refuses to run (exit 2, prints ``UNPROVEN``) unless ALL of these are set:
  BROWSER_OPERATOR_ENABLED=true
  BROWSER_REAL_PROVIDER_ENABLED=true
  BROWSER_VISUAL_PLANNER_ENABLED=true
  BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID
  OPENAI_API_KEY  (or BROWSER_PLANNER_API_KEY)
  BROWSER_B1_SMOKE_URL   (a public page you control/trust)

It never fabricates results. With credentials absent it exits marking real
visual perception UNPROVEN. Screenshots are handled in memory only and are
never written to disk or logs.

Usage:
  BROWSER_B1_SMOKE_URL=https://example.org \\
  BROWSER_OPERATOR_ENABLED=true BROWSER_REAL_PROVIDER_ENABLED=true \\
  BROWSER_VISUAL_PLANNER_ENABLED=true \\
  python3 backend/scripts/browser_b1_smoke.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _unproven(reason: str) -> None:
    print(f"UNPROVEN: real visual perception not exercised — {reason}")
    print("Do NOT record any latency/cost/reliability numbers from this run.")
    sys.exit(2)


def main() -> None:
    from app.config import settings

    if not (settings.browser_operator_enabled
            and settings.browser_real_provider_enabled
            and settings.browser_visual_planner_enabled):
        _unproven("one of the B1 flags is off")
    if not (settings.browserbase_api_key and settings.browserbase_project_id):
        _unproven("BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID absent")
    if not (os.environ.get("OPENAI_API_KEY")
            or os.environ.get("BROWSER_PLANNER_API_KEY")):
        _unproven("no multimodal planner key")
    url = os.environ.get("BROWSER_B1_SMOKE_URL", "")
    if not url:
        _unproven("BROWSER_B1_SMOKE_URL not set")

    from app.browser import coordinator, operator
    from app.browser.multimodal import get_visual_planner

    org = os.environ.get("BROWSER_B1_SMOKE_ORG", settings.demo_org_id)
    planner = get_visual_planner()
    if planner is None:
        _unproven("multimodal planner did not initialize")

    print(f"[b1-smoke] creating REAL browser session (org={org}) ...")
    session = operator.create_session(org, principal="smoke",
                                      avatar_key="laura")
    sid = session["id"]
    started = time.monotonic()
    try:
        # 1. Prove a real screenshot observation.
        operator.issue_command(org, sid, verb="navigate", url=url,
                               principal="smoke")
        obs, shot = operator.perceive(org, sid, principal="smoke")
        assert shot, "no screenshot bytes from the real provider"
        print(f"[b1-smoke] screenshot bytes={len(shot)} "
              f"digest={obs['screenshot_digest'][:12]} "
              f"elements={len(obs['elements'])}")
        # 2-4. Multimodal-planned navigation + guarded op + verification, all
        # inside the bounded coordinator (domain allowlist enforced).
        outcome = coordinator.run(org, sid, os.environ.get(
            "BROWSER_B1_SMOKE_GOAL", "explore the page and read the heading"),
            principal="smoke", planner=planner)
        elapsed = time.monotonic() - started
        print(f"[b1-smoke] coordinator outcome={outcome['outcome']} "
              f"steps={outcome.get('step_count')} "
              f"replans={outcome.get('replans')} "
              f"model_calls={outcome.get('model_calls')} "
              f"elapsed={elapsed:.1f}s")
        usage = getattr(planner, "last_usage", {})
        if usage:
            print(f"[b1-smoke] model={usage.get('model')} "
                  f"in_tok={usage.get('input_tokens')} "
                  f"out_tok={usage.get('output_tokens')}")
        print("PROVEN: real screenshot + real multimodal plan executed. "
              "Record the numbers above in BROWSER-B1-VISUAL-EYES.md.")
    finally:
        operator.close_session(org, sid, principal="smoke")


if __name__ == "__main__":
    main()
