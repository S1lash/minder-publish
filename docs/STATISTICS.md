# Opening statistics

Statistics separate accepted **guest openings** from **your signed-in openings**.
They do not measure unique people, completed reading, time spent, or every HTTP
request. Direct reloads on a revision host do not create a new opening.

Your ordinary owner openings, cabinet previews, retained-history views and clean
view count in **You**. The owner workspace shell does not count separately from
its document iframe. An accepted guest opening of an explicitly shared version
contributes to the material's guest aggregate. The explicit **Check guest view**
action (`guest=1`) contributes to neither audience.

Classification uses the server-authenticated owner session when the opening
intent is issued, with owner-session revalidation in the consumption transaction.
If an owner must enter a material password, the live owner session still determines
the owner audience. The service does not infer identity from a URL parameter,
address, browser or device. A signed-out or independent incognito browser counts
as a guest, even when the person is the owner.

The count commits atomically when a one-use opening authorization is consumed and
its target exists, including a valid static-site fallback. Reusing a matching reader
grant consumes the opening intent; the full browser handoff consumes its code.
Replaying either authorization cannot increase either count again. Recognized link
crawlers, HEAD/Range/conditional requests, explicit resource fetches, site asset
paths, declared prefetches and missing targets are excluded for both audiences.
A successful password form submission counts after its authenticated handoff,
under the same audience rule. An unrecognized automated client that completes the
normal browser protocol may still count.

Each audience has at most one lifetime aggregate per material and bounded daily
aggregates using UTC dates. Retention and the **combined** daily-row ceiling are
configured in `defaults.json` under `operations.stats_retention_days` and
`operations.stats_daily_row_limit`. Cleanup runs with lifecycle maintenance as well
as collection. Existing retained guest rows are not evicted to admit owner rows.
At the ceiling, totals continue and the affected audience's `incomplete` signals
missing daily detail. No IP address, visitor identifier, fingerprint, referrer or raw
event log is stored by the statistics subsystem. Access-control cookies continue
to serve their existing authentication purpose.

Statistics are owner-only. The library shows the sum of Guests and You on each
card and exposes the breakdown on demand. The material's Overview shows the two
totals together. Both audiences appear on one chart in the Statistics tab and
the owner drawer, with separate audience views, daily counts and collection
controls. Audience changes use the loaded snapshot. The selected 7-, 30- or
90-day period persists across reopening the drawer. The library fetches both
totals in one bounded query for its current page.
Changing the interface language redraws loaded counts without fetching them again.
Guests receive no analytics widget, script, beacon or statistics notice.

`guest_counts` pauses/resumes guest collection; `owner_counts` independently controls
your collection. Both are material policies with instance defaults; the owner
setting defaults to enabled. Pausing retains earlier counts, visible in the library
and drawer. Clearing requires explicit confirmation and recent owner authentication
and clears both audiences and the legacy counter together. It starts a fresh owner
measurement interval. Removing the material record removes both aggregates.

Migration preserves the earlier counter separately as `legacy_total`; it does not
fabricate daily history or treat older counting rules as the guest metric. The
compatibility `view_count` continues to include the earlier value and accepted new
guest openings; owner openings do not increment it. Owner collection begins with
schema 4 or material creation, whichever is later. Earlier owner openings cannot
be reconstructed. The API returns `opens:null` for owner days before collection,
outside retained daily history, or with unavailable daily detail; the interface
shows **Not collected**. A measured owner total of zero refers only to its stated
collection interval.

## Schema upgrade and recovery

Starting the server upgrades a supported older core schema in one SQLite
transaction. Authentication's separate schema and credentials are unchanged. The
backup snapshot includes both audiences and their collection boundaries. Ordinary
restore retains statistics and material bytes and applies its documented credential
revocation; see [self-hosting](SELF_HOSTING.md#backup-and-restore).

For an offline rollback to a compatible **schema-3** release, stop the service and
other writers, keep the exact previous image, and take a consistent backup outside
the data volume. Using the current executable against that stopped data root, run:

```sh
minder-publish-server --data-dir /path/to/data schema-downgrade --target 3
```

The command holds the same exclusive instance lock as the server. It accepts
core schemas 4–6 with an intact database and refuses the rollback if registered custom
addresses or configured messenger preview policy would be lost. It retains the independent owner extension tables, material metadata,
immutable revision references, guest/legacy statistics and authentication data.
It changes the core schema marker to 3; it does not restore an older snapshot over
newer materials. Do not start newer software between this command and starting
the selected schema-3 release: initialization would upgrade again. A release that
requires schema 2 or older needs its matching backup restored into a separate volume.

While schema-3 software runs, owner collection is unavailable. Re-upgrading retains
measured owner totals and exposes `collection_interrupted:true` for measurement
intervals that may span that period. This is a possible collection gap, not evidence
that any opening happened. Missing daily detail in those intervals is unknown.
Clearing statistics after re-upgrade starts a fresh interval without that gap.
Guest collection continues under the selected release's counting rules. Material
record deletion still cascades into the retained owner tables.

Rehearse rollback with the exact chosen installed release on synthetic data before
using it on an instance. Schema fixtures establish transactional preservation, not
compatibility with an arbitrary historical image. If the rollback release cannot
read the prepared database, keep traffic stopped and restart the matching current software or
restore the matching backup into a separate volume after accounting for later
material changes and deletions. Changing an image alone never downgrades a database.
