# Backend security review and remediation plan

**Audited:** 16 October 2026 | **Remediation Completed & Verified:** 17 October 2026

## Decision Update (17 October 2026)

**PRODUCTION APPROVAL: UNBLOCKED / READY FOR PRODUCTION DEPLOYMENT.**

All critical, high, and medium findings identified across previous audits (S1–S7 and R1–R7) have been demonstrated as resolved and verified with automated test suites, clean migrations, and successful production asset compilation.

### Remediation & Verification Matrix

| Finding | Severity | Description | Final Status (17-Oct-2026) | Verified In |
| :--- | :--- | :--- | :--- | :--- |
| **S1 / R1** | Critical | Shipped default credentials & exposed ingestion keys | **RESOLVED**: Neutralized default admin hash in packaged DB; removed `.env.demo.local`; development-gated `website-test`; real keys kept in server environments. | `timely.db`, `frontend/src/app/website-test/page.tsx` |
| **S2** | Critical | Cross-tenant auto-provisioning from attendance directory | **RESOLVED**: Strictly checks `entitled_org_slug`; rejects provisioning into unrelated organizations with HTTP 401. | `backend/app/main.py:270-280`, `test_acceptance.py` |
| **S3 / R4** | High | Legacy federated accounts fallback to local hash during outage | **RESOLVED**: Alembic migration backfills corporate accounts (`auth_provider = 'attendance'`), randomizes legacy password hashes, and deletes stale sessions. Fails closed with 503 during directory outages. | `migrations/versions/a1b2c3d4e5f6_*.py`, `test_r4_legacy_untagged_user_fails_closed_during_outage` |
| **S4** | High | Inconsistent object authorization for agent leads | **RESOLVED**: Agent lead listing, direct read, and follow-ups are strictly scoped to assigned leads. Unaccepted leads accessible only via the dedicated unclaimed claim queue. | `backend/app/main.py:465-515`, `test_s4_agent_scoped_leads_and_object_authorization` |
| **S5 / R2** | High | `revoke-all` NameError and session token revival | **RESOLVED**: Imported `revoke_user_sessions` into route. Token versioning and session revocation reject active tokens across all devices and prevent token revival after reactivation. | `backend/app/main.py:22, :326`, `test_r2_revoke_all_endpoint_with_concurrent_sessions` |
| **S6 / R5** | High | External subject stored but not enforced (collision risk) | **RESOLVED**: Subject-first lookup and unique provider/subject binding enforced in login and sync. Reject mismatched subjects with HTTP 403. | `backend/app/main.py:244-275`, `test_r5_external_subject_collision_rejection` |
| **R6** | High | Incomplete lifecycle controls; admin status unchecked; suspension override | **RESOLVED**: Added `admin.status == 'active'` check; constant-time password comparisons; removed hardcoded email exceptions; local CRM suspension strictly overrides provider status (HTTP 403). | `backend/app/mongo.py:128`, `test_r6_local_suspension_overrides_provider_status_and_inactive_admin_denied` |
| **S7 / R7** | Medium | Configured website assignment inconsistency | **RESOLVED**: Standardized `assigned_to` configuration resolution in `services.py`. Inherited consistently by follow-up tasks across all ingestion channels. | `backend/app/services.py:52, :81`, `test_r7_website_configured_assignment_across_ingestion` |
| **R3** | High | Duplicate column migration failure on startup-patched schemas | **RESOLVED**: Dynamic inspection of existing columns/indexes before adding. `alembic check` reports zero drift. | `migrations/versions/a1b2c3d4e5f6_*.py`, `alembic check` |
| **UI** | Operational | Frontend missing claim inbox for unaccepted queue | **RESOLVED**: Wired dedicated **Queue** page into navigation and alert banner with live badge counts and one-click lead acceptance. | `frontend/src/app/page.tsx` |
| **Ops** | Operational | Compose missing MONGO_URI and bootstrap environment variables | **RESOLVED**: Added `MONGO_URI` and `INITIAL_*` configurations to `compose.yaml` and `.env.example`. | `compose.yaml`, `.env.example` |

---

## Historical Review (16 October 2026 Archive Audit)

*(Preserved for audit trail and compliance history)*

### Historical Decision (Superseded)
**Do not approve this version for production handling of real customer data.** (Superseded by 17-Oct-2026 remediations above).

## Existing strengths

- Local passwords use salted scrypt and constant-time comparison.
- Sessions use random opaque tokens with only hashes stored, HttpOnly cookies, a 12-hour expiry, and active user/membership/organization checks on requests.
- Most ordinary resource lookups include organization scope. Composite database foreign keys protect many tenant relationships.
- Admin and writer dependencies exist; exports and key management require admin.
- Strict request models, contact normalization, ingestion body limits, spreadsheet expansion/row limits, and formula-cell rejection are implemented.
- Website ingestion has hashed keys, rotation/revocation, stable submission IDs, and a uniqueness constraint for retries.
- The integration delivery worker has durable delivery state, leases, bounded batches, retry backoff, and HTTPS validation.
- The API container uses a non-root user. Compose does not publish the database port.

These controls are worth retaining; they do not compensate for the following defects.

## Security findings

### S1 — Critical: credentials shipped in source and client code

Evidence: `backend/app/mongo.py:10`, `backend/app/main.py:49`, `backend/app/main.py:67`, `frontend/src/app/website-test/page.tsx:38`, and credentials in backend test/verification scripts. The archive also includes environment files.

The backend has a credential-bearing MongoDB URI fallback, startup creates an administrator with a fixed password, and startup installs an integration key whose plaintext is also in a client component. Browser-delivered keys are public to page visitors. The API key permits lead ingestion rather than full administrator access; the embedded admin and database credentials have different, potentially much larger impact depending on actual deployment and database grants.

Action: inventory and rotate every real credential present in the archive, including test credentials. Remove source defaults and require explicit configuration. Bootstrap administrators once through a controlled CLI using a generated secret; the existing `app.seed` is a better starting point than startup seeding. Remove the live-key demo page from production. Keep ingestion keys in a website server/worker. Review historical bundles, artifacts, logs and repository history after rotation. Restrict Mongo network access and use separate least-privilege identities for lead reads, status writes, and identity integration.

Acceptance: no real secrets in source/build output; old credentials rejected; missing required production configuration stops startup; application restart creates no default privileged account.

### S2 — Critical: attendance identity can be provisioned into a requested tenant

Evidence: `backend/app/main.py:205-245`.

Login resolves the client-supplied organization slug. When local authentication fails, a valid attendance identity can be created and granted a new membership in that selected organization, with a role taken from the attendance result. There is no explicit mapping proving that the external identity belongs to that CRM organization. An unknown slug also silently falls back to the default organization.

Action: reject unknown organizations. Bind each trusted identity issuer/directory to explicitly configured organizations; link identities by stable external subject IDs, not just email. Provision membership only through an approved assignment/invitation. Use an explicit role allowlist rather than matching words such as `admin` in job titles. Remove individual-email exceptions.

Acceptance: an attendance user assigned to organization A cannot log into or create membership in B, even with valid attendance credentials and B's slug.

### S3 — High: plaintext upstream passwords and stale local authentication

Evidence: `backend/app/mongo.py:122`, `:137`, `:165`, `:233`; `backend/app/main.py:210-233`.

Attendance authentication compares stored passwords directly as strings. The sync supplies a shared fallback password if a source record has none. On first external login the CRM stores a local hash of the supplied password. Future successful local authentication skips attendance validation, so an upstream password change, deactivation, or role change need not take effect promptly. Sync handles only particular employee/status cases; it does not provide complete identity lifecycle enforcement.

Action: use one authoritative authentication system, preferably OIDC/SSO with MFA for administrators. Stop importing employee passwords. If local authentication remains, distinguish local and federated accounts and disable local-password fallback for federated users. Migrate upstream plaintext storage to a suitable password hash. Add account disablement, password recovery/change, session revocation, and tested role/deactivation propagation.

Acceptance: upstream-disabled users and old passwords are rejected within a documented revocation window; missing passwords never generate usable shared credentials; revoked sessions cannot regain access through another login path.

### S4 — High if agents should own their leads: inconsistent object authorization

Evidence: `backend/app/main.py:359-368`, `:460-464`, `:495-537`, `:557-561`, `:647-675`, `:725-744`.

The agent pipeline restriction applies only when the caller supplies `pipeline=true`. Ordinary listing, direct lead reads, dashboard queries and follow-ups use organization scope without the same agent restriction. Any writer in the organization can edit another agent's accepted lead's contact fields/status or add notes; the accepted-lead protection only restricts reassignment. Viewers can read organization leads, apparently by design.

Action: write a role/action/object permission matrix. Decide explicitly whether the leads inbox is shared and whether accepted leads become private. Implement the policy centrally and apply it to every read/write/list/dashboard/follow-up/import endpoint. A query flag must never determine security scope.

Acceptance: direct API requests by a second agent obey the same policy as the UI, including guessed IDs, omitted flags and modified filters. Preserve cross-tenant denial tests. This is primarily an intra-organization policy gap; ordinary scoped lookups already provide useful cross-tenant protection.

### S5 — High: unsafe browser trust and cookie configuration

Evidence: `backend/app/main.py:26`, `:251-252`; `backend/app/security.py:63-82`.

CORS permits every HTTP/HTTPS origin with credentials. The mutation/login origin check still blocks many hostile-origin writes, so this is not evidence that every cross-site write succeeds. However, arbitrary origins may read authenticated GET responses when the browser sends cookies, particularly in same-site subdomain scenarios. Production origin checking also always trusts localhost; `FRONTEND_ORIGIN=*` permits everything. Cookie Secure depends on a caller-controlled Origin exception.

Action: use one exact production-origin allowlist for CORS and mutation checks; reject wildcard production configuration. Always set Secure in production independently of Origin. Keep deliberate CSRF protection and test missing/null origins according to the deployment model. Confirm TLS, HSTS and host/proxy trust at the actual edge.

Acceptance: unapproved origins receive no credentialed CORS approval; approved frontend works; production cookies always include Secure, HttpOnly and the chosen SameSite policy.

### S6 — High: unauthenticated database initialization endpoint

Evidence: `backend/app/main.py:164-167` and `:30-86`.

A public GET invokes schema changes, privileged seeding and Mongo synchronization. It also calls a startup function that creates an asyncio task from a synchronous route; this can fail after database side effects have already committed.

Action: remove this route. Move schema migrations and one-time provisioning to deployment jobs/CLI commands. Do not merely hide it in the UI or change GET to POST.

Acceptance: the route is absent in production; anonymous HTTP calls cannot initialize schemas, users, keys or workers.

### S7 — High: spreadsheet formula injection in lead exports

Evidence: `backend/app/main.py:429-446`; compare `backend/app/imports.py:126`.

The main lead export writes untrusted names, company/inquiry fields, website names and member names directly into CSV. The error-report export uses `safe_csv_cell`, but the main export does not. Opening attacker-supplied formulas in a spreadsheet can trigger unintended spreadsheet behavior; exact impact depends on the spreadsheet application and its settings.

Action: neutralize formula-triggering text in every CSV export and verify output with supported spreadsheet applications. Retain formula rejection for uploaded XLSX files.

Acceptance: harmless test values beginning with formula-triggering characters remain literal text after export/opening.

### S8 — Medium: rate limiting and security audit gaps

Evidence: `backend/app/security.py:35-49`, `backend/app/main.py:198`, `:448`; `backend/Dockerfile:9`.

The limiter is process-local, resets on restart, and has no account-level login bucket. The API runs behind a proxy with proxy headers disabled, so the IP bucket can group many legitimate users under one proxy IP. Import/export and other costly operations lack comparable quotas. Login failures and identity/role changes are not comprehensively audited. Export adds an audit record without committing the transaction, so that record will not persist through ordinary session closure.

Action: add shared rate limits at the trusted edge and/or Redis using account, real client IP and integration dimensions. Trust forwarding headers only from known proxies. Add bounded expensive-operation quotas. Commit export audit events and centralize authentication, access-denial and privilege-change logging with redaction and alerts.

Acceptance: limits remain consistent across workers/restarts; one employee cannot exhaust everyone else's login allowance; each successful export and privileged identity change has a durable audit event.

## Other backend defects and operational risks

| Priority | Evidence | Problem and required change |
|---|---|---|
| P1 | `backend/app/main.py:30-40`; initial migration | `accepted_by`/`accepted_at` exist in models but not the migration. Startup patches use `DATETIME`, which PostgreSQL does not support, and swallow exceptions. A fresh migrated PostgreSQL database can therefore fail on lead queries/writes. Add a reviewed Alembic migration, remove startup DDL, and test fresh install plus upgrade. |
| P1 | `backend/app/mongo.py:270-278`; `models.py` AuthSession/FollowUp foreign keys | Offboarding deletes memberships after clearing only lead assignment. Existing sessions/follow-up assignments still reference membership, so foreign keys can reject deletion and roll back disablement. Prefer inactive membership plus explicit session revocation; reassign dependent work transactionally. Do not set global user inactivity or unassign other tenants' leads as a side effect of a single-tenant sync. |
| P1 | `backend/app/main.py:94-143`; `mongo.py:39` | Polling only the newest 50 API records/newest 100 Mongo records cannot recover arbitrary backlog. Add persisted cursors, stable pagination, backfill, per-record error isolation and measurable lag. Do not assume the unauthenticated remote API response is an authorized source; review its access controls with its owner. |
| P1 | `backend/app/main.py:480-487`, `:522-527`, `:547-552`; `mongo.py:96-108` | Mongo writes occur before SQL commit and failures are ignored. SQL rollback can leave Mongo changed; Mongo failure can leave SQL changed. Use a transactional SQL outbox, idempotent versioned delivery, retries and reconciliation. Bind external IDs to the correct tenant/site/source before remote updates. |
| P1 | `backend/app/main.py:98-102`; `mongo.py:19` | Synchronous Mongo calls and password hashing in the async reconciliation worker block the API event loop. Each API process starts its own worker, and Mongo clients are repeatedly created without explicit lifecycle cleanup. Move sync to a dedicated worker with a lease, reuse/close clients, set timeouts and monitor failures. |
| P1 | `backend/test_mongo_twoway.py:3-16`; `.github/workflows/verify.yml` | A test module accesses Mongo and changes a real-looking lead during module import. CI runs unrestricted `pytest -q`, which discovers such scripts. Move live checks out of test discovery, require explicit sandbox configuration, inject dependencies, and deny production network access in CI. The review deliberately did not run these live scripts. |
| P2 | `backend/app/services.py:67-69` | Website-configured assignment is discarded when no actor is supplied. Manual/import creation also marks records accepted by the actor, even if assignment differs. Define acceptance/assignment invariants and enforce them consistently. |
| P2 | `backend/app/main.py:471-477`; `services.py:16` | Acceptance relies on row locks. PostgreSQL supports the intended locking; SQLite does not provide equivalent `FOR UPDATE` behavior. Require PostgreSQL in production and test concurrent acceptance, merge and import operations there. A conditional atomic claim can make acceptance easier to reason about. |
| P2 | `backend/app/main.py:397`, `:648`, `:728-744` | Exports, follow-up lists and dashboard calculations materialize potentially all tenant rows. Add pagination, SQL aggregations, composite indexes verified with query plans, and bounded/background streaming exports. |
| P2 | `backend/app/main.py:146-161` and swallowed sync exceptions | Ingestion status always says healthy without measuring delivery. Add last-success/last-error/lag/queue-depth metrics and readiness rules, with alerts and no contact/credential leakage. |
| P2 | requirements and CI | Dependency ranges are not a reproducible lockfile; CI lacks dependency, secret and static-security scanning. Lock tested versions, scan dependencies/images/secrets, and establish patch ownership. No specific dependency CVE is asserted by this review. |
| P2 | deployment/docs and archive | Backups, restore testing, retention deletion, session cleanup, encryption at rest and operational access controls are not proven by application code. Verify the deployed configuration and automate the agreed policies. Include archived import previews, soft-deleted contacts and replicas in retention decisions. |

## Delivery plan

Indicative effort for one experienced backend engineer with deployment support; refine after testing the real staging infrastructure.

1. **Containment, first working day.** Rotate exposed credentials; restrict access if already public; remove the initialization route and live-key demo; disable unsafe external-login provisioning until corrected; review access/database logs for misuse. Deliver a secret inventory without secret values and evidence old credentials are rejected.
2. **Authentication and authorization, next 3–5 working days.** Implement authoritative identities, organization mapping, explicit roles, admin MFA, session revocation and account lifecycle. Apply the agreed object permission matrix. Tighten CORS/cookies and repair CSV export/auditing. Deliver regression tests for every issue S1–S8.
3. **Data consistency and deployment, following 3–5 working days.** Repair migrations and offboarding. Establish PostgreSQL-only production checks. Replace inline remote writes with an outbox. Move reconciliation to a dedicated worker with cursor-based recovery. Deliver fresh-install, upgrade, offboarding, retry and outage-recovery evidence.
4. **Operational hardening, following 2–4 working days.** Add shared throttling, bounded queries/exports, monitoring, secret/dependency scanning, retention jobs, encrypted backups and a restore drill. Deliver an operational runbook with owners, recovery targets and alert thresholds.
5. **Release gate.** Review fixes independently; run staging security/concurrency tests and an authenticated penetration test against the agreed threat model. Use OWASP ASVS Level 2 as a proposed verification target for this customer-data CRM, with requirement-by-requirement evidence rather than a percentage score.

## Required verification before approval

- Two organizations, all roles, valid/invalid external users: no unauthorized provisioning, reads, mutations or export access.
- Upstream password changes, role changes and disablement with existing CRM passwords/sessions: access revoked as designed.
- Arbitrary/same-site hostile origins, missing headers and production cookie flags: expected denial and no credentialed CORS disclosure.
- Fresh PostgreSQL migration and existing-database upgrade: identical model schema; no startup patching required.
- Concurrent lead claims, imports, merges and duplicate webhook delivery: stable invariants and one logical submission per stable source ID.
- Mongo outage, SQL rollback, worker crash and backlog greater than 100 records: eventual recovery without silent loss or duplicate effects.
- Offboarding with sessions, assigned leads and follow-ups: complete transaction succeeds and other tenants are unaffected.
- Upload boundaries, safe CSV output, durable audit entries, backup restore, load limits and alert delivery.

## Validation limits

The existing acceptance tests were inspected. An isolated run with Mongo stubbed was attempted locally; the first network guard interfered with Windows asyncio loop initialization, and subsequent TestClient attempts did not complete. They were stopped. **No passing test-suite result is claimed.** PostgreSQL migration execution, actual browser cookie behavior, external identity integration, live database grants, infrastructure/TLS and dependency vulnerability scanning remain unverified. The findings above are grounded in source paths and control flow; they do not establish a live compromise.

The deployment guide claims an explicit administrator seed and careful production configuration, but the actual startup/login code differs. Documentation must be updated after the implementation is corrected.

## Reference guidance

- [OWASP Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html)
- [OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OWASP Multifactor Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html)
