# Version 1 API and domain contract

Versioned application endpoints below use prefix `/api/v1` unless explicitly noted.
A success returns `{ "data": ..., "meta": { "request_id": "..." } }`.
Errors use `application/problem+json`: status, code, object, field, actual, limit,
action and params (plus a safe English detail). Ordinary metadata responses never
return secret/hash fields; explicit credential-delivery actions are documented below.
A stable publication URL is returned by the server; clients do not reconstruct it.

## Identity and control

- GET `/session`: `{authenticated, setup_required, username?, csrf_token?, ...session}`.
- POST `/auth/login`: `{username,password}`; owner host-only HttpOnly cookie.
- POST `/auth/refresh`: `{}`; renew an already active owner's idle deadline, bounded
  by its absolute deadline and refresh interval. This never restores an expired session.
- POST `/auth/reauth`: `{password}`; confirm the active owner and refresh recent
  authentication. Wrong credentials are throttled and do not extend the session.
- POST `/auth/logout`: revoke session and its grants, clear owner cookie.
- GET `/settings`: `{defaults,limits,ceilings,operations,storage,thumbnails}`.
  `storage.publication_count` counts catalog records, including retained records
  after file deletion, against `limits.publication_count`. `storage.used_bytes`
  and `storage.quota_bytes` report material use and its effective quota.
- PATCH `/settings`: `{defaults?:{...},limits?:{...}}`; owner session only.
  `limits.publication_count` caps new material records within the published ceiling.
  `defaults.link_previews` is a boolean, enabled by default. It applies immediately
  to every address whose preview mode is `inherit`.
  Lowering the material limit keeps existing records and permits updates to them. The upload,
  expanded site and quota limits are shared across file transports.
- GET `/tokens`: redacted device token list; owner only.
- POST `/tokens`: `{name,scope:"materials:manage"|"materials:read"}`; recent owner
  authentication required, secret returned once.
- DELETE `/tokens/{id}`: owner only; revoke independently.
- GET `/connections`: owner only; `{enabled,connections}` with safe OAuth app/grant
  metadata, scopes and dates. This contains no access/refresh tokens.
- DELETE `/connections/{id}`: owner only; revoke that app's grant family, tokens and
  pending/unexchanged approvals. A future connection requires fresh consent.

Session metadata contains `expires_in`, `expires_at`, `absolute_expires_at`,
`authenticated_at`, `recent_auth`, `idle_timeout_seconds`,
`absolute_timeout_seconds`, `refresh_interval_seconds`,
`recent_auth_max_age_seconds`, `renewed` and `csrf_token`. Session deadline fields
are Unix seconds. The packaged browser renews after trusted visible user activity;
passive polling and background tabs do not renew a session. Renewal preserves the
opaque session credential. Recent-auth failure is `403 reauthentication_required`
with `action:"reauthenticate"`; clients retry the original action only after success.

Browser mutations use exact Origin + `X-CSRF-Token` bound to the owner session.
Bearer device tokens manage/read materials only; never account/settings/tokens.
An OAuth access token has the exact resource audience `<control-origin>/mcp` and is
accepted at MCP only, not as an external `/api/v1` bearer. MCP internally reuses the
material API through a private ASGI principal callback, revalidating its grant on
every operation. A network header cannot select this principal.
Setup/password reset is an explicit local operator command, not a public default password.

- GET `/capabilities`: safe defaults/effective limits for material-token clients; no account or token data.

### Browser entry pages

These paths are on the control origin and do not use the `/api/v1` prefix:

- `/`: the signed-out interface is a neutral material-link page, without a catalogue
  or registration. An authenticated owner sees the materials library.
- `/login`: the owner sign-in interface. Successful sign-in normalizes the URL to `/`;
  signing out shows the neutral `/`, and an expired owner session shows `/login`.
- `/upload/{job}`: the same shell for an owner-reviewed browser upload request.
  A job identifier is a nonsecret reference, not permission to read or upload.

`/login?return_to=...` accepts a saved or pinned publication address, a legacy
`/viewer/{id}` owner continuation, `/upload/{32-lowercase-hex-job-id}`, or
`/oauth/consent?request={43-character-opaque-request}`. Publication paths accept
only allowlisted preview/revision parameters. External URLs and arbitrary local
routes are discarded.

These paths serve the same packaged application shell and content security policy.
Owner chrome is hidden until the session is known. EN/RU/ES/FR language and
system/light/dark theme preferences use the existing host-only cookies; they do not
grant authorization. Protected API endpoints still require their normal credentials.
On revision hosts, `/login` remains an author content path; on a configured legacy
host it follows the legacy publication redirect rather than exposing the owner shell.

### Shared links and previews

The authoritative share address is `<base-url>/p/{link_name}`, returned as
`stable_url`. The name starts as the publication ID and can change independently.
The cabinet and owner menu use **Edit link** to replace it. A previous name kept
by the owner still opens the current revision, through the same gate, without
redirecting to the new name. Disabled names return 404 and remain reserved to
their original material. The `/p/` prefix defines a routing namespace; isolated
revision hosts enforce the origin boundary independently.
The separate version-copy action uses `<base-url>/p/{id}~{sequence}`. This pinned
link requires explicit owner sharing of that revision for guest access, including
the current revision. It follows the material's current access policy and file
retention; it never selects a replacement revision. `~` is excluded from material
identifiers, so the version suffix cannot collide with a custom name or site path.
A direct revision-host document navigation without a valid reader grant returns
to its pinned control gate, retaining its path/query and guest realm. Resource fetches, Range and conditional requests do not acquire
this navigation fallback or bypass access checks.

With previews enabled for the requested address, guest document GETs expose server-rendered Open Graph and Twitter Card HTML at
the stable entry before automatically continuing to the isolated document through
an HTML refresh. A visible continuation link works when automatic navigation is
disabled. Explicit resource/Range/conditional requests retain HTTP redirect
semantics. GET/HEAD from recognized link-preview crawlers returns the same safe
metadata before owner/unlock/handoff handling, without allocating a reader intent.
Active, present `link`
materials expose their saved title and optional summary. Password and private
records use the same generic card. Unavailable link-access records show generic
status. The brand image, optional site cover and absolute URLs use the configured
instance origin. No author HTML is
executed, no reader grant is allocated and query values are not copied into cards.
Unknown crawlers can read the initial metadata without supporting the browser
handoff; an ordinary guest continuation allocates an intent but does not count an
opening until that handoff is accepted. Password and private sign-in gates return
generic branded metadata; their GET/HEAD status is 200 without granting content
access. User-Agent chooses presentation only; it grants no authority. Responses use
`no-store`; external services can retain an earlier preview independently.
If previews are off, recognized crawlers receive 204 without a continuation or
reader state. Browser responses omit Open Graph and Twitter Card tags and
retain normal navigation and access checks. A public custom cover requires the
exact ordinary name or pinned sequence in the image URL. The cover route
rechecks that address's preview, access and retention policy on every request.
Asset decisions and platform rules are documented in [link previews](../LINK_PREVIEWS.md).

Automatic publication names use cryptographic lowercase letters/digits. Owner
settings `defaults.publication_key_min_characters` and
`defaults.publication_key_max_characters` default to 8 and 16; both must be integers
inside `operations.publication_key_bounds` (4–32), with minimum ≤ maximum.
Each collision increases the length by one until the maximum; fresh attempts then
stay at that length within `operations.publication_key_max_attempts`. Exhaustion
returns `409 publication_key_exhausted`, without replacing an existing material.
Allocation checks committed publications, pending uploads and deletion tombstones
inside one write transaction. These defaults affect new automatic names only.
Custom names retain their separate 2–64 character contract below.

All stable openings, including missing IDs, closed materials and crawler requests,
share the per-client-IP budget `defaults.publication_open_attempts_per_minute`.
Password submissions also consume the per-publication/IP budget
`defaults.publication_password_attempts` per `defaults.publication_password_window_minutes`.
The owner settings endpoint exposes each field’s validated range in
`operations.publication_rate_bounds`. Exhaustion returns `429 rate_limited` with
a `Retry-After` delay in seconds to the end of the existing window. Short names
are identifiers, not passwords; private/password access is independent.

### Native form continuation

Successful publication-password and OAuth-consent POSTs return 303 to the control
origin's `/__mp/continue?ticket={opaque-ticket}`. They do not redirect the submitted
form directly to author content or an OAuth callback. The control-only GET requires
the matching active owner, normal-viewer or guest-probe cookie, revalidates the
underlying authority and atomically consumes the short-lived ticket. HEAD, replay,
expired tickets, other cookie realms and revision hosts cannot consume it.

The 200 response is a server-owned HTML continuation with automatic meta navigation
and a localized, escaped `Continue` fallback link to the same validated destination.
It has no forms, does not cache, sends no referrer and creates no new grant.
OAuth continuation has no scripts. Publication continuation uses one constant,
CSP-hashed script to copy the browser's fragment with the URL hash setter before
navigation; the fragment is never sent to the server or interpolated into HTML.
Its no-JavaScript fallback opens the material without the client-only fragment.
Publication continuation supports the trusted control-origin preview frame;
OAuth continuation cannot be framed. The subsequent nonce handoff or OAuth code
exchange retains its independent authorization and one-use checks. Ticket lifetime
and admission use the existing handoff operation budget.

## Materials

- GET `/publications?q=&kind=&status=&states=&offset=0&limit=50`: `{items,total,offset,limit}`.
- GET `/publications/{id}`: publication, manifest and storage/lifecycle metadata.
- PATCH `/publications/{id}`: sparse metadata and policy patch (see below).
- POST `/publications/{id}/pause`: `{paused:true|false}`.
- POST `/publications/{id}/expire`: close guest access now; start saved retention.
- POST `/publications/{id}/extend`: `{seconds:N}` from max(now,current expiry);
  manual pause is preserved; purging/purged returns an explicit conflict.
- POST `/publications/{id}/password-copy`: no request body; `{password}` for the
  current owner session with recent authentication. Exact Origin and session CSRF
  are required. Device tokens and MCP principals cannot use this endpoint.
- DELETE `/publications/{id}`: `{id,deletion_state}`; close access immediately,
  report completion only after actual cleanup. The worker is idempotent.
- GET `/publications/{id}/download`: authorized unchanged original or manifest ZIP.
- GET `/publications/{id}/thumbnail`: authorized bounded raster preview or 404.
- GET `/publications/{id}/open`: `{url}` pointing to the stable controlled opening.

Publication keys: id,link_name,title,summary,kind (`file|site`),filename,entry_path,content_type,
size_bytes,created_at,updated_at,expires_at,access (`private|link|password`),password_hint,
has_password,password_copy_available,paused,content_state (`present|purge_pending|purging|purged`),
library_status (`active|paused|expired|deleted`),
purge_mode,purge_after_seconds,metadata_policy,metadata_after_purge_seconds,
site_routing,external_resources,preview,indexing,guest_counts,current_revision,
access_version,view_count,purge_due_at,purged_at,purge_reason,missing_on_import_at,
stable_url. Detail adds files and thumbnail state.

### Publication link management

- GET `/publications/{id}/link` returns
  `{id,link_name,stable_url,preview_mode,preview_default,previous_links}`.
  Each retained address has `{name,url,created_at,preview_mode}`. These routes
  require an owner session and mutations require browser CSRF protection.
- PATCH `/publications/{id}/link` takes `{name,expected_name,keep_previous,preview_mode?}`
  and returns the same snapshot. `expected_name` prevents a stale tab from saving
  against a different primary address. Pass `false` for `keep_previous` to
  disable the replaced name. Older retained names stay active until individually
  disabled. Owners can promote or reclaim a name belonging to the same material.
  Mode is `inherit`, `on` or `off`. Omission preserves the destination mode
  for an existing name and copies the source mode when assigning a new name.
- PATCH `/publications/{id}/link/{name}` sets a retained address's preview mode
  with `{preview_mode}` and returns the same snapshot.
- DELETE `/publications/{id}/link/{name}` disables a retained address and returns
  the remaining snapshot. The primary name is changed using PATCH.

New names use 2–64 lowercase ASCII letters, digits or hyphens, starting with a
letter or digit. The namespace includes primary, retained, disabled, imported and
pending names. Changing the link leaves revision URLs, metadata and access policy
intact. Previously established reader grants follow their normal bounded lifetime.

`purge_mode` is the configured future cleanup policy, not the cause of past removal.
`purge_reason` is null for present content, `retention` for a recorded scheduled
cleanup, `owner_delete` for an explicit deletion request, or `unknown` when the
cause was not recorded. The reason is recorded when cleanup is claimed and cleared
when pending cleanup is cancelled or content is replaced. Existing records are not
backfilled from current policy. `missing_on_import_at` records when an importer
observed missing source files; it does not establish when or why they disappeared.
New such imports leave `purged_at` null. Retained purged cards allow re-upload of
new bytes with explicit expiry, not recovery of deleted bytes.

Changing record retention on an already-purged card reschedules its metadata
deadline: `keep` cancels deletion, `with_content` selects the next cleanup, and
`after_purge` counts from recorded file removal or the import observation. When
neither timestamp exists, an explicit policy change schedules from that change.
Repeating identical settings preserves that unknown-history deadline; unrelated
patches do not reschedule it. Successful re-upload clears the old cleanup schedule.

The domain's internal password_hash and password_sealed are never serialized into
ordinary metadata responses. `password_copy_available` indicates a saved copy for
password access; it does not return a credential. Copy authorization and the current
publication read share one transaction. Manual deletion blocks copying immediately;
pause, expiry and automatic file cleanup preserve a retained record's password.
Hash-only records return `409 password_copy_unavailable` until the owner explicitly
sets a publication password again. Missing or unreadable key/ciphertext returns a
safe error without changing the password. Before writing to the clipboard or showing a manual-copy field, the browser
rechecks the initiating owner session and material dialog. Navigation, closure and
logout clear the manual field. A clipboard write already started cannot be retracted.

`states` is an optional comma-separated selection of library states. Omission keeps
all records for API clients; an explicitly empty selection returns no records.
Unknown values are rejected. Filtering and the Unicode-insensitive `q` search over
title, summary and filename precede totals and pagination. The owner library sends
`active,paused` by default and offers checkboxes to include expired or deleted files.
`library_status` prioritizes pending manual deletion or missing/deleting content,
then expiry, then pause. The older single-value `status` filter retains its existing
precedence and accepts `purged` for removed content. Combining filters intersects
them. Deleted-file records remain searchable only while their metadata is retained;
permanently removed records and deletion-ledger entries are not library results.

`summary` is optional: create omission → empty; patch omission → preserve; empty
string → clear. Limits are discovered from server settings, not client constants.
`password_hint` is public guest-facing metadata, never a password or secret. Its
omission preserves it on update; an empty string clears it. Changing only the
password preserves the hint; explicitly removing password protection clears it.
Metadata policy is `keep|with_content|after_purge`; purge mode is `immediate|after|never`.
Expiry is `{mode:"default"|"never"|"at",value?:RFC3339}`. Patch omission preserves
expiry. Private→external requires an explicit expiry object. Content updates preserve
all omitted metadata, including password, expiry, counters and retention.

### Site covers

To set a library cover, include a single-frame raster at the root of the built
site directory or site ZIP. Use the exact lowercase name `publish-cover.png`,
`publish-cover.jpg` or `publish-cover.webp`. The first present name in that order
is used. The filename is relative to the bundle root, including when the HTML entry
is in a subdirectory. Cabinet, CLI and API uploads use the same convention. The
source remains an ordinary site asset and is available to authorized site readers
and in ZIP downloads.

The server uses the committed manifest and its bounded raster worker to produce a
JPEG for the current revision. While processing, or when the source is absent,
invalid, animated, over the raster limits, or the worker cannot run, the library
uses its site icon. A missing thumbnail returns 404. The library thumbnail endpoint
requires material read authorization. The [link preview](../LINK_PREVIEWS.md) can use
the site cover with public link access and the current or explicitly shared revision.

The worker runs as part of committing a new revision, including replacement and
restoration as a new revision. Existing revisions are left as stored on startup
and after metadata edits. To add an image to an already published site, include it
in the bundle and publish a new revision. A replacement without the cover file
uses the site icon.

## Streaming publication

1. POST `/uploads`: `{kind,filename,size_bytes,title?,summary?,change_summary?,access?,password?,password_hint?,key?,
   expiry?,purge_mode?,purge_after_seconds?,metadata_policy?,
   metadata_after_purge_seconds?,entry_path?,site_routing?,external_resources?,
   preview?,indexing?,guest_counts?,expanded_size_bytes?,
   publication_id?,expected_revision?,supports_link_names?}`.
   Returns `{id,state,max_bytes,expires_at}`; reserves peak storage. Update requires
   publication_id and an explicitly present expected_revision. The caller owns the upload session.
   `change_summary` is an optional revision note, distinct from searchable material
   `summary`; it is accepted only on upload plans, never a metadata patch.
   Optional `key` chooses a new publication's ID and initial link name. It is
   rejected for replacement. Capabilities advertises `link_names: true`. Clients
   that accept a `stable_url` built from `link_name` send `supports_link_names: true`
   on upload plans to those instances. Older clients receive
   `publication_link_upgrade_required` before replacing a renamed material. A
   rename during a replacement produces `publication_link_changed`; refresh the
   material and plan a new upload.
2. PUT `/uploads/{id}/content`: raw bounded request body, no base64/JSON envelope.
   Returns `{id,state,received_bytes}`. Authentication/admission precede body parsing.
   Duplicate/concurrent receivers are rejected; disconnect aborts temporary data.
3. Optional POST `/uploads/{id}/inspect`: validate and prepare privately; returns
   `{id,state:"prepared",expires_at,files,dependency_report}`. Repeated inspection
   reuses prepared files. The browser shows this report before final confirmation.
   The bounded report lists external origins and missing local asset paths; it never
   fetches a URL or executes HTML/JavaScript. It excludes URL credentials, queries
   and fragments. `complete` is always false: static analysis cannot prove the
   absence of dynamic requests. Scanner bounds are owned by `dependencies.py`.
4. POST `/uploads/{id}/commit`: prepare if needed, then CAS the current pointer;
   returns publication and its bounded dependency report. Prepared review sessions
   keep their reservation and have a durable expiry; commit does not extract twice.
5. DELETE `/uploads/{id}`: abort idempotently and release reservation after an active
   receiver/preparer stops using bytes.

GET `/uploads/{id}` returns only `{id,state,expires_at}` for the matching authenticated
upload subject. Its deadline is the earlier of the phase and hard deadline. A committed
record also returns `publication_id`, `link_name` and `stable_url`: these identify the
operation and its material, even if the material has since expired or been deleted. Its `expires_at`
is the upload operation deadline, not the publication's access expiry. Use publication
detail to check current availability. A missing/expired upload record is not proof
that a commit failed. CLI `upload-status ID` and read-scoped MCP `get_upload(id)` expose
this status; browser-request jobs use the separate `get_file_upload` tool.

The shared client's `commit_upload` makes one commit attempt. A structured 4xx refusal
is preserved and followed by best-effort abort. Transport failure, 5xx, unstructured
error or an invalid commit response becomes `unknown_outcome` with `actual.upload_id`
and a status-inspection action; it neither deletes the upload nor republishes/retries.
Both local publication and optional host-file transfer use this boundary. The browser
checks status once after an unknown commit result, confirms `committed`, or permits an
explicit publish of the same ID when still `prepared`; other outcomes require status
reconciliation without creating another material.

For a purged existing publication, the concurrency token is explicit
`expected_revision:null`, with a required explicit expiry choice. An omitted token
is rejected. The first successful restoration changes the pointer, so a competing
restoration fails with a conflict. Omitted site entry paths preserve the existing
entry on a same-kind site update.

Archive site upload is explicit. Plain file ZIP is never auto-extracted. Site ZIPs
use a single disk and a bounded central directory. Standard ZIP64 and archives with
prepended data are accepted. ZIP64 extensible end records are outside this profile.
The entry and routing are validated against the manifest. Absent assets remain 404
with SPA page fallback. HTTP payloads use publication paths.

### Client preparation

The portable client in [`preflight.py`](../../src/minder_publish/client/preflight.py)
completes a private temporary snapshot before reporting local preflight. Single files
and explicit site ZIPs are copied in bounded chunks; a site directory is packaged
into a bounded temporary ZIP. Sensitive-name and portable-path checks apply to the
selected source names and included manifest, not the temporary snapshot's basename.
Site ZIP validation, dependency scanning and upload use the same completed snapshot.
Changing an original file, including a same-size rewrite with preserved timestamps
or a rewrite while uploading, cannot change the bytes sent by that operation.

This costs an additional local copy/archive pass and temporary disk up to the upload
payload limit, in addition to the original source. Preparation checks the discovered
file/payload/site limits and deadline; a growing source is bounded before writing
beyond its admitted size. A snapshot belongs to one preparation context: a standalone
CLI `preflight` finishes and removes it; a later `publish` prepares the then-current
source again. The upload iterator is explicitly closed before abort cleanup, including
partial transport failures, and the temporary directory is released on context exit.

Preflight returns a versioned `manifest` and its SHA-256 `manifest_sha256`. The
manifest covers kind, normalized filename/entry, sorted paths, sizes, byte hashes,
excluded names and the sensitive-file override. ZIP compression and timestamps do
not affect it. CLI `publish` and `update --expect-manifest HASH` compare a freshly
prepared snapshot with that reviewed digest before creating an upload. A mismatch
returns `manifest_mismatch` without uploading. A committed CLI result includes the
used digest. This is a content guard; it does not freeze access policy, server
defaults or expiry. Separate preflight and publication still read the source twice.

Dependency reports retain `complete:false`: JavaScript and runtime behavior are
outside the scanner's scope. `static_scan_complete` indicates whether the supported
HTML/CSS scan finished; `missing_assets` and `external_origins` are findings, not
completion flags. New reports include `summary` and `limitations` to explain that
distinction. Stored older reports may omit those strings. Empty findings do not
prove offline operation or absence of dynamic dependencies.

CLI `doctor --brief --json` returns connection readiness, selected server and a
small subset of discovered capabilities/defaults; `doctor --json` preserves the
full contract. Profile reads and writes wait up to two seconds for the same
configuration lock, preserving a coherent URL/token snapshot. Network requests
do not hold that lock. Contention beyond the limit returns `configuration_busy`.

## Remote MCP authorization

Remote MCP and its authorization server are enabled together by the operator;
they are disabled by default. These control-origin endpoints use OAuth responses,
not the versioned API success/problem envelope:

Both exact control paths `/mcp` and `/mcp/` dispatch directly to MCP without a redirect
or publication-alias handling. The resource audience remains `<control-origin>/mcp`.
On revision hosts, GET/HEAD `mcp` files and nested paths use ordinary authorized author
content serving; POST/OPTIONS cannot enter MCP. Legacy and unrecognized hosts do not
serve the MCP transport.

| Endpoint | Contract |
|---|---|
| GET `/.well-known/oauth-authorization-server` | Issuer, authorization/token/registration/revocation endpoints and supported protocol capabilities |
| GET `/.well-known/oauth-protected-resource/mcp` | Exact resource `<control-origin>/mcp`, issuer and material scopes; root `/.well-known/oauth-protected-resource` is an alias |
| POST `/oauth/register` | Bounded dynamic client registration; HTTPS redirect URIs only; exact registered callback matching; no remote metadata fetch |
| GET `/oauth/authorize` | Authorization code flow; mandatory S256 PKCE, exact resource and optional `materials:read` / `materials:manage` scope; default read |
| GET/POST `/oauth/consent` | Owner-bound pending request; explicit approval/denial, exact Origin and CSRF; approval requires recent owner authentication in the grant transaction |
| POST `/oauth/token` | URL-encoded authorization-code or refresh exchange; exact resource, client binding and verifier checks |
| POST `/oauth/revoke` | Client-authenticated token/family revocation; unknown tokens do not reveal existence |

Public clients use `token_endpoint_auth_method:"none"`; confidential clients use
`client_secret_post` or `client_secret_basic`. Registration returns a confidential
client's secret once. Callback strings must be canonical HTTPS URLs without userinfo,
fragments or reserved OAuth response query names. Existing nonprotocol query fields,
including blank/repeated values, are preserved. Success/error callbacks identify the
issuer with `iss`. CORS is available for token/registration/revocation, not owner
consent; revision and legacy hosts expose none of these endpoints, including OPTIONS.
Authorized revision GET/HEAD paths with the same names remain author content and
use the ordinary reader/policy check; they never return the instance's OAuth metadata.

Authorization codes are atomic one-use credentials. Access tokens are short opaque
credentials; refresh tokens rotate within a stable app grant family. Code replay or
spent-refresh replay revokes that family. Narrowed scopes cannot be widened by refresh.
Successful narrowing also updates the family's current scopes atomically: connections
show them, and removing manage permission closes its pending browser-upload authority.
Manage includes read; neither grants instance/account administration. Storage limits,
TTLs and registration caps are owned by
[`OAuthOptions`](../../src/minder_publish/server/oauth/store.py), not browser settings.

MCP HTTP denials advertise `WWW-Authenticate` protected-resource discovery and
recommend `materials:manage` for the publishing workflow. The client requests that
scope and the owner explicitly consents; this does not expand an existing grant.
Clients may explicitly request read-only access. A read-only connection must
reauthorize with management scope before publishing if its host does not support
per-tool scope escalation. Remote
tool declarations include scoped `_meta.securitySchemes`; tool-time authentication
or insufficient-scope errors include `_meta["mcp/www_authenticate"]`. These follow
the [OpenAI authentication contract](https://developers.openai.com/plugins/build/auth)
using the maintained Python SDK's public tool metadata/result interfaces. Other
authorization failures do not trigger an unrelated account-linking loop. Protocol
tests do not establish that any particular cloud account is connected.
The public SDK `call_tool` boundary is specialized to replace argument-validation
errors with `invalid_tool_arguments` without rejected input values. Advertised schemas
remain available; signed file URLs/password-like rejected values are not echoed in
model-visible validation errors or SDK error logs. Deliberate domain errors retain
their safe structured details.

## Browser file-upload requests

The remote tools `create_file_upload`, `get_file_upload` and `cancel_file_upload`
support a chat that cannot transfer actual file bytes. They wrap these API endpoints:

- POST `/upload-jobs`: creator must be a live manage-scoped device/OAuth principal.
  Intent is `{kind:"file"|"site",title?,summary?,access?,expiry?,entry_path?}`.
  No password, server path, arbitrary download URL or replacement identity is accepted.
  Returns `{id,state,intent,created_at,expires_at}`. The shared client adds
  authoritative `upload_url:<control-origin>/upload/{id}`.
- GET `/upload-jobs/{id}`: creator or active owner only. A different app/device gets
  404. State is `pending|uploading|published|expired|revoked|cancelled|failed`.
  `publication_id` and `stable_url` are returned only after confirmed publication.
  Only the claiming owner session sees `upload_id`; owner responses also include
  `claimed_by_current_session`.
- POST `/upload-jobs/{id}/start`: owner session + CSRF only. The owner chooses a real
  local file and a normal new-upload plan; passwords are entered privately here.
  Kind must match the intent. Atomically claims the job once and returns the normal
  upload reservation with `job_id`; no bytes are reserved merely by creating a job.
- DELETE `/upload-jobs/{id}`: creator or owner; cancels pending work and aborts its
  temporary upload. A published job conflicts and never deletes its publication.

The MCP `create_file_upload` tool accepts `ttl` (`default`, `never`, a duration such
as `7d`, or RFC3339), matching the content-publication tools. It converts this to
the API's `expiry` object before creating the intent. Supplying both `ttl` and
`expiry` fails before creating a job; omitting both preserves the instance default.
The job's own deadline is independent of the requested material expiry.

The browser then uses raw upload, inspect, review and commit. The owner can adjust
intent defaults before approval; a job never publishes without that explicit action.
Commit revalidates the claiming session, live creator/grant, job deadline and binding
inside the same Core transaction that promotes the publication. Logout, creator
revocation, expiry or cancellation closes pending authority. Refreshing OAuth tokens
does not change the creator's stable family identity. Job caps/TTL and cleanup live
in [`upload_jobs.py`](../../src/minder_publish/server/upload_jobs.py).

### Optional host-provided file input

`publish_host_file` is advertised on remote MCP only when the operator configures a
nonempty exact DNS allowlist through `PUBLISH_MCP_FILE_HOSTS`. Its scoped tool metadata
includes `_meta["openai/fileParams"]:["file"]`. The structured file argument is
`{download_url,file_id,file_name?,mime_type?}`; extra fields are rejected. The tool also
accepts `kind`, `title`, `summary`, `access`, `ttl` or `expiry`, and `entry_path`.
The TTL conversion and conflicting-expiry guard are the same as other publication
tools. Passwords remain
in the browser-upload path. The optional filename is validated and MIME is determined
by the server's ordinary manifest handling, not trusted from `mime_type`.

[`HostFiles`](../../src/minder_publish/mcp/host_files.py) accepts HTTPS port 443 only,
without userinfo/fragments, validates every DNS answer as globally routable, and pins
the selected address while retaining the allowlisted hostname for TLS verification/SNI.
It uses no ambient proxy, redirect following or external authorization headers.
The source must return 200 with one bounded Content-Length and no content/transfer
encoding. Bytes stream directly into an authenticated, quota-reserved Core upload;
there is no auxiliary local download spool. The adapter inspects prepared files for
sensitive names before commit. Upstream resolution/read/transfer deadlines are bounded;
ordinary Core preparation and lease limits still apply.

Transfer/preparation failure attempts abort; unavailable/revoked authority can leave
cleanup to the server's upload lease. An ambiguous commit result returns
`unknown_outcome` with `actual.upload_id` and an inspection action; it never deletes
or retries a possibly committed publication. A file reference is not proof of its
origin or safety. Actual provider compatibility must be verified before allowlisting
its exact file host; an unavailable/unsupported host uses the browser request instead.

## Client and skill distribution

GET `/distribution` is authenticated and returns release version, instance/MCP URLs,
available assets with SHA-256/size, and credential-free runtime installation prompts.
GET `/downloads/{name}` is a public control-origin attachment download of one known
packaged asset: skill ZIP, plugin ZIP, and, when the operator supplies a matching
client wheel, that wheel plus `install-client.py`. Unknown names return 404; no local
path is accepted. Content hosts retain their ordinary author-path routing.

The installer verifies the advertised wheel digest, creates a per-user virtual
environment and never changes global PATH or handles credentials. Dependency resolution
uses pip and the wheel's requirements. The owner configures a device token through a
private local prompt. Downloading a skill/plugin ZIP does not install it into a cloud
account or complete OAuth consent. Local MCP filesystem tools read only their local
runtime's files; remote MCP does not expose those tools.

## Browser content boundary

`/p/{link_name}` and legacy `/{link_name}[/{path}]` are stable control entries. They authenticate
owner/guest as required, select current revision and cross into
`r-{opaque_revision}.{content_domain}` using a valid reader capability or a one-time
browser-nonce handoff. Content
hosts expose only serving/handshake paths, never management or account endpoints.
The validated hostname selects content before any control router or static mount.
Existing author paths such as `/static/`, `/api/`, `/oauth/`, `/mcp`, `/login` and
`/healthz` remain publication files on revision hosts; they cannot invoke control
handlers. Only the root `/__mp` protocol namespace is reserved. Revision requests
other than GET/HEAD fail before body consumption. A control owner cookie or API
Bearer credential alone never replaces the revision's reader grant.
Ordinary readers cannot mint a new grant for an unshared retired revision. Owner
history openings can mint a retained-revision grant after owner/session and current
policy revalidation. Explicitly shared pinned links issue separate grants bound to
the revision's sharing epoch and current material access version. Disabling sharing
invalidates these grants, including pending handoffs; enabling sharing again does
not revive them. Existing ordinary grants have bounded drain; policy revocation wins.
GET/HEAD/Range/conditional requests all authorize before serving.

The cold browser chain is `/p/{link_name}` → revision `/__mp/start` → control
`/__mp/authorize` → revision `/__mp/accept` → content. For a normal GET opening,
`/__mp/start` can reuse its existing reader cookie and redirect directly to content.
The intent and reader must match publication, revision, subject kind/ID, access
version, owner-history/shared-version modes, sharing epoch and browser realm. Both
must pass current new-opening authorization inside the same write transaction.
That transaction consumes the intent once, renews the existing reader's ordinary
deadline and records an eligible opening. It returns the new opening's path/query
and preference cookies; it does not clear a separate pending bridge cookie.
Mismatch, missing or expired authority leaves the intent unconsumed and uses the
full cold chain. HEAD, prefetch, original password POST and guest-probe openings
never use this shortcut. A consumed intent cannot resume or mint another grant.

`?guest=1` on a stable control entry checks that material's guest policy without
owner authorization or prior normal reader unlocks. It uses a fresh probe viewer
and separate `g-{revision}` host/read-cookie realm. Normal owner/reader cookies
remain intact. This is a material-access test, not an incognito browser profile:
author-controlled links to other sites are ordinary browser navigation. The reserved
`guest` and `preview` parameters are not forwarded to author content.

## Owner workspace, history and opening statistics

`GET /p/{link_name}` renders authenticated owner tools around an isolated revision
document on ordinary owner root navigation. `/p/{id}~{sequence}` retains the exact
selected revision. Author paths and queries retain their normal navigation semantics.
The allowlisted preference cookie `mp_owner_view=clean` disables that automatic
entry; `?preview=tools` explicitly opens the workspace. `/viewer/{id}` redirects to
that canonical owner entry, preserving a selected revision through login.
`?preview=1` opens the original
without the rail, preserving owner authority. `?guest=1` instead checks guest access and is excluded from statistics. The owner
workspace shell is not counted; its preview and owner clean or historical views count
accepted owner openings when enabled.

These API routes require an owner browser session, not a material device token:

- `GET /publications/{id}/revisions` returns `{items,total,available,catalog_limit,preview_default}`. Each item
  includes `revision_id`, `sequence`, `created_at`, `size_bytes`, `change_summary`,
  `current`, `available`, `retain_until`, `baseline`, `shared`, `version_url`, `preview_mode`,
  `kind`, `filename` and `content_type`. The last three describe that revision's
  files and entry manifest, not the current material; they are null when unavailable.
  The version URL identifies the revision even when sharing is disabled; returning
  it to the owner does not authorize guest access.
- `GET /publications/{id}/revisions/{revision}/open` starts an owner-authorized
  browser handoff for retained files. Selecting a version does not publish it.
- `GET /publications/{id}/revisions/{revision}/download` downloads a retained
  original file or site ZIP under owner authorization and a bounded read lease.
- `POST /publications/{id}/revisions/{revision}/restore` accepts only
  `{expected_revision}`. Recent owner authentication and CSRF are required. It
  publishes a new immutable revision with the current policy unchanged; stale
  replacement conflicts rather than overwriting concurrent work.
- `POST /publications/{id}/revisions/{revision}/sharing` accepts only
  `{enabled:boolean}` and returns `{revision_id,sequence,shared,version_url,preview_mode,preview_default}`.
  Recent owner authentication and CSRF are required. The revision must still be
  retained. Sharing does not change current access, password, expiry or retention
  settings; private material remains private. Repeating the same setting is
  idempotent. Disabling then reenabling sharing rotates the grant epoch.
- `PATCH /publications/{id}/revisions/{revision}/sharing` sets `{preview_mode}`
  for a shared, retained version and returns the same snapshot. This owner
  session change requires CSRF and preserves the existing sharing epoch.
  An unshared revision returns 409.
- `GET /publications/{id}/statistics?days=7|30|90` returns `total`, `legacy_total`,
  `started_at`, `last_opened`, `period_total`, `daily:[{day,opens}]`, `enabled`,
  `incomplete` and `timezone:"UTC"` for guests, plus `owner` with `total`,
  `started_at`, `last_opened`, `period_total`, `daily`, `enabled`, `incomplete`
  and `collection_interrupted`. Owner daily `opens` can be null when the day is
  outside the measured or retained interval or a gap may exist. A collection
  interruption flags a possible gap; totals contain measured openings only.
- `DELETE /publications/{id}/statistics` clears guest, owner and legacy counts and starts a new owner measurement interval;
  it requires recent owner authentication and CSRF. Independent `guest_counts` and
  `owner_counts` metadata fields pause/resume collection without deleting earlier
  counts. `owner_counts` defaults to true.

Owner list/detail responses additionally expose `guest_open_count` and `owner_open_count`; material-token
responses retain their existing fields. `view_count` remains the compatibility
counter, including pre-migration counts; it must not be presented as the new clean
total. History retention and restoration semantics are in
[revision history](../REVISION_HISTORY.md). Counting semantics and retention are in
[opening statistics](../STATISTICS.md).

## Domain core interface (Python)

`Core(config)` owns domain/storage, no FastAPI imports. Expose:

- `initialize()`; `policy_settings()` → fresh policies, limits and effective ceilings
  without filesystem accounting; `settings()` adds a current storage snapshot;
  `update_settings(patch)`;
- `list_publications(query,kind,status,offset,limit,*,states=None)` → paginated dict;
- `get_publication(id,manifest=False)` → internal dict including password_hash;
- `create_upload(plan,subject,*,requested_id=None,on_created=None)` → upload dict;
- `await receive_upload(id,chunks,subject)` → upload dict;
- `inspect_upload(id,subject)` → prepared manifest and dependency report;
- `upload_status(id,subject)` → minimal historical operation status;
- `commit_upload(id,subject,*,authorize=None)` → publication dict; `abort_upload(id,subject)`;
- `patch_publication(id,patch)`, `pause(id,paused)`, `expire(id)`, `extend(id,seconds)`;
- `delete_publication(id)` → deletion dict; `sweep()` → operation counts;
- `get_revision(revision_id)` → revision/manifest; `resolve_file(revision_id,path)`;
- `acquire_lease(revision_id,kind="read")` → lease id; `renew_lease(id)`; `release_lease(id)`;
- `storage_snapshot()` → configured/used/reserved/maximum/free space;
- `reserve_external(bytes,subject,kind="multipart")` / `release_external(id,subject)`
  → bounded persisted reservations for auxiliary temporary allocations;
- `repository.connect()` context manager for the separate authentication repository.

`on_created(connection,upload_id)` and `authorize(connection,upload_id)` are trusted
internal callbacks inside Core's write transaction. Upload jobs use them to claim a
job atomically and prevent a revoked creator/session from racing publication. They
are not HTTP payload fields. The separate `requested_id` argument serves the legacy
adapter; canonical HTTP callers choose a validated `key` on a new plan.

Domain errors are `PublishError(code,status=400,detail=...,field=None,actual=None,
limit=None,action=None)` in `errors.py`. Tests use an actual temporary SQLite/volume.
Server `Config` owns data_dir/base_url/content_domain/secret/storage_budget_bytes;
properties db_path, revisions_dir, staging_dir, defaults/limits/ceilings/operations/
thumbnails, cookie_secure, control_host, content_origin(revision_id).
`Core` owns publication/revision/upload/settings schema; authentication, OAuth and
upload jobs own separate version tables in the same DB, never mutation of the legacy
database. Each module refuses unknown schema versions.
