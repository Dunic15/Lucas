"""Personal-first tenancy policy; gate domain→shared-org routing off.

Owner decision (2026-07-13, re-confirmed 2026-07-16): connections and
workspaces are PERSONAL; one durable uuid org per user, including logins on
a VERIFIED corporate domain. The shared-domain behavior is PARKED, not
removed: a one-row policy table gates the "verified corporate domain wins"
branch inside laura_private.ensure_user, default off. The app syncs the
LAURA_SHARED_DOMAIN_ORGS env var into that row at boot (a SQL function cannot
read a process env var) via the SECURITY DEFINER setter below; the same
exact-entry-point discipline as every other laura_private function (0004).

Existing shared orgs (e.g. the SFF org) are NOT touched: their orgs /
memberships / meeting history stay in place. A member whose only membership
was the shared org simply falls through to ensure_user's personal-org branch
on their next login and gets a fresh personal org (org + owner membership +
org_agents + the 900s free billing account). A user who OWNS an org keeps
resolving to it; for the legacy org's owner that org IS their personal org,
which also keeps the old meeting history reachable from their dashboard.

Revision ID: 0008_personal_orgs_policy
Revises: 0007_durable_artifacts
"""
from __future__ import annotations

from alembic import op

revision = "0008_personal_orgs_policy"
down_revision = "0007_durable_artifacts"
branch_labels = None
depends_on = None


_POLICY_TABLE_AND_SETTER = """
CREATE TABLE IF NOT EXISTS laura_private.policy_settings (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  shared_domain_orgs boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
INSERT INTO laura_private.policy_settings (singleton, shared_domain_orgs)
VALUES (true, false)
ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION laura_private.set_shared_domain_orgs(
  p_enabled boolean
)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
AS $function$
  INSERT INTO laura_private.policy_settings (singleton, shared_domain_orgs, updated_at)
  VALUES (true, COALESCE(p_enabled, false), pg_catalog.now())
  ON CONFLICT (singleton) DO UPDATE SET
    shared_domain_orgs = excluded.shared_domain_orgs,
    updated_at = excluded.updated_at;
$function$;

REVOKE ALL ON FUNCTION
  laura_private.set_shared_domain_orgs(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  laura_private.set_shared_domain_orgs(boolean) TO laura_app;

DO $roles$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON FUNCTION
      laura_private.set_shared_domain_orgs(boolean) FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON FUNCTION
      laura_private.set_shared_domain_orgs(boolean) FROM authenticated;
  END IF;
END
$roles$;
"""


# ensure_user, byte-compatible with 0004's contract, with ONE change: the
# "verified corporate domain wins" branch only runs when the policy row says
# shared_domain_orgs; otherwise every login resolves through the personal-org
# branch (existing owned org, else a fresh personal bundle: org + owner
# membership + org_agents laura/cedric + billing_accounts free/900s).
_ENSURE_USER_PERSONAL_FIRST = """
CREATE OR REPLACE FUNCTION laura_private.ensure_user(
  p_google_sub text,
  p_email text,
  p_name text DEFAULT ''
)
RETURNS TABLE (
  user_id uuid,
  org_id uuid,
  normalized_email text,
  created boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  v_sub text := pg_catalog.btrim(COALESCE(p_google_sub, ''));
  v_email text := pg_catalog.lower(
    pg_catalog.btrim(COALESCE(p_email, ''))
  );
  v_name text := COALESCE(p_name, '');
  v_user_id uuid;
  v_inserted_user_id uuid;
  v_org_id uuid;
  v_created boolean := false;
  v_domain text;
  v_local text;
  v_membership_status text;
  v_membership_exists boolean := false;
  v_shared_domain_orgs boolean := false;
BEGIN
  IF v_email = '' THEN
    RETURN;
  END IF;

  -- Serialize both unique identities so concurrent OAuth callbacks
  -- converge without exposing the users table to the runtime role.
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('laura:email:' || v_email, 0)
  );
  IF v_sub <> '' THEN
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended('laura:sub:' || v_sub, 0)
    );
  END IF;

  IF v_sub <> '' THEN
    SELECT u.id INTO v_user_id
      FROM public.users AS u
     WHERE u.google_sub = v_sub
     LIMIT 1;
  END IF;
  IF v_user_id IS NULL THEN
    SELECT u.id INTO v_user_id
      FROM public.users AS u
     WHERE u.email = v_email
     LIMIT 1;
  END IF;

  IF v_user_id IS NULL THEN
    v_user_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.users AS u (
      id, email, name, google_sub, provider, auth_method
    )
    VALUES (
      v_user_id, v_email, v_name, NULLIF(v_sub, ''),
      'google', 'google'
    )
    ON CONFLICT DO NOTHING
    RETURNING u.id INTO v_inserted_user_id;

    v_created := v_inserted_user_id IS NOT NULL;
    IF v_inserted_user_id IS NULL THEN
      v_user_id := NULL;
      IF v_sub <> '' THEN
        SELECT u.id INTO v_user_id
          FROM public.users AS u
         WHERE u.google_sub = v_sub
         LIMIT 1;
      END IF;
      IF v_user_id IS NULL THEN
        SELECT u.id INTO v_user_id
          FROM public.users AS u
         WHERE u.email = v_email
         LIMIT 1;
      END IF;
    END IF;
  ELSE
    UPDATE public.users AS u
       SET name = CASE WHEN v_name <> '' THEN v_name ELSE u.name END,
           email = v_email,
           google_sub = CASE
             WHEN (u.google_sub IS NULL OR u.google_sub = '')
               THEN NULLIF(v_sub, '')
             ELSE u.google_sub
           END
     WHERE u.id = v_user_id;
  END IF;

  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'identity provisioning failed';
  END IF;

  -- PERSONAL-FIRST POLICY (0008): the "verified corporate domain wins"
  -- branch is gated on the one-row policy table. Off (the default) means
  -- every login, verified domain included, resolves to a PERSONAL org
  -- below. org_domains rows are untouched; flipping the row back on
  -- restores the parked shared-domain behavior unchanged.
  SELECT ps.shared_domain_orgs INTO v_shared_domain_orgs
    FROM laura_private.policy_settings AS ps
   LIMIT 1;

  v_domain := pg_catalog.split_part(v_email, '@', 2);
  IF COALESCE(v_shared_domain_orgs, false) AND v_domain <> '' THEN
    SELECT d.org_id INTO v_org_id
      FROM public.org_domains AS d
     WHERE pg_catalog.lower(d.domain) = v_domain
       AND d.verified_at IS NOT NULL
     ORDER BY d.verified_at
     LIMIT 1;
  END IF;

  IF v_org_id IS NOT NULL THEN
    SELECT m.status INTO v_membership_status
      FROM public.memberships AS m
     WHERE m.user_id = v_user_id
       AND m.org_id = v_org_id;
    v_membership_exists := FOUND;
    IF v_membership_exists AND v_membership_status <> 'active' THEN
      RAISE EXCEPTION 'corporate membership is not active'
        USING ERRCODE = '42501';
    END IF;
    IF NOT v_membership_exists THEN
      INSERT INTO public.memberships (user_id, org_id, role, status)
      VALUES (v_user_id, v_org_id, 'member', 'active');
    END IF;
  ELSE
    SELECT m.org_id INTO v_org_id
      FROM public.memberships AS m
      JOIN public.orgs AS o
        ON o.id = m.org_id
       AND o.deleted_at IS NULL
     WHERE m.user_id = v_user_id
       AND m.role = 'owner'
     ORDER BY o.created_at
     LIMIT 1;

    IF v_org_id IS NULL THEN
      v_org_id := pg_catalog.gen_random_uuid();
      v_local := pg_catalog.split_part(v_email, '@', 1);
      IF v_local = '' THEN
        v_local := 'personal';
      END IF;

      INSERT INTO public.orgs (id, name, slug, plan)
      VALUES (
        v_org_id,
        pg_catalog.left(v_local, 80),
        'org-' || pg_catalog.left(
          pg_catalog.replace(v_org_id::text, '-', ''), 12
        ),
        'free'
      );
      INSERT INTO public.memberships (user_id, org_id, role, status)
      VALUES (v_user_id, v_org_id, 'owner', 'active');
      INSERT INTO public.org_agents (org_id, avatar_id)
      VALUES (v_org_id, 'laura'), (v_org_id, 'cedric');
      INSERT INTO public.billing_accounts (
        org_id, plan, included_seconds
      )
      VALUES (v_org_id, 'free', 900);
    END IF;
  END IF;

  RETURN QUERY SELECT v_user_id, v_org_id, v_email, v_created;
END
$function$;
"""


def upgrade() -> None:
    op.execute(_POLICY_TABLE_AND_SETTER)
    op.execute(_ENSURE_USER_PERSONAL_FIRST)


def downgrade() -> None:
    # Restore 0004's unconditional domain routing by re-running its exact
    # ensure_user body: simplest is re-creating with the gate forced on via
    # the policy row, then dropping the gate infrastructure. To avoid keeping
    # two full copies of the function in this file, downgrade re-enables the
    # branch structurally: set the row true (routing behaves as before) and
    # leave the gated function in place; the table/setter stay because the
    # function body references them. True removal happens by re-running 0004's
    # CREATE OR REPLACE (documented in docs/infra; not automated here).
    op.execute(
        "UPDATE laura_private.policy_settings SET shared_domain_orgs = true, "
        "updated_at = pg_catalog.now() WHERE singleton"
    )
