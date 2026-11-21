# minder publish.

Publish a result, keep control of its link. Minder Publish is a self-hosted service
for people working with AI agents: publish a file or a complete built static site,
open it yourself, or share it with others using an explicit access policy.

One instance has one owner. The browser cabinet searches your publication history;
the CLI and skills let an agent publish actual files. An optional MCP adapter uses
the same API. Each instance manages its account and data locally.

Review the required [security acceptance checks](docs/SECURITY.md#release-acceptance)
before exposing a self-hosted build publicly.

## Watch the demo

[![Watch the 50-second Minder Publish demo](https://publish.minder.world/assets/publish-demo-poster.webp)](https://publish.minder.world/#demo)

Follow one report from an agent’s terminal to a phone, then update and share it at the same address. [Open a sample publication](https://publish.minder.world/example/).

## What you can publish

- Any ordinary file, including HTML, documents, images, audio, video and archives.
- A built static site: a directory through the CLI, or an explicitly selected ZIP
  through the cabinet. The service does not run your build scripts.
- A new immutable revision under the same stable link, preserving omitted metadata,
  access, password, expiry and cleanup rules.

Choose owner-only, link or password access. Access expiry, file cleanup and record
retention are separate policies. The owner can open protected materials through an
active session, search by title/description/filename, pause, extend, expire or delete
publications. Access badges identify password-protected materials; checkbox filters
include expired and deleted-file records on demand. The owner can copy a saved
publication password after recent authentication. New-publication defaults and
storage limits are configurable. The UI
ships in English, Russian, Spanish and French, with light, dark and system themes.

Opening a material as its owner shows a thin management rail around the original
document. The side menu previews saved versions, restores one as a new current
version, copies the stable link or password, and shows guest-opening counts.
**View without owner tools** opens the original separately; **Check guest access**
checks the real password/access gate without counting an opening. The choice to
show owner tools is remembered in this browser. History retention fits inside the
instance storage quota; see [revision history](docs/REVISION_HISTORY.md).

Use **Edit link** in the cabinet or the owner menu to give a publication a named
address. The previous address stops working by default. Keep it when recipients
still need it, and disable it later from the same menu.

Shared links expose messenger-friendly titles, descriptions and a brand thumbnail.
Protected materials use a generic card without revealing their contents. See
[link previews](docs/LINK_PREVIEWS.md) for platform behavior and deployment requirements.

Static sites can include a raster image for their library cover.
See [site covers](docs/implementation/API.md#site-covers) for bundle filenames.

Password protection is an access gate, not encryption. Author HTML runs on isolated
revision origins. Read [the security model](docs/SECURITY.md) before sharing sensitive
or executable material.

## Supported environments

- **Server:** Docker Compose with Linux containers on a Linux host such as an Ubuntu
  VPS, or on macOS with OrbStack. Python for the server ships in the container.
- **CLI and local skills:** Python 3.10 or later on Linux or macOS. Install the CLI
  on the machine where the agent reads the files you want to publish.

For discussion of other environments, see [Contributing](CONTRIBUTING.md).

## Run your own instance

Follow the [self-hosting guide](docs/SELF_HOSTING.md) for DNS and
wildcard TLS, a non-root Compose deployment, local owner bootstrap, budgets, backups,
restore and legacy import.

Sign in at your configured control hostname. **Materials** publishes and manages
files; **Settings** sets publication defaults, storage limits and version retention; **Connection** guides Codex, Claude Code, ChatGPT, Claude and other assistants
through installation, authorization, skills and a connection check. There is no default account or password. Existing materials
keep their publication policies when defaults change. History retention is an
instance-wide setting and can remove saved previous files during cleanup.

## Connect the CLI

In the cabinet, choose **Connection → Codex** or **Claude Code**, then **Copy
setup instructions**. Paste the instruction into your agent. It verifies the
instance's installer, installs the CLI and matching skill in your user profile,
and checks an existing connection. For a new connection, create one device token
in the cabinet and enter it in the private terminal prompt supplied by the agent.
Never paste the token into chat. No PATH changes are required. Each runtime's skill
remembers the instance selected by its latest setup and verifies that server before
publishing; an unrelated global CLI default remains unchanged. The operator must
provide the CLI-only release wheel; the supplied Docker image includes it. No public
package-registry entry is assumed.

To install the CLI manually, use an isolated Python environment and an inspected
source release:

```sh
python -m pip install .
minder-publish configure home --url https://publish.example.com
minder-publish doctor --brief --json
```

`configure` prompts privately for a device token created in the cabinet. For
noninteractive setup, use `--token-stdin` or a private `--token-file`; never put the
token in an argument, a URL, a skill or model context. Named instances are selected
with `--instance NAME`. To connect through environment variables, pair
`MINDER_PUBLISH_URL` with `MINDER_PUBLISH_TOKEN` or `MINDER_PUBLISH_TOKEN_FILE`.
A complete pair supplies the connection independently of saved profiles. Unset
these variables to use a named profile. Configuration uses your user profile.
Redacted configuration
is available with `minder-publish config --json`.
The selected instance applies across project directories. Use another named
instance explicitly when you want another destination. Omit `--brief` from doctor
to inspect the full server capabilities.

```sh
minder-publish preflight ./report.html --kind file --json
minder-publish publish ./report.html --kind file --title "Research report" --json
minder-publish preflight ./dist --kind site --entry index.html --json
minder-publish publish ./dist --kind site --entry index.html --access link --expiry default --json
minder-publish list --query "Research report" --json
minder-publish update MATERIAL_ID ./report-v2.html --kind file --json
```

After reviewing preflight, pass its `manifest_sha256` with `--expect-manifest HASH`
to publish or update. If content or the site entry changed, the client stops before
upload; review the new manifest before retrying. This guard covers content, not
access or expiry policy. Dependency scanning covers static HTML/CSS references;
empty findings do not guarantee that a site works offline.

Private publications normally have no expiry; external links use the instance's
external default. A ZIP remains a file unless site mode is explicit. Preflight
reports included files, sensitive-name exclusions, paths and sizes. Review it before
sending anything; do not bypass exclusions without inspecting the affected files.
Use the server-returned `stable_url` rather than constructing a link.

## Add skills or MCP

The combined setup installs the skill automatically. For a separate skill repair
or installation, preview its diff first, then apply the managed files you reviewed.
Use `--instance NAME` to select its preferred instance; otherwise the saved CLI
default is selected. The preview reports the instance and server URL:

```sh
minder-publish skills --runtime all
minder-publish skills --runtime all --apply
```

The installer changes only the named managed skill; it refuses to overwrite an
unmanaged skill. Other tools can use [the same skill source](skill/minder-publish/SKILL.md)
and the CLI.
Claude Code invokes it as `/minder:publish`; Codex uses `$minder-publish`. The
portable skill archive retains the `minder-publish` name; the installer applies the
Claude Code namespace without copying a second set of instructions.

For optional local MCP, use the source distribution. With a Python version supported by the MCP extra, install
`python -m pip install ".[mcp]"` and configure the harness to launch
`minder-publish-mcp` using the same private instance configuration. It uses stdio by
default. Remote MCP is disabled by default; enabling it is a separate operator choice.
Remote MCP supports owner-authorized OAuth connections as well as scoped device
Bearer credentials. Its tools do not read server filesystem paths. For binary files
or a complete site, a browser upload request lets the owner select, review and
publish the actual file; the assistant retrieves the confirmed link. Optional host
file inputs require an operator-approved source host. See [connection configuration](docs/SELF_HOSTING.md#connect-an-ai-tool)
for setup and provider-specific limitations.

## Development and contracts

The full Python distribution contains `server/`, `client/` and `mcp/` under
[`src/minder_publish/`](src/minder_publish/). Browser resources and translations are
packaged with the server. The [architecture](docs/ARCHITECTURE.md) describes component and data flows.
The [packaged defaults](src/minder_publish/defaults.json) own configuration values;
[security acceptance](docs/SECURITY.md#release-acceptance) defines release checks.
Legacy import and API compatibility are maintained within the same distribution
and verified against synthetic fixtures.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, tests and clean wheel checks.
Application code is [MIT](LICENSE); bundled fonts retain their adjacent OFL notices.
