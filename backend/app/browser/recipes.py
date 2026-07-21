"""Scripted, deterministic how-to walkthroughs for known sites (Sable B3+).

The visual planner is great at open-ended browsing but unreliable at driving a
specific multi-step flow through a complex SaaS app. For the how-to
walkthroughs a meeting asks for ("show me how to create a task"), a scripted
RECIPE against confirmed, stable selectors is reliable where the planner
stalls. Each recipe is a short list of read-only steps:

  navigate; go to a page (domain-allowlist enforced by the caller)
  point; glide the visible cursor to a control and pulse (no click)
  reveal; click a control that only OPENS a form/menu (never submits)
  say; narration only (the avatar speaks it)

Recipes are STRICTLY read-only: they point and open, they never type or submit,
so nothing in the user's workspace changes. Selectors were captured from the
live app (Italian UI here; aria-labels/text are language-specific, so keep a
few fallbacks per control). A step whose control isn't found is skipped
gracefully; the narration still lands, the walkthrough never hard-fails.
"""
from __future__ import annotations

# {site_label: {task_key: [steps]}}. Each step: op + (url|sel) + say.
# `sel` may carry alternates separated by " || ": the runner tries each.
# STRICTLY READ-ONLY: `reveal` only OPENS things that don't persist (the Create
# form, a task's detail view). Action controls that would CHANGE the workspace
# (mark complete, save) are `point`-only; shown, never clicked.
_ASANA_HOME = "https://app.asana.com/"
# Opening any task's detail view; read-safe (just views it).
_OPEN_TASK = ("[aria-label^='Apri modale'] || [aria-label^='Open task'] "
              "|| [aria-label*='task actions'] || div[role='row'] a[href*='/task/']")

RECIPES: dict[str, dict[str, list[dict]]] = {
    "asana": {
        "create_task": [
            {"op": "navigate", "url": _ASANA_HOME,
             "say": "Let me open your Asana."},
            {"op": "point", "sel": "text=Crea attività || text=Create task || text=Add task || [aria-label='Crea'] || [aria-label='Create'] || [aria-label='Quick add']",
             "say": "To create a task, you start with Create, up here."},
            {"op": "reveal", "sel": "text=Crea attività || text=Create task || text=Add task || [aria-label='Crea'] || [aria-label='Create'] || [aria-label='Quick add']",
             "say": "That opens a new task."},
            {"op": "point",
             "sel": "[aria-label='Nome attività'] || [aria-label='Task name'] || [placeholder*='Nome attività'] || [placeholder*='Task name'] || [placeholder*='task name']",
             "say": "Here's where you type the task's name."},
            {"op": "point", "sel": "[aria-label='Data di scadenza'] || [aria-label='Due date']",
             "say": "This is where you set the due date."},
            {"op": "point", "sel": "[aria-label='Descrizione'] || [aria-label='Description']",
             "say": "And any details go here."},
            {"op": "say",
             "say": "Then you assign it and save — and it's on the board. "
                    "I'll leave the actual creating to you."},
        ],
        "create_project": [
            {"op": "navigate", "url": _ASANA_HOME,
             "say": "Let me open your Asana."},
            {"op": "point",
             "sel": "text=Crea progetto || text=Create project || [aria-label='Nuovo progetto o portfolio'] || [aria-label='Crea'] || [aria-label='Create']",
             "say": "To start a project, you use Create, up here."},
            {"op": "reveal",
             "sel": "text=Crea progetto || text=Create project || [aria-label='Nuovo progetto o portfolio']",
             "say": "That's where you pick a blank project or a template."},
            {"op": "say",
             "say": "You give it a name, choose a layout — list, board, "
                    "timeline — and add your tasks. I'll leave the rest to you."},
        ],
        "tour": [
            {"op": "navigate", "url": _ASANA_HOME,
             "say": "Here's your Asana workspace."},
            {"op": "point", "sel": "[aria-label='Crea']",
             "say": "Create, up here, is where you add tasks and projects."},
            {"op": "point", "sel": "text=Le mie attività || text=My tasks || [aria-label='Le mie attività'] || [aria-label='My tasks']",
             "say": "My Tasks is your own to-do list across every project."},
            {"op": "point", "sel": "[aria-label='Cerca'] || [aria-label='Search'] || [placeholder='Cerca'] || [placeholder='Search']",
             "say": "Search up top finds any task or project fast."},
            {"op": "say",
             "say": "Your projects are in the sidebar on the left — open one "
                    "to see its tasks. That's the lay of the land."},
        ],
        "my_tasks": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "point", "sel": "text=Le mie attività || text=My tasks || [aria-label='Le mie attività'] || [aria-label='My tasks']",
             "say": "My Tasks, here, is your personal to-do list."},
            {"op": "reveal", "sel": "text=Le mie attività || text=My tasks || [aria-label='Le mie attività'] || [aria-label='My tasks']",
             "say": "It pulls together everything assigned to you across every "
                    "project, and you can sort it by due date or by project."},
        ],
        "add_section": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": "[aria-label^='Apri progetto'] || [aria-label^='Open project'] || text=Monitoraggio dei compiti || a[href*='/project/']",
             "say": "Open a project — sections organise its tasks into groups."},
            {"op": "point", "sel": "text=Aggiungi sezione || text=Add section || [aria-label='Aggiungi sezione'] || [aria-label='Add section']",
             "say": "Add section, here, creates a new group — like To do, "
                    "Doing, Done — and you drag tasks into it."},
        ],
        "add_task_in_project": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": "[aria-label^='Apri progetto'] || [aria-label^='Open project'] || text=Monitoraggio dei compiti || a[href*='/project/']",
             "say": "Inside a project,"},
            {"op": "point", "sel": "text=Aggiungi attività || text=Add task || [aria-label='Aggiungi attività'] || [aria-label='Add task']",
             "say": "Add task, here, drops a new task straight into this "
                    "project — type the name and hit enter."},
        ],
        "add_comment": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": _OPEN_TASK,
             "say": "Open any task to see its detail panel."},
            {"op": "point", "sel": "[aria-label='Commenta'] || [aria-label='Comment'] || [aria-label='Add a comment'] || [aria-label='Modifica commento'] || text=Commenta || [placeholder*='comment']",
             "say": "Down here is where you comment — @-mention someone and "
                    "they get notified. That's how the discussion stays on the "
                    "task itself."},
        ],
        "add_subtask": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": _OPEN_TASK, "say": "Open a task,"},
            {"op": "point", "sel": "[aria-label='Aggiungi sottoattività'] || [aria-label='Add subtask']",
             "say": "and Add subtask, here, breaks it into smaller steps, each "
                    "with its own assignee and due date."},
        ],
        "set_due_date": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": _OPEN_TASK, "say": "Open a task,"},
            {"op": "point", "sel": "[aria-label='Data di scadenza'] || [aria-label='Due date']",
             "say": "and Due date, here, is where you set when it's due — you "
                    "can even give it a start-to-end range."},
        ],
        "assign_task": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": _OPEN_TASK, "say": "Open a task,"},
            {"op": "point",
             "sel": "[aria-label^='Aggiungi o rimuovi collaboratori'] || [aria-label^='Add or remove collaborators'] || [aria-label*='Assignee'] || [aria-label*='Assegnatario']",
             "say": "and the assignee and collaborators go here — pick who owns "
                    "it and who should follow along."},
        ],
        "complete_task": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "reveal", "sel": _OPEN_TASK, "say": "Open a task,"},
            {"op": "point",
             "sel": "[aria-label^='Contrassegna come completata'] || [aria-label^='Mark complete'] || [aria-label^='Mark as complete']",
             "say": "and this check — Mark complete — closes it out. I'm just "
                    "pointing, not clicking, so nothing changes."},
        ],
        "invite_member": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "point",
             "sel": "text=Invita colleghi del team || text=Invita un collega || text=Invite teammates || text=Invite || [aria-label*='Invite']",
             "say": "Invite, down here, adds a teammate — you type their email "
                    "and they can see and be assigned tasks."},
        ],
        "create_portfolio": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "point", "sel": "text=Portfolio || [aria-label='Portfolio'] || text=Portfolios",
             "say": "Portfolios, here, group several projects so you can watch "
                    "their status in one place — good for a program view."},
        ],
        "search": [
            {"op": "navigate", "url": _ASANA_HOME, "say": "Opening your Asana."},
            {"op": "point", "sel": "[aria-label='Cerca'] || [aria-label='Search'] || [placeholder='Cerca'] || [placeholder='Search']",
             "say": "Search, up top, jumps to any task, project, or person — "
                    "and you can build saved searches for advanced filters."},
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
