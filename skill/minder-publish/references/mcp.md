# Connected MCP

Use the selected instance's discovered tool schemas and capabilities. The MCP
connection owns authentication; no owner password or device token belongs in a
tool argument, skill, generated file or chat message.

When helping install a cloud connection, ask once whether the user wants the host
to ask before each tool use or allow this connection without repeated prompts.
After consent, use only controls actually available in that host; explain any
unavailable choice instead of claiming it was applied. Preserve an existing host
policy when updating or reinstalling a skill or checking a connection; do not ask
again or reset it. Host prompts are separate
from OAuth `materials:read` / `materials:manage` access. The skill adds instructions,
not grants. Claude's per-tool Always ask may be unavailable, and conversation mode
also matters; check the host's current controls and guidance.

For existing materials, `list_artifacts` searches title, summary and filename;
`get_artifact` returns identity, revision and lifecycle. Use the actual discovered
names if the host namespaces them.

`publish_content` and `update_content` currently accept bounded UTF-8 text/HTML,
with a 1 MiB limit. Inline content is not a binary upload. Omitted metadata is
preserved on content update. Return the actual stable URL and lifecycle from the
result.

When supported by the discovered schema, `change_summary` describes a new revision;
`summary` remains the material description used by search and public link cards.

For a real PDF, image or complete site, use a supported file-input capability
only when the selected host and instance expose it. Never send a local/server
pathname as though the remote service could read it. Do not convert large binary
files to text or fetch an arbitrary URL as a workaround.

If an authenticated browser upload job is offered, explain the required user
step, open its control-origin upload link, and check completion through the
provided status tool. Its ID does not itself authorize upload or reading.
Do not claim this capability exists merely because a remote MCP is connected.
If neither file transfer nor a browser job exists, state that limitation and
use the cabinet or the already-configured local CLI instead.

Read errors before retrying. A failed/expired upload, missing file permission,
revoked connection or unsupported option must not produce a guessed link.
For `unknown_outcome`, inspect its `actual.upload_id` with `get_upload` before
retrying. A committed status proves the upload committed; inspect the material
separately for current availability. If the outcome remains unknown, report it
and stop instead of repeating the mutation.
Preserve the user's chosen access and cleanup policy across transports.
