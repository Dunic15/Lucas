"""Core foundations: config (settings), auth (bearer/session + tenant resolve),
crypto (encryption helpers), security (guards), entitlements (plan/limits).
Moved from the flat app/ namespace; app.<name> still resolves via a
compatibility shim at the old path.
"""
