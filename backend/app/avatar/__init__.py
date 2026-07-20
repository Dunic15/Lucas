"""Avatar-identity domain: avatars (roster/loading), avatar_resolver
(session->avatar resolution), avatar_overlay (per-org overlays). Package is
named ``avatar`` (singular) on purpose: a package at app/avatars/ would shadow
the compat shim at app/avatars.py and break the flat-name identity contract.
"""
