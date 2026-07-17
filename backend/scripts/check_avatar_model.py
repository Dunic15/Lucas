#!/usr/bin/env python3
"""Does this .glb work as an avatar face? — check BEFORE a live meeting.

A model that loads fine in a viewer can still be broken for us, and the failure
is invisible until someone is in a call. This encodes what we learned the hard
way (see docs + the Cedric render notes):

  - MetaPerson rigs ship per-eye eyeLookUp/Down morphs but NOT the aggregate
    `eyesLookUp`/`eyesLookDown` that TalkingHead's animate() expects. Missing
    them makes it throw EVERY frame: blink, pose and lipsync all die and the
    visible symptom is a head frozen pitched ~25° down. talk.html carries a fix
    keyed on the `AvatarHead` node — so a rig that lacks the aggregates AND
    isn't named AvatarHead gets no fix and is dead on arrival.
  - Visemes are what makes a mouth move. A high-poly model with a poor viseme
    set looks better standing still and worse doing its actual job (mpfb.glb:
    84k tris but 8 visemes vs brunette's 15 — a downgrade dressed as an
    upgrade).
  - The bot browser downloads this over the meeting path. 35MB is a real cost.

Usage:
    python3 backend/scripts/check_avatar_model.py frontend/petra.glb
    python3 backend/scripts/check_avatar_model.py --all

Exit code 0 = usable, 1 = would break or regress. Read the WARNs either way:
this reports what a face will actually DO, not whether a file parses.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

# TalkingHead drives the mouth from Oculus visemes. Verified against the three
# models we ship — RPM, Avaturn and MetaPerson all carry exactly these 15, so
# this set is the real bar, not a guess at the spec. (The vowels are I/O/U, NOT
# ih/oh/ou: getting that wrong makes every model look 12/15 and the check
# useless.)
OCULUS_VISEMES = {
    "viseme_sil", "viseme_PP", "viseme_FF", "viseme_TH", "viseme_DD",
    "viseme_kk", "viseme_CH", "viseme_SS", "viseme_nn", "viseme_RR",
    "viseme_aa", "viseme_E", "viseme_I", "viseme_O", "viseme_U",
}
# The aggregates whose absence throws every frame (the Cedric bug).
EYE_AGGREGATES = {"eyesLookUp", "eyesLookDown"}
# Reference points from the models we already ship.
TRIS_FLOOR = 20_000      # below this the silhouette reads low-poly next to cedric
SIZE_WARN_MB = 20.0      # the bot browser pays this on the meeting path


def parse_glb(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise ValueError(f"{path.name}: not a GLB (bad magic)")
    off, js = 12, None
    while off < len(data) - 8:
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        if ctype == 0x4E4F534A:
            js = json.loads(data[off + 8:off + 8 + clen].decode("utf-8"))
            break
        off += 8 + clen + ((4 - clen % 4) % 4)
    if js is None:
        raise ValueError(f"{path.name}: no JSON chunk")
    return js


def inspect(path: Path) -> dict:
    g = parse_glb(path)
    acc = g.get("accessors", [])
    verts = tris = 0
    targets: set[str] = set()
    node_names = {n.get("name", "") for n in g.get("nodes", [])}
    for m in g.get("meshes", []):
        for pr in m.get("primitives", []):
            pos = pr.get("attributes", {}).get("POSITION")
            if pos is not None:
                verts += acc[pos].get("count", 0)
            idx = pr.get("indices")
            if idx is not None:
                tris += acc[idx].get("count", 0) // 3
        for n in (m.get("extras") or {}).get("targetNames", []) or []:
            targets.add(n)
    return {
        "size_mb": path.stat().st_size / 1e6,
        "generator": (g.get("asset") or {}).get("generator", "?"),
        "verts": verts, "tris": tris,
        "targets": targets, "nodes": node_names,
        "skins": len(g.get("skins", [])),
    }


def check(path: Path) -> list[tuple[str, str]]:
    """[(level, message)] — level in {FAIL, WARN, OK}."""
    d = inspect(path)
    out: list[tuple[str, str]] = []
    t = d["targets"]

    if not d["skins"]:
        out.append(("FAIL", "no skin/armature — cannot be posed or animated"))

    visemes = {v for v in t if v in OCULUS_VISEMES}
    missing = OCULUS_VISEMES - visemes
    if not visemes:
        out.append(("FAIL", "no Oculus visemes — the mouth will never move"))
    elif missing:
        out.append((
            "WARN",
            f"{len(visemes)}/15 Oculus visemes (missing: "
            f"{', '.join(sorted(missing))}) — lipsync degrades to mumbling",
        ))
    else:
        out.append(("OK", "all 15 Oculus visemes present"))

    # THE Cedric bug: no aggregates AND not the node talk.html patches.
    has_agg = EYE_AGGREGATES <= t
    per_eye = any(x in t for x in ("eyeLookUpLeft", "eyeLookDownLeft"))
    if not has_agg:
        if "AvatarHead" in d["nodes"]:
            out.append((
                "OK",
                "no eyesLookUp/Down aggregates, but the rig has AvatarHead — "
                "talk.html's MetaPerson fix applies",
            ))
        elif per_eye:
            out.append((
                "FAIL",
                "per-eye eyeLook* morphs but NO eyesLookUp/eyesLookDown "
                "aggregates, and the rig is NOT named AvatarHead — TalkingHead's "
                "animate() will throw every frame (head frozen pitched down, "
                "dead blink/lipsync). Extend talk.html's fix to this rig first.",
            ))
        else:
            out.append(("WARN", "no eye-gaze morphs at all — eyes will not track"))
    else:
        out.append(("OK", "eyesLookUp/eyesLookDown aggregates present"))

    if d["tris"] < TRIS_FLOOR:
        out.append((
            "WARN",
            f"{d['tris']:,} tris — reads low-poly beside cedric.glb (48,659). "
            f"Not broken, just visibly cheaper.",
        ))
    else:
        out.append(("OK", f"{d['tris']:,} tris"))

    if d["size_mb"] > SIZE_WARN_MB:
        out.append((
            "WARN",
            f"{d['size_mb']:.1f} MB — the bot browser downloads this on the "
            f"meeting path; laura/cedric are ~12-14 MB",
        ))

    print(f"\n=== {path.name}  ({d['size_mb']:.1f} MB, {d['generator']})")
    print(f"    verts={d['verts']:,} tris={d['tris']:,} "
          f"morphs={len(t)} visemes={len(visemes)}/15")
    return out


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    if "--all" in argv:
        paths = sorted((root / "frontend").glob("*.glb"))
    else:
        args = [a for a in argv if not a.startswith("-")]
        if not args:
            print(__doc__)
            return 2
        paths = [Path(a) if Path(a).is_absolute() else root / a for a in args]

    worst = 0
    for p in paths:
        if not p.exists():
            print(f"!! {p} does not exist")
            worst = 1
            continue
        try:
            for level, msg in check(p):
                print(f"    [{level:4}] {msg}")
                if level == "FAIL":
                    worst = 1
        except Exception as e:  # noqa: BLE001 — a malformed file is a result
            print(f"    [FAIL] unreadable: {e}")
            worst = 1
    print()
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
