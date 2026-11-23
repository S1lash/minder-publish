# Owner revision history

A publication has a stable ID and immutable files for each committed revision.
`summary` describes the publication; optional `change_summary` describes one update
and is limited to 500 characters. The history catalog, owner preview and restoration
require owner session authorization. A material token alone does not acquire
historical browser access. A selected historical revision never changes the public
current pointer.

The ordinary `/p/{link_name}` link opens the current revision. A separate
`/p/{id}~{sequence}` link pins one revision. The owner explicitly enables sharing
for that revision before guests can use its pinned link, including when it is the
current revision. All revisions start unshared; sharing one does not expose the
history list or any other revision. Editing the ordinary address leaves pinned
links intact. The owner's authenticated preview works without enabling sharing.

Sharing follows the material's current access, password, pause and expiry rules.
It does not make private material public or extend retained-file lifetime. Stopping
sharing closes the pinned link and invalidates its pending and active guest grants;
reenabling it does not resurrect those grants. The ordinary current link remains
governed by the material's access policy. A revision-host document URL without a
valid reader grant returns to its pinned link; it never substitutes a different
revision. Existing ordinary reader grants retain their bounded transfer drain.

The sharing flag, revocation epoch and pinned address's messenger preview mode live in the revision's JSON metadata
and are included in backup/restore. Enabling or stopping sharing requires recent
owner authentication checked inside the write transaction. Restoring files creates
a new unshared revision; it does not copy an older revision's sharing authorization.

The core history ledger stores monotonic sequence numbers, timestamps, byte sizes
and change summaries separately from retained files. It retains the newest 100
entries per material; total published sequence remains on the publication. Existing
revision rows migrated from schema 2 are marked `baseline`: already collected
revisions are not reconstructed or assigned fictional events.

Available history entries expose their own kind, filename and entry content type.
The owner viewer uses these to choose the selected version's preview; replacing a
material with another file type does not reinterpret earlier files. Access and
document preview policies remain those of the current material. The pinned
address supplies its own [messenger preview policy](LINK_PREVIEWS.md#preview-controls). Collected entries expose
null for these file fields.

Settings `history_previous_versions` (default 4, range 0–20),
`history_retention_days` (default 30, range 0–365), and `history_budget_percent`
(default 10, range 0–100) govern retained previous files. The budget is inside the
operator storage budget. The effective maximum for each setting is exposed under
the matching `ceilings` key in the settings response. The history allocation is
not an extra disk reservation. Retention starts when the
revision is retired. The earliest applicable age, count or global budget limit
wins. Reducing retention can release older versions; increasing it does not revive
versions already released. Current revisions are not candidates for history
collection. Transfer drain deadlines and active bounded read leases can delay
physical removal; the history UI stops offering released files immediately.

Expired publication cleanup removes all its revision files. Record retention can
keep the history ledger with unavailable entries. Deleting the publication record
cascades to its history. Operator backup includes all retained revision files and
the history ledger. Ordinary export remains scoped to its explicitly authorized
current revision; the history download API requires owner access. Guests can receive
the original files of an explicitly shared version through its authorized content
origin, under the material's current policy.

Restoration copies a validated saved manifest into a reserved upload and uses the
normal compare-and-swap commit. It requires the expected current revision and an
authorization callback rechecked inside that commit transaction. It creates a new
revision ID/origin, preserving current access, password, expiry and descriptive
metadata. Source reads hold a bounded lease; bytes and digests are verified while
copying. Storage limits, concurrent preparation limits and deadlines still apply.
A site restore reserves the expanded content size without constructing a duplicate
ZIP archive. A failed or concurrently stale restore cannot replace the current
revision.

Follow the [schema upgrade and recovery procedure](STATISTICS.md#schema-upgrade-and-recovery)
when changing server versions.
