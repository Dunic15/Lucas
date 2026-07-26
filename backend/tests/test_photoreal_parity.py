"""P0 renderer contract: stop parity and identity-safe face fallbacks.

Browser/GPU behavior is kept framework-free, so these tests lock the executable
protocol seams and exercise backend readiness/404 behavior without vendor keys.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, main  # noqa: E402
from app.api import pages  # noqa: E402  (page routes extracted from main)

ROOT = Path(__file__).resolve().parents[2]
PHOTO = (ROOT / "frontend" / "photoreal.html").read_text()
TALK = (ROOT / "frontend" / "talk.html").read_text()
GPU = (ROOT / "gpu" / "server.py").read_text()


def test_photoreal_obeys_generation_safe_stop_and_drops_late_frames():
    assert 'msg.type === "stop"' in PHOTO
    assert "stopPlayback(genOf(msg))" in PHOTO
    assert "staleBefore = Math.max" in PHOTO
    assert "dropNextBinary" in PHOTO
    assert "Preserve dropNextBinary" in PHOTO
    assert "epoch !== playbackEpoch" in PHOTO
    assert 'type: "stop", generation_id: generation' in PHOTO


def test_photoreal_reuses_server_audio_and_reports_real_playback():
    assert "payload.audio ? Promise.resolve(payload.audio)" in PHOTO
    assert "if (payload.audio) sayOne(text, payload)" in PHOTO
    assert "/avatar/speaking/" in PHOTO
    assert "await new Promise((resolve) => {" in PHOTO
    assert "!!activeSource || pendingAudio.length > 0 || waitingFirstFrame" in PHOTO


def test_photoreal_hand_raise_parity():
    assert 'msg.type === "raise_hand"' in PHOTO
    assert 'msg.type === "lower_hand"' in PHOTO
    assert 'id="raisedHand"' in PHOTO


def test_gpu_stop_cancels_between_frames_and_invalidates_queue():
    assert 'if kind == "stop":' in GPU
    assert "command_epoch += 1" in GPU
    assert "item_epoch != command_epoch" in GPU
    assert '"cancelled": cancelled' in GPU


def test_gpu_unknown_avatar_fails_closed():
    assert "if aid not in engines:" in GPU
    assert '"type": "face_unavailable"' in GPU
    assert "engines.get(aid, engine)" not in GPU
    assert "or next(iter(engines))" not in GPU


def test_talk_never_borrows_lauras_model_for_another_avatar():
    assert 'throw new Error("face_unavailable")' in TALK
    assert 'return "/laura.glb"' not in TALK
    assert 'params.get("conversation_id") ? null : params.get("avatar_url")' in TALK


def test_specific_missing_portrait_is_explicitly_unavailable():
    response = pages.photoreal_reference("definitely-not-an-avatar")
    assert response.status_code == 404
    assert b"face_unavailable" in response.body


def test_customer_avatars_publish_explicit_renderer_readiness():
    laura = avatars.load("laura").renderer_readiness
    cedric = avatars.load("cedric").renderer_readiness
    # Photoreal shelved (owner 2026-07-22: Runpod credit parked at $1.67 and
    # the pod stopped — an accidental wake would burn it). Laura runs the free
    # 3D renderer; the photoreal ASSETS stay ready so `face: photoreal` in
    # avatar.yaml re-enables Ultra-HD with zero other changes.
    assert laura["preferred"] == "talk"
    assert laura["fallback"] == "talk"
    assert laura["photoreal"]["asset"] == "reference-laura.jpg"
    assert laura["talk"]["asset"] == "laura.glb"
    # Cedric wears the hologram tier (owner 2026-07-26): /robot projects his
    # own cedric.glb, so his readiness rides on the same talk asset.
    assert cedric["preferred"] == "robot"
    assert cedric["fallback"] == "photoreal"
    assert cedric["talk"]["asset"] == "cedric.glb"
    assert cedric["photoreal"]["asset"] == "reference-cedric.jpg"
    assert laura["talk"]["ready"] is True
    assert laura["photoreal"]["ready"] is True
    assert cedric["talk"]["ready"] is True
    assert cedric["photoreal"]["ready"] is True
    assert laura["ready"] is True
    assert cedric["ready"] is True


def test_internal_duccio_avatar_stays_off_the_product_roster():
    assert avatars.is_internal("duccio") is True
    assert "duccio" not in avatars.list_ids()


def test_renderer_javascript_and_gpu_python_compile():
    compile(GPU, "gpu/server.py", "exec")
    scripts = [
        ("photoreal.js", re.findall(r"<script>(.*?)</script>", PHOTO, re.S)[0]),
        ("talk.mjs", re.findall(
            r'<script type="module">(.*?)</script>', TALK, re.S
        )[0]),
    ]
    for name, source in scripts:
        suffix = ".mjs" if name.endswith(".mjs") else ".js"
        with tempfile.NamedTemporaryFile(
            "w", suffix=suffix, encoding="utf-8"
        ) as handle:
            handle.write(source)
            handle.flush()
            result = subprocess.run(
                ["node", "--check", handle.name],
                capture_output=True,
                text=True,
                check=False,
            )
        assert result.returncode == 0, result.stderr
