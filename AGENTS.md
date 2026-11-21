# Minder Publish — contributor and agent instructions

Minder Publish is a self-hosted, single-owner publication service. Each instance
owns its account, SQLite database and immutable files.

## Code and commands

`src/minder_publish/` owns the distribution. `client/` supports Python 3.10+ on
Linux and macOS. The server uses the pinned Linux container environment. See
[supported environments](README.md#supported-environments). `server/web/` owns the
interface and its four-language catalog.

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q tests
python -m build --no-isolation
python scripts/normalize-sdist.py dist/minder_publish-*.tar.gz
```

Run tests against real temporary SQLite and directories. Browser checks use an
isolated instance at `http://publish.localhost:8080`. Never use production data as
a test fixture. Deployment commands and TLS ownership are in
[self-hosting](docs/SELF_HOSTING.md); deployment is an explicit operator action.

## Invariants and canonical homes

- [API contract](docs/implementation/API.md) owns transport semantics;
  [architecture](docs/ARCHITECTURE.md) explains module boundaries.
- `defaults.json` owns defaults, ceilings and operation budgets. UI and clients
  discover effective limits from the server; no duplicated policy constants.
- SQLite owns metadata, pointers, reservations, leases and hashed credentials.
  Revisions are immutable. A failed or stale upload cannot replace the current one.
- Access expiry, file deletion and metadata retention are independent. Sparse
  updates preserve omitted fields; private-to-external requires an expiry choice.
- Authorization precedes body parsing and every byte-serving response. Uploaded
  content runs only on a distinct revision origin, never the control origin.
- One ASGI worker and a lifetime instance lock protect offline operations. Hold
  leases through complete transfers and await thumbnail workers before shutdown.
- Device tokens read/manage materials only. Settings, account and token management
  require owner sessions. Browser mutations require exact Origin and session CSRF.
- Passwords are access gates; stored files and backups remain plaintext. Read
  [security](docs/SECURITY.md) before changing serving, archives, auth or lifecycle.
- Only local MCP registers filesystem tools. Remote tools use caller-scoped grants
  and the same API checks through a private ASGI bridge; OAuth MCP tokens are not
  accepted by the external HTTP API. No privileged shared server identity exists.
- Remote host-file input is opt-in for exact operator-approved public HTTPS sources;
  reserve upload space before streaming and never spool outside accounted storage.
- Skills have one source at `skill/minder-publish/SKILL.md`; wheel/install adapters
  consume it. Preferences are allowlisted cookies, never authentication state.

Legacy import and API compatibility belong to `src/minder_publish/server/` and
their synthetic fixtures in `tests/`. Import reads a consistent source copy into a
separate data root; never run a source instance's sweeper against the new root.
