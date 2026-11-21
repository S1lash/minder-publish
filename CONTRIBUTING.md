# Contributing to Minder Publish

Minder Publish is a self-hosted publisher for ordinary files and built static sites.
Its owner cabinet, CLI and optional MCP adapter share the same versioned API.

Read [the architecture](docs/ARCHITECTURE.md) and
[security acceptance requirements](docs/SECURITY.md#release-acceptance) before
changing a serving, storage or authentication boundary. Tests must exercise the
actual boundary each change protects.

## Local development

Use [the supported environments](README.md#supported-environments) for validation.
Docker Compose is the self-hosting path. To run the full test suite with the locked
wheels, use Python 3.13 or 3.14 on Linux with glibc or on Apple Silicon macOS.
On Intel macOS, use a Linux environment for the server suite. Native CLI support
follows the environments listed in the README.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q tests
python -m build --no-isolation
python scripts/normalize-sdist.py dist/minder_publish-*.tar.gz
python scripts/build-client-wheel.py
python scripts/check-release.py --source-root . dist/minder_publish-*.whl dist/minder_publish-*.tar.gz
python scripts/check-release.py --source-root . --client-wheel dist/client/minder_publish-*.whl
```

Use the wheel in `dist/client/` for anonymous instance downloads. The wheel in
`dist/` contains the server and is used by private installations.

Run server integration tests in Linux and client tests on the supported platforms.
For deployment changes, build the image and run the container smoke test.

```sh
docker build -t minder-publish:local .
python scripts/smoke-container.py --image minder-publish:local
```

Use temporary SQLite databases and volumes for tests of quotas, crash recovery,
migration and auth. Use synthetic files and credentials. Test browser origin
isolation and cookie behavior in a browser. Dependency changes require a clean
resolution of the relevant lock files and a run of the affected checks. Preserve
SHA-256 hashes for the approved wheels on the supported Python and OS combinations.
Verify both Linux image architectures when changing runtime dependencies.

## Release source verification

From a clean checkout of the intended release commit, verify the Git source
export alongside the built packages. The source check compares paths and files
against the specified Git tree. Normalize the source distribution after building
to remove local user and group information from its archive headers. Audit the
normalized archive before calculating release checksums.

```sh
mkdir -p dist
git archive --format=tar --prefix=minder-publish/ HEAD | gzip -n > dist/minder-publish-source.tar.gz
python scripts/check-release.py dist/minder-publish-source.tar.gz --source-root . --git-tree HEAD
```

## Issues and platform ports

Open an issue before changing a public API, a deployment requirement or the data
format. Include the user scenario and discuss compatibility with existing
installations. Bug reports should include the version, environment, steps, expected
and actual behavior, and a redacted error or request ID when available. Follow
[private reporting](docs/SECURITY.md) for security issues.

Community ports can be maintained separately. Discuss support in this project before
adding an operating system, installer or CI job. A supported port needs an owner,
installation and upgrade instructions, and a verifiable test path.

## Brand assets

The editable [Open Corner SVG](docs/brand/open-corner.svg) and
[theme CSS](src/minder_publish/server/web/theme.css) are inputs to the
[asset generator](scripts/render-brand.py). Run `python scripts/render-brand.py`
with `rsvg-convert` on PATH after changing the mark or palette. The application
ships the rendered assets for light and dark surfaces and a square link-preview
fallback.

## Changes and reviews

- Make one cohesive change with a concrete before/after behavior and relevant tests.
- Preserve sparse-patch semantics and immutable revisions; failed uploads must leave
  the current revision and access policy intact.
- Keep account powers out of device tokens and remote MCP. Keep filesystem access
  out of the remote tool registry.
- Escape user metadata and keep author HTML off the control origin. UI text belongs
  in `src/minder_publish/server/web/messages.json`, with EN/RU/ES/FR translations.
- Preserve OFL notices beside bundled fonts. The application code is licensed under
  [MIT](LICENSE); third-party assets retain their own licenses.
- Never include `.env`, tokens, cookies, password hashes, data directories, TLS keys,
  personal infrastructure paths, logs with query strings, or private deployment files.

Write the pull request in English. Describe the problem, resulting behavior,
compatibility impact and executed validation, and identify any check you could not
run. Update the affected docs and translations alongside the code. Use screenshots
for visible changes and reproducible checks for backend behavior.
