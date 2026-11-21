---
name: minder-publish
description: Publish a finished file or static site to the user's Minder Publish instance, return a controlled link, or find and update an existing material. Use for publishing and managing results through CLI or connected MCP.
---
<!-- minder-publish-managed-skill -->
# Minder Publish

One entry for publishing, finding and updating results. Claude Code installs it
as `/minder:publish`; Codex uses `$minder-publish`. Both use this same source and
load only the transport reference needed for the current task. For local terminal agents,
start with [this installation](references/installation.md) for the executable
and selected server; project directories do not select a different instance.

Publish the actual finished result to the selected instance. An explicit request
to publish authorizes that result; ask only when the file, audience, or intended
change is unclear. Do not publish an entire workspace when one deliverable was
requested.
Keep Publish as the chosen delivery destination; building the result does not
require choosing or configuring another hosting service.

In connected ChatGPT or Claude cloud sessions, use [MCP](references/mcp.md).
A file in the assistant's sandbox does not imply a configured local CLI;
use the connection's supported file transfer or browser handoff.
For local terminal agents, locate the command using
[local installation](references/installation.md) and read [CLI](references/cli.md).
Load only the applicable reference. A skill supplies instructions, not access
to another environment's files.

A ZIP remains a downloadable file unless the user chooses a site. A site is an
already-built static directory or site archive with an explicit entry. Review
the included manifest and sensitive exclusions before upload. Building the
project is a separate task; never bypass exclusions or execute arbitrary build
scripts merely to publish.

Choose access from the request. For a result only the owner should open, use
private access. For sharing with recipients, use link or password access and
the instance's external expiry default unless another lifetime was requested.
Explain the resulting access briefly. Obtain publication passwords through a
supported private mechanism or the cabinet, not by requesting secrets in chat.

Use a useful title and optional short summary grounded in the result, so it can
be found later. Search or inspect the existing material before updating. Preserve
omitted metadata, access, password and cleanup policy; replace content through
the supported revision check. Changing access to external requires an expiry
choice. Use only fields and capabilities the instance actually supports.

When replacing files, an optional short change summary explains that revision;
it is separate from the material description. History, restoration and detailed
statistics are owner-only cabinet tools unless the connected transport explicitly
advertises a corresponding permission. Never infer owner authority from a token.

Use the returned publication `id` for updates, including after the owner changes
the link name in the cabinet. Return the server's authoritative `stable_url`, access
and expiry. Never invent a URL, claim a pending upload succeeded, or expose
credentials and signed file URLs.
The browser may open a revision subdomain to isolate uploaded HTML/JavaScript
from the cabinet.
After a timeout, inspect the outcome before repeating a mutation. If no
authoritative outcome can be established, report uncertainty and stop instead
of creating another publication.

Pause, expiry and permanent deletion differ: expiry can trigger saved file
cleanup. Make that consequence clear. Permanently delete only the identified
material with explicit authorization. Password protection controls access; it
does not erase downloaded copies or encrypt server backups.
