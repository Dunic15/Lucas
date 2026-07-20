"""Persistence domain: store (the SQLite system-of-record — sessions, artifacts,
ledger rows, orgs, tokens) and billing (metering/entitlement storage). Moved
from the flat app/ namespace; app.store / app.billing still resolve via a
compatibility shim at the old path.
"""
