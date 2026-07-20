"""Scripted, deterministic how-to walkthroughs for known sites (Sable B3+).

The visual planner is great at open-ended browsing but unreliable at driving a
specific multi-step flow through a complex SaaS app. For the how-to
walkthroughs a meeting asks for ("show me how to create a task"), a scripted
RECIPE against confirmed, stable selectors is reliable where the planner
stalls. Each recipe is a short list of read-only steps:

  navigate  — go to a page (domain-allowlist enforced by the caller)
  point     — glide the visible cursor to a control and pulse (no click)
  reveal    — click a control that only OPENS a form/menu (never submits)
  say       — narration only (the avatar speaks it)

Recipes are STRICTLY read-only: they point and open, they never type or submit,
so nothing in the user's workspace changes. Selectors were captured from the
live app (Italian UI here — aria-labels/text are language-specific, so keep a
few fallbacks per control). A step whose control isn't found is skipped
gracefully — the narration still lands, the walkthrough never hard-fails.
"""
from __future__ import annotations

# {site_label: {task_key: [steps]}}. Each step: op + (url|sel) + say.
# `sel` may carry alternates separated by " || " — the runner tries each.
RECIPES: dict[str, dict[str, list[dict]]] = {
    "asana": {
        "create_task": [
            {"op": "navigate", "url": "https://app.asana.com/",
             "say": "Let me open your Asana."},
            {"op": "point", "sel": "text=Crea attività || [aria-label='Crea']",
             "say": "To create a task, you start with Create, up here."},
            {"op": "reveal", "sel": "text=Crea attività || [aria-label='Crea']",
             "say": "That opens a new task."},
            {"op": "point",
             "sel": "[aria-label='Nome attività'] || [placeholder*='Nome attività']",
             "say": "Here's where you type the task's name."},
            {"op": "point", "sel": "[aria-label='Data di scadenza']",
             "say": "This is where you set the due date."},
            {"op": "point", "sel": "[aria-label='Descrizione']",
             "say": "And any details go here."},
            {"op": "say",
             "say": "Then you assign it and save — and it's on the board. "
                    "I'll leave the actual creating to you."},
        ],
        "create_project": [
            {"op": "navigate", "url": "https://app.asana.com/",
             "say": "Let me open your Asana."},
            {"op": "point",
             "sel": "text=Crea progetto || [aria-label='Nuovo progetto o portfolio'] || [aria-label='Crea']",
             "say": "To start a project, you use Create, up here."},
            {"op": "reveal",
             "sel": "text=Crea progetto || [aria-label='Nuovo progetto o portfolio']",
             "say": "That's where you pick a blank project or a template."},
            {"op": "say",
             "say": "You give it a name, choose a layout — list, board, "
                    "timeline — and add your tasks. I'll leave the rest to you."},
        ],
        "tour": [
            {"op": "navigate", "url": "https://app.asana.com/",
             "say": "Here's your Asana workspace."},
            {"op": "point", "sel": "[aria-label='Crea']",
             "say": "Create, up here, is where you add tasks and projects."},
            {"op": "point", "sel": "text=Le mie attività || [aria-label='Le mie attività']",
             "say": "My Tasks is your own to-do list across every project."},
            {"op": "say",
             "say": "Your projects are in the sidebar on the left — open one "
                    "to see its tasks. That's the lay of the land."},
        ],
    },
}


def recipe_for(site_label: str, task_key: str) -> list[dict] | None:
    """The recipe steps for (site, task), or None when there's no scripted
    route (the caller then falls back to the visual-planner walkthrough)."""
    steps = (RECIPES.get(site_label or "", {}) or {}).get(task_key or "")
    return list(steps) if steps else None


def selector_alternates(sel: str) -> list[str]:
    """Split a step's ' || '-separated selector into ordered fallbacks."""
    return [s.strip() for s in (sel or "").split("||") if s.strip()]
