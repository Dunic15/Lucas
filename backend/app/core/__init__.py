"""Core foundations: config (settings + REPO_ROOT), auth (login + tenant
resolve), crypto, security (rate limits + headers), entitlements (plan gates).
Moved from the flat app/ namespace; app.<name> resolves via a compat shim.
"""
