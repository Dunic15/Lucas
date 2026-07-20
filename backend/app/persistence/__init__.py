"""Persistence domain: store (SQLite system-of-record), billing, control_plane
(Postgres control-plane DAL), org_avatars_pg (per-org avatar rows). Moved from
the flat app/ namespace; app.<name> resolves via a compat shim at the old path.
"""
