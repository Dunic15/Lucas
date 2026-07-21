# Access & Security Provisioning: Standard Operating Procedure

**Process owner:** IT / Security
**Applies to:** Any request to grant, change, or revoke system access
**Last reviewed:** 2026-02

## Required steps

1. **Access request filed**: via the access request form. Must name the
   requester, the person receiving access, the systems, and the access level.
2. **Manager approval**: the receiving person's manager approves the request.
   Standard access stops here.
3. **Security review for elevated access**: admin, production, or
   data-export access requires a documented Security lead approval. No elevated
   access is granted without it.
4. **Provisioning**: IT grants access through SSO. Direct (non-SSO) grants are
   not allowed except for documented break-glass cases.
5. **Evidence recorded**: the approval and the provisioning action are logged
   with a ticket id. This is the audit trail.
6. **Quarterly access review**: all elevated access is re-reviewed every
   quarter; unused access is revoked.

## Approvals required

- **Standard access**: manager approval.
- **Elevated access (admin/prod/data export)**: manager approval **and**
  Security lead approval, both documented.

## Owners

- IT owns provisioning and the ticket/evidence record.
- Security lead owns elevated-access approval and the quarterly review.
- The receiving person's manager owns the business justification.

## Definition of done

Access is correctly granted only when the request, the required approval(s),
and the provisioning action are all recorded against one ticket id.

## Common gaps

- Elevated access granted without the documented Security lead approval.
- No ticket id / missing evidence for the grant.
- Access granted outside SSO without a break-glass justification.
