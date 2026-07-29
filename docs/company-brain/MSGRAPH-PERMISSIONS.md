# Company Brain — Microsoft Graph permission & admin-consent matrix

Status: draft for the Company Brain data plane (v0). Every claim below should be
re-verified against the live Microsoft Graph permissions reference before the
first real-tenant pilot; permission names and protected-API policies change.

## Principles

1. **Application permissions, admin-consented, for sync** — the crawler runs as
   a background service, not on behalf of a signed-in user. Delegated
   permissions are used only in the onboarding UI (picking sites/teams to
   index) where a human admin is present.
2. **Least privilege by capability** — each product capability maps to the
   narrowest permission that supports it, and we do not request a permission
   until the admin enables the capability that needs it. The app registration
   uses **incremental/dynamic consent**: the base install requests only the
   metadata tier; enabling "index SharePoint" triggers a second admin-consent
   prompt for that tier only.
3. **`Sites.Selected` over `Sites.Read.All` wherever possible** — tenant-wide
   read is the single biggest objection in enterprise security review. The
   default install path grants Laura access to *zero* sites until the admin
   explicitly grants specific sites.
4. **Reading permissions requires the same tier as reading content** — ACL
   ingestion (the `permissions` facet on driveItems, site permissions, group
   membership) does not require extra write-level scopes, but it *does* require
   the directory-read tier for group expansion. That tier is mandatory for the
   ACL-security story and must be explained to the admin in those terms.

## Capability → permission matrix

Legend: **App** = application permission (admin consent always required),
**Del** = delegated permission. ✅ = required for the capability, ⛔ = never
request.

| Product capability | Graph API surface | Least-privilege App permission | Broader App permission (only if admin opts into "index everything") | Delegated (onboarding UI only) | Admin consent | Notes / risks |
|---|---|---|---|---|---|---|
| **A. Core identity & ACL resolution** (required for any capability) | `/users`, `/groups`, `/groups/{id}/transitiveMembers`, `/directoryObjects` | `User.Read.All` + `GroupMember.Read.All` | `Directory.Read.All` (⛔ avoid; superset) | `User.Read` (sign-in), `User.ReadBasic.All` (people pickers) | Yes | Needed to normalize ACL principals and expand groups. Without it, ACL evaluation cannot be done safely → capability gate: no directory tier, no indexing at all. |
| **B. SharePoint sites (scoped)** — default path | `/sites/{id}`, `/sites/{id}/drives`, `/drives/{id}/root/delta`, `/drives/{id}/items/{id}/permissions` | `Sites.Selected` (grants nothing until per-site grant) + per-site `read` role granted via `POST /sites/{id}/permissions` | — | `Sites.Read.All` (Del) so the *admin* can browse & pick sites during onboarding; their own rights apply | Yes | Per-site grants are written by the admin (or by us with `Sites.FullControl.All` — ⛔ never; instead have the admin run the grant, or use the delegated onboarding session). This is the flagship least-privilege story. |
| **C. SharePoint sites (tenant-wide)** — explicit opt-in only | same as B, plus `/sites?search=`, `/sites/getAllSites` | `Sites.Read.All` + `Files.Read.All` | — | — | Yes | Only when the admin explicitly chooses "index all sites". UI must show the blast radius before consent. |
| **D. OneDrive user drives** | `/users/{id}/drive/root/delta`, item `permissions` | **`Files.SelectedOperations.Selected`** (GA on v1.0, application mode — grants nothing until an explicit per-file/folder grant) preferred; else `Files.Read.All` | — | `Files.Read` (Del, own drive — demo/preview only) | Yes | `Files.SelectedOperations.Selected` is now GA with application mode and works on user drives, so it is the narrow default. Caveats: the grant unit is **file/folder, not drive** (no per-drive analogue to `Sites.Selected`), granting breaks inheritance, and *writing* the grants app-only needs an elevated write scope — so grant provisioning is an admin-run/delegated step. Keep **our own** crawl allowlist as defense-in-depth. (Corrected 2026-07-29 vs live [permissions-selected-overview](https://learn.microsoft.com/en-us/graph/permissions-selected-overview).) |
| **E. Outlook mail** | `/users/{id}/messages`, `/users/{id}/mailFolders/{id}/messages/delta` | `Mail.Read` (App) **+ mandatory mailbox scoping** | — | `Mail.Read` (Del) for "index only my mailbox" self-serve tier | Yes | `Mail.Read` App = every mailbox in the tenant. Scope it with **RBAC for Applications** (`New-ManagementRoleAssignment -App <SP> -Role "Application Mail.Read" -RecipientAdministrativeUnitScope/-CustomResourceScope <group>`) — **not** `New-ApplicationAccessPolicy`, which Microsoft now labels *legacy* and says new configs should not use ("replaced by Role Based Access Control for Applications"). Verify by probing an out-of-scope mailbox and expecting 403; refuse to enable mail sync until the probe fails correctly (default deny). (Corrected 2026-07-29 vs live [application-rbac](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).) |
| **F. Teams channel messages** | `/teams/{id}/channels/{id}/messages` (+ `/delta`), export API `/teams/{id}/channels/{id}/messages/getAllMessages` | `ChannelMessage.Read.All` — **protected API**: requires a Microsoft approval request | — | `ChannelMessage.Read.All` (Del) exists for interactive scenarios | Yes + Microsoft protected-API approval | Longest-lead-time item: the protected-API request form takes weeks. Ship Teams *last*. **Metering retired 2025-08-25** — the Teams message APIs are no longer metered and the `model` query param is ignored (exception: Copilot-licensed AI-insight paths), so budget only the protected-API approval, not per-message consumption. Seeded-channel scoping is ours to enforce. (Corrected 2026-07-29 vs live [export-teams-content](https://learn.microsoft.com/en-us/graph/teams-licenses).) |
| **G. Teams 1:1/group chats** | `/chats/getAllMessages` | `Chat.Read.All` (protected API) | — | — | Yes + protected-API approval | Recommend **not** requesting in v1. Chats are the highest-PII, lowest-process-value source. Explicitly out of scope until a customer demands it. |
| **H. M365 Groups (for ACLs + group sites)** | `/groups`, `/groups/{id}/sites/root` | covered by tier A (`GroupMember.Read.All` + `Group.Read.All` if group metadata needed) | — | — | Yes | Group-connected team sites are still driveItems → tier B/C covers content; tier A covers membership. |
| **I. Change notifications (webhooks)** | `/subscriptions` on drive roots, mailFolders, channels | No extra permission — subscription creation is authorized by the underlying resource permission | — | — | — | Correctness never depends on webhooks (delta queries are the source of truth); notifications only *accelerate* sync. Subscriptions need public HTTPS endpoint + lifecycle renewal. |
| **J. Sign-in for the Laura dashboard (admin + users)** | OIDC | — | — | `openid`, `profile`, `email`, `User.Read` | No (user consent) | Used to map Laura users ↔ Entra object IDs for ACL evaluation. Users who sign in with Google on an M365-connected org need an explicit identity-linking step (unresolved risk — see RISKS doc). |

## Admin-consent flow (multi-tenant)

> **Correction (2026-07-29):** the admin-consent endpoint (and app-permission
> consent generally, via `scope=…/.default`) grants consent for **all**
> application permissions registered on the app — it cannot request "tier A
> only" at runtime. Per-tier consent therefore requires one of: **(a)** a
> separate app registration per tier (recommended — the blast radius of each
> consent matches the tier), **(b)** one registration whose required
> permissions Laura grows over time, re-triggering admin consent when a new
> capability is enabled, or **(c)** one registration + Laura-side capability
> gating with all permissions consented up front (weakest least-privilege
> story). The flow below assumes **(a)/(b)**: "tier A" means *the app
> registration that has only tier-A permissions registered*.

1. Laura's app registration: **multi-tenant** (`signInAudience:
   AzureADMultipleOrgs`), publisher-verified. The base registration has only
   the tier-A permissions registered; higher tiers are separate registrations
   (or added to required-permissions when their capability is enabled).
2. Install: admin hits
   `https://login.microsoftonline.com/organizations/v2.0/adminconsent?client_id=…&scope=https://graph.microsoft.com/.default&redirect_uri=…&state=<conn_id>`
   which consents every permission registered on **that** registration (tier
   A only, because that is all it has). We record `tenant_id` from the
   redirect and create the connection in `pending_scope` state.
3. Scoping: admin signs in (delegated `Sites.Read.All`) to a site/team/mailbox
   picker; selections are stored as the connector's **scope set**; for
   SharePoint we then walk the admin through granting `Sites.Selected` per
   site (or they opt into tier C with a second consent prompt).
4. Verification probes before first crawl: attempt one in-scope read (expect
   200) and one out-of-scope read (expect 403/404). Both must pass or the
   connection stays `pending_scope`. The out-of-scope probe result is stored as
   `permission_health` evidence on the connector status page.
5. Revocation: admin deletes the service principal or revokes consent →
   Graph calls start failing 401/403 → connector enters `revoked`, sync stops,
   and (per retention policy) indexed content from that connection is
   quarantined immediately (excluded from retrieval by connection status, not
   by waiting for purge) and purged on the configured schedule.

## What we never request

- `Directory.Read.All`, `Sites.FullControl.All`, any `*.ReadWrite.*` for the
  read path — the data plane is read-only by construction. (Provisioning
  `Files.SelectedOperations.Selected`/`Sites.Selected` *grants* app-only does
  need an elevated write scope; that is an admin-run/delegated onboarding
  step, not a data-plane permission.)
- `Chat.Read.All` in v1 (see G).
- `Mail.Read` without a verified mailbox-scoping RBAC assignment in place.

## Verified against live Microsoft docs (2026-07-29)

The load-bearing claims were re-checked against learn.microsoft.com. Confirmed
current: `Sites.Selected` default-deny + `POST /sites/{id}/permissions` grant
model; driveItem `/delta` with `nextLink`/`deltaLink`; change-notification
support with delta as the reliable reconciliation path;
`GroupMember.Read.All` + `User.Read.All` sufficing for group expansion without
`Directory.Read.All`. Corrected above: OneDrive scoping (tier D — Selected
scopes now GA), mail scoping mechanism (tier E — RBAC, not the legacy policy),
Teams metering (tier F — retired), and the admin-consent granularity (`.default`
consents all registered permissions).

## Open verification tasks before pilot

- Confirm subscription max lifetimes per resource type for the renewal job.
- Confirm the exact RBAC-for-Applications role/scope names for a read-only
  mail assignment in the target tenant's Exchange Online version.
