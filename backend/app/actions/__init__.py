"""Action-lifecycle domain: proposal → approval → execution → provenance.

Modules: workflow (post-meeting email/Slack sends, formerly app/actions.py),
ledger, outbox(+_pg), executor, action_plane, action_deps, action_reconcile,
autopilot, scheduler. The workflow send helpers are re-exported here so
``app.actions.send_email`` / ``post_to_slack`` / ``artifact_to_slack_text``
keep resolving (and stay monkeypatchable) after the flat-module move.
"""
from app.actions.workflow import *  # noqa: F401,F403
