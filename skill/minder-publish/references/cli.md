# Local CLI

Use the installed `minder-publish` in the agent's actual execution environment.
Run `minder-publish doctor --brief --json` to check the selected instance and its
effective defaults; omit `--brief` only when full capabilities are needed. Missing setup
belongs in the product's Connection flow; do not request a device token in chat.
Use `--instance NAME` when a specific configured instance was selected.

```text
minder-publish preflight ./report.pdf --kind file --json
minder-publish publish ./report.pdf --kind file --title "Report" --summary "Findings and next steps" --access link --expiry default --json
minder-publish preflight ./dist --kind site --entry index.html --json
minder-publish publish ./dist --kind site --entry index.html --access link --expiry default --json
minder-publish list --query "Report" --json
minder-publish info MATERIAL_ID --json
minder-publish update MATERIAL_ID ./report.pdf --kind file --change-summary "Updated findings" --json
```

Review the included/excluded manifest before publishing. Pass the preflight's
`manifest_sha256` as `--expect-manifest HASH` on publish or update to require the
same reviewed content. A mismatch stops before upload: inspect the changed
manifest instead of silently dropping the guard. A separate preflight does not
freeze the next operation; each prepares a private, bounded temporary snapshot.
The digest covers content and entry selection, not access or expiry policy.

Dependency diagnostics describe supported static HTML/CSS references.
`static_scan_complete` says whether that scan finished; `missing_assets` and
`external_origins` are findings. `complete=false` retains the limitation that
JavaScript and runtime behavior are not analyzed. Empty findings do not prove
the site works offline or has no dynamic dependencies. Review missing assets;
choose external-resource policy deliberately when the result needs them.

Use the instance's discovered expiry default, not a fixed number of days.
Return the committed `expires_at` (or explicitly no expiry), because the server
resolves the deadline when publishing. File cleanup is a separate policy.

Read JSON `data` on success and `error` on failure. Follow the reported
remediation; a quota conflict, stale revision, expired credential or transport
failure is not success. For `unknown_outcome`, use its `actual.upload_id` with
`minder-publish upload-status UPLOAD_ID --json` before retrying. A committed
status proves the upload committed; inspect the material separately for current
availability. A title search cannot establish the outcome. If it remains unknown,
report that uncertainty and stop; do not create another publication.

For publication passwords, use the CLI's hidden prompt or private file/stdin
without exposing its value to the model. If the environment cannot supply the
secret privately, let the owner set it in the cabinet. Credentials come from
the private CLI profile, never command arguments or this skill.

For existing materials, `set ID` changes only supplied metadata/access/expiry;
`pause ID` and `resume ID` control access, `extend ID 7d` adds that duration,
and `expire ID` starts the saved cleanup policy. `delete ID --yes` permanently
deletes the identified material after explicit authorization. Read each command's
help for its flags; no full API manual is needed for these operations.

`publish --key NAME` chooses the initial link name at creation.

In `info`, `purge_mode` is policy, while `content_state` is current file state.
`purge_reason=unknown` means the historical cause was not recorded; do not infer
it from today's policy. `missing_on_import_at` records discovery during import,
not original deletion time. A retained purged card permits re-upload with an
explicit expiry choice; it cannot recover removed bytes.
