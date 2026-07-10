"""Meeting-bound Runpod GPU (il gemello Runpod di gpu_runtime.py, che è EC2).

Attivo quando RUNPOD_API_KEY e RUNPOD_POD_ID sono impostati. A differenza del
gemello EC2 (gated sul GLOBALE avatar_page), qui il gate è PER-AVATAR: il pod
si sveglia solo se l'avatar invitato ha `face: photoreal` (Avatar.page) — così
Cedric in 3D non accende mai la GPU, Laura sì.

  - parte una sessione con avatar photoreal -> podResume (fire-and-forget; la
    pagina photoreal resta sul ritratto statico finché lo stream non arriva,
    poi si aggiorna da sola — è il suo comportamento nativo)
  - finisce l'ultima sessione -> podStop dopo runpod_idle_stop_minutes di
    grazia (annullato se nel frattempo parte un'altra riunione)

Pod FERMATO ≠ terminato: il disco resta (centesimi/giorno), il resume ~60-90s
e l'URL del proxy non cambia. Ogni chiamata è best-effort in un thread daemon:
un errore Runpod non può mai bloccare o rompere il percorso live.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from typing import Callable, Optional

from .config import settings

_API = "https://api.runpod.io/graphql"

_lock = threading.Lock()
_stop_timer: Optional[threading.Timer] = None
_count_active: Callable[[], int] = lambda: 0


def configure(count_active: Callable[[], int]) -> None:
    """Contatore sessioni vive: lo stop schedulato ri-verifica prima di fermare."""
    global _count_active
    _count_active = count_active


def enabled() -> bool:
    return bool(settings.runpod_api_key and settings.runpod_pod_id)


def _gql(query: str) -> dict:
    req = urllib.request.Request(
        f"{_API}?api_key={settings.runpod_api_key}",
        data=json.dumps({"query": query}).encode(),
        headers={
            "Content-Type": "application/json",
            # Cloudflare davanti a api.runpod.io banna lo UA di default
            # "Python-urllib" (403, error 1010): serve uno UA esplicito.
            "User-Agent": "laura-backend/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def on_session_started(avatar_page: str) -> None:
    """Chiamata all'invio del bot. Mai bloccante sul percorso di join."""
    if not enabled() or avatar_page != "photoreal":
        return
    global _stop_timer
    with _lock:
        if _stop_timer is not None:
            _stop_timer.cancel()
            _stop_timer = None
    threading.Thread(target=_resume_pod, daemon=True).start()


def _resume_pod() -> None:
    try:
        out = _gql(
            f'mutation {{ podResume(input: {{podId: "{settings.runpod_pod_id}", '
            f"gpuCount: 1}}) {{ id desiredStatus }} }}"
        )
        status = (out.get("data") or {}).get("podResume") or {}
        print(f"[runpod] resume richiesto: {settings.runpod_pod_id} "
              f"-> {status.get('desiredStatus', out.get('errors'))}", flush=True)
    except Exception as e:  # noqa: BLE001 — solo infra, mai verso il meeting
        print(f"[runpod] resume fallito ({type(e).__name__} "
              f"{getattr(e, 'code', '')}) — la pagina resta "
              f"sul fallback", flush=True)


def on_session_ended(active_count: int) -> None:
    """Dopo la finalize di una sessione: ferma il pod quando nulla lo usa."""
    if not enabled() or active_count > 0:
        return
    global _stop_timer
    with _lock:
        if _stop_timer is not None:
            _stop_timer.cancel()
        _stop_timer = threading.Timer(
            settings.runpod_idle_stop_minutes * 60, _stop_if_still_idle
        )
        _stop_timer.daemon = True
        _stop_timer.start()
    print(f"[runpod] nessuna sessione attiva — stop pod fra "
          f"{settings.runpod_idle_stop_minutes} min", flush=True)


def _stop_if_still_idle() -> None:
    try:
        if _count_active() > 0:
            print("[runpod] stop annullato — nuova sessione attiva", flush=True)
            return
        _gql(
            f'mutation {{ podStop(input: {{podId: "{settings.runpod_pod_id}"}}) '
            f"{{ id desiredStatus }} }}"
        )
        print(f"[runpod] stop richiesto: {settings.runpod_pod_id}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[runpod] stop fallito ({type(e).__name__} "
              f"{getattr(e, 'code', '')}) — fermarlo a mano "
              f"da console.runpod.io", flush=True)
