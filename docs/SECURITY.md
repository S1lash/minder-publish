# Security model and reporting

Minder Publish is a single-owner, self-hosted publication service. Its security
boundary is between the owner/control origin, mutually isolated publication
revisions, device clients, and untrusted visitors. The operator controls the host,
TLS, persistent volume, credentials, backups and software updates.

Security fixes target the current release line. Include the exact installed version
in a report. Use the repository's private vulnerability-reporting channel if it is
enabled. Otherwise ask the maintainer for a private channel without posting exploit
payloads, credentials, private publication links or database dumps in a public issue.
Do not assume that an ordinary public issue is a private report.

## Credentials and access

Owner sign-in and publication passwords are separate. Publication passwords are
server-side access gates, **not encryption**. Files are stored in plaintext on the
server. A host administrator, compromised process with volume access, or unprotected
backup can read them. Use protected disks and encrypted backups when that matters;
Minder Publish does not provide end-to-end encryption or protection from its operator.

The owner account is created by an explicit local command. There is no default
password. Owner passwords use Argon2; opaque session/device credentials are stored
as hashes. Production browser cookies are host-only, HttpOnly and Secure. Device
tokens grant material read or management powers; they cannot administer accounts,
instance settings or other tokens. Remote MCP is disabled by default and must not
expose local paths, directory reads or arbitrary server-side file operations.

Device token issuance verifies the owner's recent authentication within the
transaction that stores the credential. Use one scoped device token per integration.
Before upgrading an instance that sets `PUBLISH_LEGACY_API_KEY`, switch those clients
to scoped device tokens and remove the setting.

Publication passwords retain their Argon2 verification hash and an AES-GCM encrypted
copy for the owner's explicit **Copy password** action. The separate random 32-byte
`publication-password.key` lives in the data root with mode `0600`; it is independent
of `server.secret`. The ciphertext is bound to the publication ID and current hash.
Ordinary metadata, CLI and MCP responses never expose this copy. Delivery requires
the owner session, exact Origin, CSRF and recent password authentication checked in
the same transaction as the current publication. Hash-only imported records cannot
be recovered: the owner must explicitly set a password again to enable copying.
Removing password protection clears its saved copy. Manual deletion immediately
blocks delivery; expiry and automatic file cleanup can retain it with the record.

The operator and a complete backup can recover these publication passwords. Keep the
key and database protected together; this feature does not protect passwords against
an operator or a process with data-volume access. A missing, corrupt or mismatched
key fails closed and must be restored with its matching data. Owner-password reset
revokes credentials without rotating this separate publication-password key.

Owner sessions have independent idle and absolute deadlines. A visible trusted
browser action can request bounded renewal; a passive tab does not keep a session
alive. Renewal cannot resurrect an expired session or move its absolute deadline.
Creating a device credential and approving an OAuth application require recent
password authentication. Session status, renewal and reauthentication never return
the owner cookie value. Logout invalidates that session and dependent browser grants;
independent device credentials and consented apps remain until separately revoked.
Operator password reset and restore revoke sessions, device credentials and OAuth
grants. Reset fails closed for an unknown OAuth schema rather than silently keeping
credentials valid.

Browser mutations require an exact allowed Origin and a session-bound CSRF token.
Server-owned control gate pages may load the packaged preference-menu module with
`script-src 'self'`. It dismisses menus, manages focus and changes the non-authentication
theme preference without submitting the password form. Native preference forms remain
available without JavaScript. OAuth consent also loads packaged preference controls, which write only the allowlisted language and
theme cookies; native consent works without JavaScript. Its reauthentication
form associates the owner username with the password field for browser autofill. Revision and legacy-host gates do not receive that enhancement.
Server-owned form pages use `Referrer-Policy: same-origin` so native form POSTs
retain their exact Origin; cross-origin referrers remain withheld. Their
`form-action 'self'` policy does not permit password submission to author content
or OAuth clients. Authorized POST transitions use the control-only
[native form continuation](implementation/API.md#native-form-continuation).
Its opaque ticket is stored hashed. Its destination and protocol fields are
AES-GCM sealed under a purpose- and origin-separated HKDF key from `server.secret`;
the cookie realm, subject and audience are authenticated as associated data.
This preserves one-use credential confidentiality in the intent table. It does
not encrypt publication files or complete backups; protect the server secret and
backup according to the host threat model.
Do not expose the application port directly alongside the trusted proxy or trust
arbitrary forwarded headers from the internet. The supplied proxy replaces client-IP
and scheme headers and forwards the original Host. Do not embed passwords, session or device credentials, or OAuth access/refresh
tokens in URLs, published/static HTML, skills, model context, logs or command
arguments. Explicit owner copy actions may use a transient manual-copy field when
the clipboard is unavailable; it is cleared on closure, navigation or logout. One-use protocol references follow the documented browser/OAuth flow
and must stay out of logs. Theme/language cookies
are non-authentication preferences and survive logout without granting access.

## Connected applications and file delivery

The optional remote MCP service uses an authorization server on the same instance;
it does not require an external identity provider. Dynamic registration is bounded
and rate-limited. Registration accepts exact canonical HTTPS callback URLs; it does
not fetch supplied metadata, logos, JWKS or arbitrary discovery URLs. Registration
does not itself confer access: the owner must approve the named app, callback and
scopes in a CSRF-protected consent bound to the owner session. Recent authentication
is checked inside the grant transaction to prevent a reset/consent race.

Authorization code flow requires S256 PKCE. Codes are short-lived, client-bound and
consumed atomically once. Opaque access/refresh tokens and confidential client secrets
are hashed at rest. Refresh rotates tokens; reuse of a spent refresh credential or
authorization code revokes its family. Downscoping cannot be reversed by refresh.
The protected audience is exactly this instance's `/mcp` resource. Those tokens are
rejected as external REST API bearer credentials. Within the server, MCP reuses the
material API using a private ASGI scope callback; it revalidates the grant per operation
and cannot become the owner. No network header selects this trusted callback.

`materials:read` and `materials:manage` concern materials only. A management app can
publish, replace and delete material; grant it only to a client you trust. App revocation
closes its family and pending/unexchanged approvals. It does not delete material the
app already published. Revocation prevents subsequent authorized operations; it cannot
retract an already returned result or bytes. Tool metadata and protocol challenges
support account linking without embedding credentials in model-visible arguments.

A browser-upload job is a nonsecret request reference, not an upload/read credential.
Only its creator or the owner can inspect it. The owner signs in, chooses the real
file, reviews the plan/dependency report and explicitly publishes it. The app supplies
no password or server path. A job is claimed once; final commit rechecks the owner
session, creator's live grant, deadline and job/upload binding in the publication
transaction. Cancelling or revoking pending authority aborts temporary work; it never
silently deletes a published material. Local MCP filesystem tools stay local to the
client process and are absent from the remote tool surface.

An optional host-file adapter is disabled by an empty `PUBLISH_MCP_FILE_HOSTS` setting.
Enabling it adds a remote attachment tool for exact operator-approved DNS names; it
does not permit wildcards, arbitrary URLs, IP literals or private-network addresses.
Every DNS answer must pass public-address validation, and the connection pins the
selected IP with original-host TLS verification. Redirects, ambient proxies, URL
userinfo and compressed/chunked source responses are rejected. No owner/device/OAuth
credential is forwarded upstream. Bounded source bytes stream into an ordinary reserved
upload, not an unaccounted spool. Source deadlines and DNS worker slots are bounded;
site validation and sensitive-name checks still run before commit.

The operator trusts an allowlisted host to serve the intended attachment, and the
caller still chooses a URL on that host. `file_id` is a reference label, not an
independently verified signature or proof that a cloud host supplied it. An allowlist
does not make the attachment trusted. Confirm the real provider's reference format,
host and response behavior before enabling it. Signed source URLs may be credentials:
do not log or copy them into publication metadata. On an ambiguous commit response,
inspect the upload before retrying; automatic retry could duplicate a publication.
The shared commit boundary leaves an unknown result intact and returns its upload ID
for authenticated reconciliation. Status is bound to the original upload subject and
reveals no saved plan or bytes. A historical `committed` result does not reopen deleted
content; a missing/expired record does not prove no commit occurred. CLI/local MCP and
host-file transfers use the same behavior. Schema-validation failures at the MCP SDK's
public tool-call boundary omit rejected values, so signed URLs and malformed secret
inputs do not leak through generated validation text or SDK error logs.

Public downloads publish the installer, CLI wheel, skill and plugin with instance
URLs. Server startup checks the configured CLI wheel's archive paths, package
identity and console entry point before making it available. These assets contain
no account credentials. Verify the installer and wheel hashes before executing downloaded code.
They are not proof of publisher identity when the origin itself is compromised, and
they do not install into or authorize a cloud account automatically.

## Publication names and link previews

Random link names default to an owner-configurable 8–16 characters; the allowed
4–32 range is a usability choice, not an access guarantee. Five lowercase
alphanumeric characters have about 26 bits of uncertainty; eight have about 41.
Per-IP throttling slows casual enumeration but distributed callers can use many
IP addresses. Use password protection or private access for confidential material.
Explicit custom names can be easier to guess. Existing and deleted names remain
reserved; automatic collisions never overwrite them.

Stable opening admission counts missing, private and preview requests before
looking up the publication. Publication and authentication counters have independently bounded admission
pools, so filling the public pool does not consume owner-login capacity. The
persisted fixed-window limiter survives restart; its IP attribution depends on the trusted proxy boundary. Password verification
has a separate, tighter publication/IP budget. Owners can tighten the public budgets in Settings → Security. An exhausted bucket returns HTTP 429 and a `Retry-After` delay. Configure upstream request limits
and monitoring as described in [self-hosting](SELF_HOSTING.md#link-previews-and-guessing-protection).
The navigation intent, handoff and reader stores reserve additional capacity for
requests authorized by an owner session. Password viewer grants use the guest policy.
Neither limiter prevents a distributed denial of service.

Link-preview crawlers receive title/summary only for guest-accessible link
materials. Owner sessions and previous guest unlocks do not expand this metadata.
Other existing records show the brand and a generic access/status caption, without
the saved title, summary, filename, hint or content. The
generic image is a public packaged PNG. A public site's preview can use its selected
revision's sanitized raster cover through a separately authorized image route.
Escaped metadata comes from the publication record, not uploaded HTML. Making a public link private
stops future public metadata delivery; it cannot retract cards already cached by
Slack, Telegram or another recipient.

## Author content and browser origins

HTML, SVG and complete static sites are executable author content. They do not become
trusted because an AI agent generated them. They run on a distinct origin for their
immutable revision, separate from the cabinet and other revisions. A browser opening
selects a revision and revalidates its existing reader capability or establishes
one through a bounded, one-time, nonce-bound handoff. Owner cookies
are not parent-domain cookies and are never sent to content hosts. Management and
account handlers do not exist on content hosts. Host-based content dispatch keeps
author files independent of control path names; an uploaded `/api/` or `/static/`
file remains reader-authorized content and cannot acquire control authority. The
root `/__mp` namespace is reserved for the browser protocol. The content DNS suffix
equals the control hostname, so embedded preview keeps its
necessary host-only cookies within the same browser site.

A normal GET opening may reuse an already valid reader cookie on that revision
origin. The new one-use intent alone grants nothing: the existing cookie must
prove the same publication, revision, subject, access version, history mode and
sharing epoch. Both capabilities must satisfy current new-opening authorization;
an old reader's drain allowance is insufficient. One write transaction consumes
the intent, renews the reader and records an eligible opening. Mismatches fall back
to the full nonce-bound handoff without consuming the intent. Password POSTs,
HEAD, prefetches and guest-probe openings use the full protocol. Every subsequent
file request still rechecks access and retention. Reader renewal does not renew
the owner's independent idle or absolute session deadline.

A protected page can open without a separate publication password for a signed-in
owner when the owner enables that convenience. Guests still follow the publication's
access policy. This is a server authorization decision, not an owner query parameter
or browser-local flag. Logout/revocation invalidates dependent authorization.
The cabinet's **Check as guest** action ignores owner authorization and previous
normal reader unlocks for that opening. A separate probe/read-cookie realm protects
the owner's ordinary session. It is not a separate browser profile and does not
isolate arbitrary external navigation initiated by a published page.

The owner workspace at `/p/{link_name}` is a trusted control-origin shell. Author
content stays inside a distinct-origin iframe; no controls or credentials are
injected into uploaded HTML. Sandboxing allows ordinary new windows but not
top-level navigation of the shell. The clean-view preference is presentation only,
not authorization. Owner history access is session-bound and separate from
retired guest grants; restoring creates a new revision after recent authentication
and a current-revision compare-and-swap check. Explicitly shared version links
recheck the sharing epoch, retained files and current material access on every
request; they never grant access to the rest of the history. See
[revision history](REVISION_HISTORY.md).

Origin separation does not sanitize a publication, make its content honest, or stop
an authorized script from using information already available to it. The network
profile permitting HTTPS resources/APIs allows calls from the visitor's browser.
Use the `self_only` profile when external calls are unwanted; remote fonts, CDNs and
APIs may then break. A published site is a built static artifact: the server does not
run its build, execute supplied shell commands, or fetch arbitrary source URLs.

Service Workers and offline/prewarmed-cache behavior are outside the supported
profile. Requests identifying Service Worker scripts must be rejected; the proxy must
preserve `Service-Worker` and `Sec-Fetch-Dest`. Ordinary dedicated/shared workers are
allowed only within the tested worker policy. Do not infer browser safety solely
from a CSP string or an HTTP test. See the release acceptance checks below.

## Interface resources and caching

Only release-owned resources under the control origin's `/static/` path use
`private, no-cache`: a browser can store them but must revalidate before reuse.
Successful revalidation uses content-digest ETags, with separate compressed and
identity representations. These files are public and independent of credentials;
GET/HEAD serving skips owner-session and token lookup. Host, path and Service Worker
admission checks still apply. Published files on revision origins never use this
shortcut, including files whose author-chosen path begins with `/static/`.

Owner pages, API responses and published material retain `no-store`; this exception
does not authorize caching private data or bypassing checks on author-content 304,
Range or HEAD responses. Packaged UI assets change only with a restarted release.

## Expiry, bytes and retention

Every new content request, including HEAD, Range and conditional requests, must pass
authorization before response bytes or a 304. Expiry/revocation blocks subsequent
requests. It cannot retract bytes already delivered or guarantee interruption of a
transfer that began while authorized. A recipient can save, screenshot or redistribute
what they were allowed to receive. `no-store` is not remote erasure.

Access expiry, actual file deletion and library-record deletion have independent
policies. Recovery retains bytes within the quota so the owner can extend access;
it does not reopen a guest link by itself. A cleanup lease protects an active transfer
until the transfer exits or its bounded deadline is reached. Publication defaults do
not rewrite existing access/cleanup policies. History retention and its allocated
budget are instance-wide: lowering them can delete saved previous files during
cleanup, while current files remain protected from history collection.

Deletion on the active server does not remove recipient copies or existing backups.
An older backup may contain a subsequently deleted publication. A restore must revoke
restored credentials and apply the latest available deletion ledger when reconciling
an older snapshot. Without that later ledger it cannot know about later deletions.
Keep deletion records separately and test deleted IDs before reopening a restored
instance. Follow [the backup/restore procedure](SELF_HOSTING.md#backup-and-restore).

## Opening counters

Owner-only aggregate statistics distinguish accepted guest and signed-in owner
openings; neither measures unique people or completed reading. Owner previews,
historical and clean views count as owner openings; guest access probes and the
workspace shell do not. Owner authority is revalidated when its handoff is consumed.
The aggregate tables store counts by publication/day plus lifetime totals; they
contain no IP, visitor IDs, raw user agents, referrers or per-visit logs. Security
throttling and server access logs are separate and follow their own retention policies.
Guest and owner collection can be disabled independently per material. Clearing all
counts requires recent owner authentication and exact-origin CSRF; backups retain
the aggregate state they captured.

## Host and parser boundaries

The supported deployment uses [Docker Compose](SELF_HOSTING.md), one application
worker and a local persistent volume. The server runs on Linux inside the container.
The [supported environments](../README.md#supported-environments) list host platforms.
Thumbnail workers depend on enforceable Linux resource limits; a successful client
run on another OS does not establish server isolation there. Network filesystems,
multiple independent writers and relaxed container caps require their own validation.

Uploads are admitted before parsing against actual disk availability, configured
budgets and reservations. A ZIP is extracted only in explicitly selected site mode.
The site parser checks central-directory size and record count before allocating
entry metadata. Path traversal, links, duplicate/colliding paths and expansion
limits are checked before promotion to the serving tree. Failed preparation or a revision
conflict must leave the existing revision and access policy intact. Raster thumbnail
work is bounded and separate; HTML is never executed to generate a cover.
Optional [site covers](implementation/API.md#site-covers) use an exact asset name
from the committed manifest. The worker opens only supported raster formats, with
the same lease, storage and decoder budgets as standalone image thumbnails. The
derived image is served through the authenticated material API.

The supplied containers run without root, with dropped capabilities, no privilege
escalation, a read-only root filesystem, process/CPU/memory limits and bounded logs.
These controls reduce impact; they do not replace host patching, container/image
review, disk monitoring or access control over the volume and Docker daemon.
Container builds and locked server test environments verify SHA-256 hashes of Python
wheels. Review updated locks and base-image digests on supported architectures.
Application secrets and DNS/TLS private keys are never part of the image or git
history. Access logs omit query strings, cookies, auth headers and request bodies.
The nginx error log is restricted to critical diagnostics to avoid routine request
context; application logging must keep credentials and handoff material redacted.

## Engineering checks

- For account, upload and publication changes, identify the caller, scope, decision
  point and data effect. When a write relies on current authority, recheck it in the
  transaction that commits the write. Use tests for both permitted and denied
  callers and check the resulting state.
- For files and structured inputs, check the opened object and apply resource
  limits before materializing untrusted bytes or metadata. Include ordinary boundary
  cases at and above configured limits.
- Bound hot-path work independently of the size of the full data set. For receipt
  and asset responses, prefer indexed lookups of the requested record and test the
  normal response with a large synthetic fixture.
- Use synthetic accounts, credentials and content in disposable instances. Record
  the exact source, image architecture, package versions and check results. Review
  dependency advisories against the installed package and active feature set.

Use the [OWASP ASVS](https://owasp.org/www-project-application-security-verification-standard/)
to select applicable checks for a code change.

## Release acceptance

Before making a build available to untrusted visitors, record executed checks for:

- Owner/private/link/password access; wrong credentials; token scopes; exact Origin,
  CSRF, host-only cookies, session rotation, logout and device-token revocation.
- Idle/absolute expiry, visible-action renewal, recent-auth retries, safe login
  continuation and stale asynchronous secret responses across logout/new login.
- OAuth callback/issuer/resource binding, PKCE, code replay, rotating-refresh reuse,
  downscope, consent/session/reset races, app revocation, DCR bounds and no metadata
  fetching. External API tokens must not acquire MCP's internal principal authority.
- Browser-upload creator isolation, one-time owner claim, cancellation/expiry,
  grant/session revocation at final commit and cleanup of interrupted temporary data.
- Optional host input: exact allowlist, all-answer public IP checks, DNS pinning/TLS,
  redirect/proxy/header rejection, truncated/oversized/stalled upstreams, active quota
  reservation, revoked-creator cleanup and ambiguous-commit behavior. Actual provider
  attachment delivery remains a distinct integration check.
- Nonce-bound handoff replay/expiry; disallowed Host; no management paths or owner
  cookies on content origins; independent origins for different revisions.
- Chrome, Firefox and Safari: iframe and new-tab opening; subresources and Range;
  dedicated/shared worker policy; Service Worker registration and cache-prewarm
  attempts; access after expiry, password change, pause, replacement and logout.
- Conditional requests authorize before 304; no cache/CDN bypass; missing SPA assets
  remain 404; unknown/archived files download under the intended policy.
- Interrupted upload, malformed/large archive, path attacks, resource exhaustion,
  quota races, restart recovery, active-read cleanup and failed CAS replacement.
- Clean wheel installation outside the checkout, packaged resources, consistent
  backup/restore, restored-credential revocation, a later tombstone ledger and
  a legacy migration rehearsal with rollback data retained.

Record the exact build, test environment and result for each check in the release
review. Verify browser behavior in the relevant engines and rehearse restoration of
the same build on a disposable deployment.
