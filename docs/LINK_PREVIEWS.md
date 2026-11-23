# Messenger link previews

Stable links with previews enabled expose server-rendered Open Graph and Twitter Card metadata. The
instance's configured HTTPS origin supplies all canonical and image URLs. Material
metadata follows the same public availability rule as the guest entry.

`og:site_name` is `minder publish.`. An available link-access material supplies its
title and optional description. Password/private materials use “Something worth
opening.” and the generic brand description; their titles, summaries, hints,
passwords and contents are not put in previews. Unavailable materials use a neutral
status. Changing access cannot erase an earlier card cached by another service.
An explicitly shared `/p/{id}~{sequence}` version uses that pinned canonical URL
with the material's current guest-visible title and description. Internal revision
change notes stay private. An unshared or unavailable version does not expose its
material metadata through a crawler request.

## Preview controls

**Settings → Links** sets whether addresses using the instance default supply a
messenger card. It starts enabled on existing and new installations and takes
effect immediately for every inheriting address. **Edit link** controls the
primary address and each retained address. An explicitly shared revision has
its own setting beside its version link.

Each address can use the instance default or explicitly keep previews on or off.
When editing the name, keeping the address setting preserves the mode of an
existing destination or carries the current mode to a new destination. A kept
prior address keeps its own mode. These controls affect future card responses.
The address, access rules, indexed-discovery policy and library thumbnail are
independent.

Recognized crawlers receive a 204 response for an active address with previews
off. The ordinary opening and access pages still work and omit social-card tags.
A messenger may choose its own fallback and continue showing a cached card.
Remove that card in the messenger if it already appears in a message.

## Card assets and delivery

An available public static site with a processed [cover](implementation/API.md#site-covers)
gets a 1024 × 1024 JPEG that fits the entire artwork inside safe margins, with a
small Minder Publish mark. The title and summary use preview metadata. The image
uses the selected revision and a credential-free endpoint that rechecks access,
expiry, retention, sharing and the exact address's preview setting on every request. The route draws from the bounded
sanitized raster already generated for the library. An ordinary image URL
without its address name returns 404. Pinned image URLs use their version
sequence.

The fallback is a 1024 × 1024 PNG with the [Open Corner](brand/open-corner.svg) inset
for small square crops. The SVG favicon and 180 × 180 Apple touch icon always use
the brand mark. Titles and summaries remain native preview metadata.
Asset URLs include their content fingerprint so a refreshed card requests the new
image after an upgrade; this cannot force a messenger to refresh the card itself.
X Cards use `summary` with the square image.

| Platform | Published guidance and design consequence |
|---|---|
| [Slack](https://docs.slack.dev/messaging/unfurling-links-in-messages/#classic-link-unfurling) | Classic unfurling reads Open Graph and X Card metadata. No Slack app installation is required for this path. |
| [Telegram](https://core.telegram.org/constructor/webPage) | Its preview model supports title, site name, description and photo; client layout and small/large media decisions vary. A centered graphical mark remains legible in either crop. |
| [Apple Messages](https://developer.apple.com/documentation/technotes/tn3156-create-rich-previews-for-messages) | Serve metadata consistently across user agents, without JavaScript. Images should be at least 900 px wide and square icons at least 108 px. Avoid text in the image and repeated branding in the title. |
| [Microsoft Teams](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/build-and-test/share-to-teams-from-web-apps#craft-your-website-preview) | The documented website-preview flow uses title, description and image through HTML or Open Graph tags; metadata must be reachable without authentication. |
| WhatsApp, Discord, X and other readers | The shared Open Graph/Twitter payload is available without a crawler-specific integration. Exact dimensions, cropping and refresh timing remain client-controlled; no undocumented fixed rendering guarantee is assumed. |

With previews enabled, known preview bots receive a card without starting access negotiation. Other
clients receive the same safe metadata directly at the stable URL, plus an
automatic HTML refresh and a visible continuation link for browsers. Reading the
metadata does not count as a guest opening. Private/password gates expose generic
metadata without unlocking files. Resource fetches keep their existing redirect
behavior. The serving contract is in [API](implementation/API.md).

HTTP tests verify metadata, dimensions, arbitrary configured origins, unknown user
agents, access-state privacy and handoff exclusion. Local rendering checks examine
the mark at favicon and messenger-thumbnail sizes. These checks do not substitute
for a real post in each messenger; services may cache older cards or suppress them
through user/workspace settings. Test a fresh stable URL when checking a new image.
