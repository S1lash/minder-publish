# Self-hosting Minder Publish

[Русский](SELF_HOSTING.ru.md) · [Security model](SECURITY.md) · [API](implementation/API.md)

Minder Publish runs as one owner-controlled instance. It publishes ordinary files
and explicitly selected built static sites, with a browser cabinet, CLI and optional
MCP adapter. The server runs in Docker Compose on the
[supported hosts](../README.md#supported-environments). The host does not need
Python to set up and run the server.

## What to prepare

- Docker with BuildKit and Compose on Linux (for example an Ubuntu VPS) or
  OrbStack on macOS. The host needs persistent storage and outbound access to
  build the chosen release.
- A control hostname, for example `publish.example.com`, and wildcard DNS for
  `*.publish.example.com`, pointing to the same reverse proxy. The content suffix
  must equal the control hostname. Each immutable revision
  uses its own hostname beneath it. This keeps embedded preview on the same browser
  site while isolating publication origins. Separate cross-site suffixes are rejected.
- A TLS certificate covering the control hostname **and** `*.CONTENT_DOMAIN`.
  A wildcard alone does not cover its apex. Obtain and renew it using your chosen
  certificate provider's [DNS-01 process](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge). Keep DNS credentials outside the application.
- Capacity for live data, staging, retired revisions and backups. The configured
  storage budget is an admission envelope, not a measurement of free physical disk.

The supplied Compose profile sets a 1 GiB / 1 CPU application cap, a 128 MiB / 0.5 CPU
proxy cap, process limits and bounded Docker logs. These are initial operator limits,
not a capacity guarantee. Use a server with enough additional memory and disk for its
OS, Docker and your backup process. Application defaults, ceilings and parser budgets
have one source in [`defaults.json`](../src/minder_publish/defaults.json).

## Configure and bootstrap

Use the source for a [published release tag](https://github.com/S1lash/minder-publish/releases/latest).
These commands build and start containers on your own host. Review the Dockerfile
and Compose configuration before running them.

```sh
cp .env.example .env
```

Edit `.env` with your HTTPS control origin, content suffix and storage budget. Use
plain literal `NAME=value` entries. Use an ASCII DNS hostname (Punycode for an
internationalized name) with no trailing dot. The renderer reads the selected
environment file. `PUBLISH_DATA_DIR=/data` is the container path;
Compose mounts the named `publish-data` volume there. Do not add a password or token
to this file. Bootstrap creates `/data/server.secret` with restricted permissions.
An external secret manager can supply `PUBLISH_SECRET` instead; its value must have
at least 32 random bytes. Treat it as an application credential.

```sh
docker compose build &&
docker compose run --rm --no-deps -T --entrypoint python app \
  /opt/minder-publish/deploy/publish/render_config.py --env-file - --output - \
  < .env > deploy/publish/nginx.conf.tmp &&
mv deploy/publish/nginx.conf.tmp deploy/publish/nginx.conf &&
docker compose config --quiet &&
docker compose run --rm --no-deps app bootstrap --username owner
```

The bootstrap command prompts without echoing the password. For automation, use
`bootstrap --username owner --password-stdin` and send the password on standard
input from your secret manager. Never put the password in command arguments or a
committed file. Owner passwords must have at least 12 UTF-8 bytes. There is no default
account or remote first-user registration endpoint.

Compose sets `PUBLISH_TRUSTED_PROXIES=*` only inside its dedicated app/proxy network.
Only nginx publishes host ports. Keep the application socket private.

The data directory is owned by UID/GID `10001:10001` and mode `0700`. Keep that
ownership when using a bind mount instead of the named volume; do not fix permission
errors by making data world-readable. The image runs as this non-root account, with
a read-only root filesystem and a small temporary filesystem.

## Link previews and guessing protection

Set `PUBLISH_BASE_URL` to the externally reachable HTTPS control origin. The service
builds absolute Open Graph/Twitter Card and public PNG URLs from it; no Slack app,
Telegram bot token or hard-coded maintainer domain is needed. Crawlers must reach
both stable links and `/static/link-preview.png` without a proxy login or challenge.
A private network or upstream access wall cannot supply an external chat preview.
Share the stable `/p/{id}` URL returned by Publish. Public cards use saved title and
summary; protected cards use the brand and a generic access caption. Chat clients can disable or cache
previews. [Slack's crawler](https://api.slack.com/robots) reads this metadata without
JavaScript; [Telegram](https://telegram.org/blog/link-preview) fetches link previews
on its own infrastructure. A successful local fetch is not proof of a provider's
current cached card.
The stable document entry exposes metadata to unknown crawlers too; browsers
continue automatically with a visible fallback link. See
[link previews](LINK_PREVIEWS.md) for platform guidance and image sizing.

In Settings → Security the owner can limit stable link opens per IP (1–300 per
minute, default 300), including missing names. Password checks allow 1–10
submissions per material/IP (default 10) in a 15–60 minute window (default 15).
Each window starts at its first request. Once exhausted, further requests return
429 and `Retry-After` in seconds. Retries do not postpone reopening. New attempt
counts apply immediately; the window duration applies to newly started windows.
Owner sign-in has a separate fixed budget. Verify `429 rate_limited` on an
isolated test instance, including misses and forged `X-Forwarded-For` headers;
do not flood a live instance to test this. Publication and authentication attempts use separately bounded, persisted SQLite
pools. Filling the publication pool does not consume owner-login capacity. A
restart does not reset an active bucket.

Keep the application socket private. The supplied nginx configuration overwrites
`X-Forwarded-For` and `X-Real-IP` from the actual peer; trust only that proxy at the
application. With a CDN, configure its documented, exact trusted address ranges
before accepting its real-client-IP header. Blindly trusting client headers lets
attackers evade per-IP budgets; treating every visitor as the proxy IP shares one
budget among everyone.

For an internet-facing instance, also apply a request budget at the reverse proxy
or WAF so rejected traffic does not reach SQLite. nginx's
[request limiter](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)
can use `$binary_remote_addr`, a bounded shared zone, a small burst and status 429.
Apply opening limits to control `/p/` requests, including misses; exclude static
assets, health checks and authorized site subresources from that opening budget.
Tune upstream limits for your expected NAT/proxy population, monitor 429s and resource
pressure, and use provider-level controls for distributed traffic. These are
additional operator controls; the supplied proxy does not claim a configured WAF.

Keep the default minimum length of eight unless a more guessable address is an
explicit choice. Minimum and maximum are owner settings; shorter names, including
custom aliases, never replace password/private authorization.

## Install your certificate

The proxy reads `deploy/publish/tls/fullchain.pem` and `privkey.pem` read-only.
Use the commands for your host and replace the example paths with the files from
your certificate provider. Keep the TLS directory and private key restricted.

On Linux, set the container's UID/GID `101:101` on the files:

```sh
sudo install -d -m 0750 -o 101 -g 101 deploy/publish/tls
sudo install -m 0400 -o 101 -g 101 /path/to/fullchain.pem deploy/publish/tls/fullchain.pem
sudo install -m 0400 -o 101 -g 101 /path/to/privkey.pem deploy/publish/tls/privkey.pem
```

On macOS with OrbStack, install the files as the signed-in user:

```sh
install -d -m 0700 deploy/publish/tls
install -m 0400 /path/to/fullchain.pem deploy/publish/tls/fullchain.pem
install -m 0400 /path/to/privkey.pem deploy/publish/tls/privkey.pem
```

On either host, check the mounted configuration and TLS permissions before starting:

```sh
docker compose run --rm --no-deps proxy -t &&
docker compose up -d --wait &&
docker compose ps &&
docker compose exec app minder-publish-server doctor
```

`proxy -t` adds `-t` to the configured nginx entrypoint; it validates the mounted
configuration and certificate without opening public listeners. An application's
unhealthy status is reported by its HTTP healthcheck; `restart: unless-stopped`
restarts exited processes, not merely unhealthy ones. Monitor the externally
reachable HTTPS endpoint as well as container health and disk usage.

Publish a small private file, a password-protected file and a site ZIP; open them as
owner and in a separate guest browser. Check subresources, a missing asset, downloads,
Range requests, expiry and logout. The browser security gates in
[SECURITY.md](SECURITY.md#release-acceptance) must pass before exposing a release to
untrusted visitors. A successful healthcheck does not prove those properties.

Your certificate renewal process must replace the mounted files with the same
restricted ownership, run `docker compose exec proxy nginx -t`, then
`docker compose exec proxy nginx -s reload`. No certificate provider account, DNS
credential or renewal service is bundled into the application.

## Connect an AI tool

The control origin's `/` shows a neutral page to a signed-out visitor, with guidance
to open the received material link and an owner sign-in action. It does not list
publications or offer registration. The owner signs in at `/login`; successful sign-in
opens the library at `/`. A signed-in owner opening `/` gets the cabinet directly.
Signing out returns to the neutral page; an expired owner session returns to `/login`.
Normal owner material links open a thin management rail with versions, download,
password copying and guest-opening counters. The owner can remember a clean-view
preference in this browser, or open the original separately while keeping the
workspace available. A guest access check exercises real access rules without
adding to counters. No owner controls or statistics are exposed to guests.
Language (English, Russian, Spanish or French) and theme use the same host-only
preference cookies on these pages. Material links retain their own access rules.

Sign in at `/login`, open **Connection**, and choose your assistant. For Codex or
Claude Code, copy one setup instruction into the assistant: it installs the verified
client release and compact skill together. It reports an absolute connection command
for your own terminal, so installation does not depend on the executable being on
`PATH`. For a new connection, create a device token with **Manage materials**
and enter it into that command’s hidden prompt. Setup checks the connection.
Open a fresh chat to load the installed skill. The token stays outside model context.
Manual installation and skill repair instructions are in the advanced section.
Read scope is sufficient for tools that only read materials. Run the Claude Code
installer from the same Claude configuration you use for publishing, so it can
honor its `CLAUDE_CONFIG_DIR` when set.

The Docker image retains its CLI-only wheel at `/opt/minder-publish/client.whl`.
For a direct source installation, run `python scripts/build-client-wheel.py` in
the release checkout and set `PUBLISH_CLIENT_WHEEL` to the absolute path of the
wheel in `dist/client/`. The version must match the running server. The cabinet
provides download links and checksums for the installer, CLI wheel, complete
skill ZIP and plugin package. The CLI download is unavailable when no wheel is
configured.

Remote MCP is disabled by default. For Docker Compose, set `PUBLISH_REMOTE_MCP=true`
in `.env`. If the service is already running, apply the new environment to the
app container from the release directory:

```sh
docker compose up -d --wait --no-deps app
```

The image includes the MCP dependencies. For a direct source installation,
install the `mcp` extra, set `PUBLISH_REMOTE_MCP=true` in the server environment
and restart the single worker. Use `https://YOUR_CONTROL_HOST/mcp` as the
endpoint and canonical resource. The proxy must forward Authorization and support
unbuffered requests. Provider servers must be able to reach the HTTPS endpoint;
a desktop browser reaching an IP-restricted instance does not prove cloud access.

In ChatGPT, choose OAuth with automatic client registration (DCR); leave static
client ID and secret empty. The instance supplies discovery and supports PKCE.
Set up ChatGPT on the web. OpenAI documents custom MCP write access for eligible
Business and Enterprise/Edu workspaces. If your client is read-only or lacks the
app, use the cabinet or CLI for publication.
[ChatGPT custom MCP availability](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
Claude can also use OAuth; choose **Register automatically** for its OAuth client.
A client supporting fixed credentials can instead use its own device token in the
`Authorization: Bearer ...` header; in Claude choose **No sign in** for this alternative.
Publishing requires `materials:manage`, which includes reading, creating, changing
and deleting materials. `materials:read` is read-only. Review the requested permissions
on consent before approving. Initial MCP discovery recommends management for this
publishing workflow. Existing read-only connections keep their permissions; reconnect
and approve management before publishing when the host cannot request it per tool. Neither
OAuth nor device grants can administer the account, settings or other credentials.
Revoke applications or devices independently in **Manage connected apps and devices**.

After connecting, choose how the host should approve tool use: ask each time, or
allow this connection to act without repeated prompts. Automatic approval can permit
changes and deletion within the granted access. In ChatGPT, review the Publish app's
permission controls or its approval prompt; options depend on the account, app and
action. In Claude on web or desktop, open **Customize → Connectors → Publish →
Permissions** and review each tool. **Always ask** is rolling out and may not be
available; the conversation mode matters too. See Claude's [tool permission guide](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork).
These host prompts are separate from the OAuth scopes above. When assisting with
installation, ask the user for their preference once, then apply it only using
controls the host actually offers; explain if the chosen behavior is unavailable.
A skill adds instructions and does not grant access or change host permissions.

Install or enable the resulting connection and start a new chat. Use **Copy
connection check** for a read-only check, then **Copy publish test** to verify
that the client can publish and update an HTML sample at the same address.
The connector can run these tools without a skill.

Connection supplies one skill ZIP with all references for either application.
In ChatGPT, open Plugins → Skills → Create → Upload from your computer. In Claude,
open Customize → Skills → + → Create skill → Upload a skill; some layouts place Skills under
Plugins or Customize. Upload the ZIP and enable the skill with the existing MCP
connection. In Claude, enable code execution and file creation if **Skills**
is missing. Complete setup on the web or desktop, then open a new chat on each
device and check whether its connected app and skill are available.
Follow Claude's [connector guide](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities)
and [skill upload guide](https://support.claude.com/en/articles/12512180-use-skills-in-claude)
for the controls available in the account. Confirm actual account and mobile
availability before relying on an installation across devices. A separate plugin
bundle is available for applications that consume plugin packages; it is not
required for this direct skill-upload flow.

Inline UTF-8 text and HTML up to 1 MiB can be published and updated directly.
For PDFs, images and static-site ZIPs, `create_file_upload` provides an authenticated
browser handoff: the owner signs in, chooses the file and confirms. This creates a
new material; replacing an existing binary file or site requires its cabinet controls
or the configured CLI. `get_file_upload` returns the confirmed publication. Remote
`publish_file` and directory-reading tools are absent. Optional provider file input
is configured with `PUBLISH_MCP_FILE_HOSTS`, a comma-separated list of exact trusted
HTTPS source hostnames. Keep it empty until the provider's real file source is
verified. There are no wildcard hosts, arbitrary-URL imports or redirect following.
The fetch uses the same upload quota and preparation workflow. Protocol details and
security boundaries belong to [the API](implementation/API.md) and
[security model](SECURITY.md).

Do not put passwords or tokens in a skill, publication, command argument, endpoint
URL or model context. Every computer and connected app should have its own revocable
grant. The public password hint is visible before unlocking a protected publication;
it must not contain the password or other secrets.

## Owner sessions

The owner account is created with the local `bootstrap` command. Its username may
be an email address; this does not require SMTP or enable password-reset email.
Use the local `reset-password` command for operator recovery. Passkeys are outside
this release.

The browser session has an idle timeout and an absolute lifetime. Visible owner
actions renew it at the configured interval; background tabs, health probes and
publication content do not keep it alive. The cookie and database expiry are
updated together, without reviving expired sessions. Sensitive connection creation
requires a recent owner password confirmation while preserving the current form.

Operator environment variables override their namesake defaults from
[`defaults.json`](../src/minder_publish/defaults.json):

| Environment variable | Policy |
| --- | --- |
| `PUBLISH_OWNER_SESSION_IDLE_DAYS` | Time without owner activity |
| `PUBLISH_OWNER_SESSION_ABSOLUTE_DAYS` | Maximum lifetime after login |
| `PUBLISH_OWNER_SESSION_REFRESH_SECONDS` | Minimum renewal interval |
| `PUBLISH_OWNER_RECENT_AUTH_SECONDS` | Fresh confirmation window |

All values must be positive integers and satisfy the session policy constraints.
Apply changes to the Compose environment with `docker compose up -d --wait --no-deps app`.
For a direct source installation, restart the worker. Logout revokes that browser session;
password reset revokes owner sessions, device tokens, OAuth grants and content
access credentials. An ordinary logout does not disconnect independently authorized
applications.

The CLI prepares a private temporary snapshot before reporting and sending its
files. Allow additional local temporary disk up to the selected upload payload,
plus the copy/archive preparation work. Editing the original after that preparation
does not change the bytes uploaded by that operation. A separate `preflight` command
does not reserve its snapshot for a later `publish` command. Pass the reviewed
`manifest_sha256` as `--expect-manifest HASH` to reject content changes before
uploading. The exact preparation
and cleanup contract is in [Client preparation](implementation/API.md#client-preparation).

## Storage, expiry and updates

Publication access expiry, file deletion and library-record retention are separate
steps. Expiry closes guest access on time. The recovery period retains files within
the quota so access can be extended. Automatic file deletion can run immediately,
after a recovery period or be disabled by operator policy. Record retention begins
when files are actually deleted, not when access expires. Files already deleted
require a new upload and an explicit expiry choice.

The cabinet's **Settings** page changes defaults for future publications and owner
limits beneath the deployment ceilings. Publication defaults do not rewrite existing
publication policies. **Upload limits** apply to file transfers from the cabinet,
assistant browser-upload requests, CLI and API. On a new installation, the single-file
and transmitted-upload limits both start at 109 MiB. Existing settings are preserved.
Connection shows the current effective limits for files and sites. **Limits and storage** sets the byte
quota and the maximum number of material records, including retained records whose
files have been removed. Lowering those limits does not remove existing materials.
**Version history** controls retained previous files for the
whole instance. Reducing its count, age or storage budget can remove those files
during cleanup; increasing limits cannot recover them. A zero history limit stops
retaining previous files. Current versions are never history-cleanup candidates.
The storage summary includes history inside the total quota. See
[revision history](REVISION_HISTORY.md) for the retention and restore contract.
Decreasing a quota does not delete existing files; it can block new writes. A site ZIP
has both an uploaded-size limit and an expanded-size/file-count limit. An ordinary
ZIP stays an ordinary file. The application does not run a build or fetch a source URL.

Set `PUBLISH_PROJECT_URL` to display the “Made available via minder publish.” link
on the guest homepage, owner sign-in and material password pages. When the project
URL is empty, a configured `PUBLISH_SOURCE_URL` points these pages to the public
GitHub repository. With neither set, the password page links to the instance home.
Use public HTTPS URLs without credentials, query or fragment. The server renders
the links without fetching their destinations.

Keep a single server worker. Immutable revisions, upload reservations, actual disk
checks, leases and the cleanup worker coordinate access to the local data volume.
Do not run the legacy sweeper against the new data root. An operator can request a
cleanup pass with the writer stopped:

```sh
docker compose stop app
docker compose run --rm --no-deps app sweep
docker compose start app
```

For schema upgrades and offline rollback, follow [schema upgrade and recovery](STATISTICS.md#schema-upgrade-and-recovery) and rehearse with the exact selected release.

## Backup and restore

Keep application backups outside the data root and copy them to separately protected
storage. They contain private publications, metadata, credential hashes and the
publication-password encryption key. A full backup can recover saved publication
passwords. Encrypt backups and restrict access. TLS/DNS credentials are managed
separately. An archive existing on the same disk is not an off-host backup.

For a consistent backup, stop the application writer and its background worker first.
The proxy can remain running; requests fail closed while the application is stopped.
Prepare a private host directory writable by container UID `10001`. On Linux run
`sudo install -d -m 0700 -o 10001 -g 10001 backups`. On macOS with OrbStack run
`install -d -m 0700 backups` as the signed-in user.

```sh
docker compose stop app
docker compose run --rm --no-deps -v "$(pwd)/backups:/backup" app backup --output /backup/snapshot.tar
docker compose start app
```

Use a unique output name for every backup and verify it can be read before discarding
an older restore point. Rehearse restore into an empty, separate data volume with the
application stopped:

```sh
docker volume create minder-publish-restore-check
docker compose run --rm --no-deps -v minder-publish-restore-check:/data -v "$(pwd)/backups:/backup:ro" app restore --input /backup/snapshot.tar
```

Restore revokes restored browser sessions, device tokens, OAuth grants and content
grants. It writes a new `server.secret`. Version 2 archives preserve `publication-password.key`
independently so saved publication passwords remain copyable. The key is created
automatically in the data root with mode `0600`; do not delete, replace or rotate it
independently of its encrypted publication records. Restore accepts version 1
hash-only backups; those passwords continue to work but cannot be copied until
explicitly set again. Older software cannot preserve version 2 password copies;
use a matching image and verified backup for a rollback.
If an external secret manager supplies `PUBLISH_SECRET`, remove the override to
use the restored `server.secret` or provision that file's exact value. Reconnect
trusted tools explicitly. An older snapshot may contain publications deleted later.
Export the latest available deletion ledger from the current instance into separately
protected storage:

```sh
docker compose run --rm --no-deps -v "$(pwd)/backups:/backup" app tombstones --output /backup/latest-tombstones.json
```

Use a fresh output name for each ledger export. Capture the final ledger with writers
stopped during a cutover. When restoring an older backup, supply that current ledger with
`restore --input /backup/snapshot.tar --tombstones /backup/latest-tombstones.json`.
Retain and synchronize the ledger independently of old backups. Its current format
also records link reservations and revocations. Later names remain reserved during
restore. When the latest ledger has revoked the primary name in a snapshot, use a
more recent snapshot instead of serving through an old address. Before exposing
the restored instance, confirm deleted materials and disabled addresses are still
unavailable. The ledger can only account for changes it recorded.

For a core schema 5 upgrade, save a stopped, verified schema 4 backup and the
matching image. The migration keeps every existing current address and pinned
revision URL. Rehearse on an isolated copy. Once named addresses change, recover
with the current build and a compatible backup to keep the link registry. An image
using schema 4 requires the matching pre-upgrade data snapshot.

Select the restored volume through a reviewed Compose override only after checking
counts, bytes, sample files and deletion state. Keep the original volume as a rollback
point. Do not use `docker compose down -v`: it removes the named data volume.

## Import a legacy instance

Use a consistent stopped-writer copy of the legacy database and blob directory.
Mount that source read-only and use a separate target volume. First run:

```sh
docker compose run --rm --no-deps -v /path/to/legacy-copy:/legacy:ro app migrate --legacy-db /legacy/metadata.sqlite3 --legacy-blobs /legacy/blobs --dry-run
```

Review missing files, ID conflicts, counts, bytes and retention dates before the same
command with `--apply`. Import preserves legacy IDs and access semantics; retained
legacy files receive a preservation policy until the owner changes it. Do not point
new code or its sweeper at the original legacy root. Keep the source backup intact.

## Upgrade and rollback

Inspect the chosen release and its migration notes, stop writers, take a consistent
backup, build the pinned dependency set, then start the new version against the
intended data volume. Run `doctor`, test a publication and verify guest denial after
expiry/logout. Keep the prior image and its matching backup. A database migration is
not rolled back merely by running an older image; restore the corresponding data
into a separate volume before changing traffic.

The supplied image tags identify releases, but tags can be republished. A published
image release must resolve and record immutable base-image digests, run a vulnerability
review and pass the [release acceptance checks](SECURITY.md#release-acceptance).
