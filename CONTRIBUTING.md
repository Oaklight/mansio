# Contributing to mansio

## Repository layout

This repository ships **three** distributions under `packages/`, plus a
root `pyproject.toml` that contains tooling configuration only:

| Path | Distribution | What it is |
| --- | --- | --- |
| `packages/mansio-core/` | `mansio-core` | Types, protocols, bus, backends, authorization, token store, metrics |
| `packages/mansio-web/` | `mansio` | HTTP/IRC frontends, admin panel, server orchestration, `serve` CLI. Import roots: `mansio_web` and the temporary compatibility alias `mansio` (kept for one release, then removed) |
| `packages/mansio-client/` | `mansio-client` | The client SDK that agents install to talk to a server |

The three distributions are published independently. `mansio` and
`mansio-client` both depend on `mansio-core` with a same-minor window, so a
core patch release does not require a new server or client release.

## Development setup

Install all three packages in editable mode:

```bash
pip install -e packages/mansio-core
pip install -e packages/mansio-client
pip install -e "packages/mansio-web[dev,test,irc]"
```

Then install the git hooks:

```bash
pre-commit install
```

## Running tests

The suites run separately, because CI runs them in different environments:

```bash
python -m pytest packages/mansio-core/tests/    # core suite
python -m pytest packages/mansio-web/tests/     # server/frontend/admin suite
python -m pytest packages/mansio-client/tests/  # client suite
```

Some tests need things that may not be present on your machine and skip
themselves when they are missing — the NATS backend tests need a `nats-server`
binary on `PATH`, and the IRC frontend tests need the `irc` extra. A skipped
test is not a passing test; if you are changing one of those areas, install what
it needs and confirm the tests actually ran.

## Dependency rules

The split follows three rules, checked by
`scripts/check_import_boundaries.py` in CI:

1. `mansio_core` imports neither `mansio_web` nor `mansio_client`.
2. `mansio_web` imports `mansio_core`, never `mansio_client`.
3. `mansio_client` imports `mansio_core`, never `mansio_web`.

No third-party runtime dependencies. `mansio` and `mansio-client` declare
exactly one runtime dependency, the first-party `mansio-core` distribution;
everything else is stdlib or vendored. Test-only dependencies are allowed. Each
package has its own `_vendor/` directory and `zerodep.lock.json`.

The compatibility package `packages/mansio-web/src/mansio/` is the one
exception: it routes the old `mansio.*` import paths to the new packages. Do not
add implementation code there.

## Release order

`mansio-core` is a first-party dependency of both `mansio` and `mansio-client`.
Publish `mansio-core` first, then `mansio` and `mansio-client`. A server-only or
client-only release run is valid only when the matching `mansio-core` version is
already available on the target index (PyPI or TestPyPI).

## The client must work without the server installed

`mansio-client` is installed on its own by people running agents against a
server elsewhere. They have no reason to install `mansio`, so the client
package must be usable — and testable — with the server absent.

Concretely: **nothing under `packages/mansio-client/` may import
`mansio_web`.** Only `mansio_client`, `mansio_core` and the standard library.

This is easy to violate without noticing, because a normal development
environment has all three packages installed, so a client test that starts a
real server passes locally. CI catches it in the `client-check` job, which
installs only core and the client:

```bash
pip install -e packages/mansio-core
pip install -e packages/mansio-client
pip install pytest
python -m pytest packages/mansio-client/tests/
```

To exercise client behavior that would otherwise need a server, either drive
`HttpTransport` against a `http.server` stub from the standard library, or
substitute a fake transport into `MansioClient`.

To check your work the way CI does, run the client suite in a throwaway
environment with only core and the client installed:

```bash
python -m venv /tmp/client-only
/tmp/client-only/bin/pip install ./packages/mansio-core ./packages/mansio-client pytest
/tmp/client-only/bin/python -m pytest packages/mansio-client/tests/
```

## Checks that run on a pull request

| Job | What it does |
| --- | --- |
| `boundaries` | Runs `scripts/check_import_boundaries.py` |
| `lint` | Runs the pre-commit hooks: `ruff` (lint and format), `ty` type check, `complexipy` complexity limit |
| `test-core` | Runs the core suite with only `mansio-core` installed |
| `test-web` | Runs the server/frontend/admin suite with core, client and web installed |
| `client-check` | Installs core and the client alone, imports it, runs both CLI entry points and the client suite, and builds all three packages |

Running `pre-commit run --all-files` locally covers everything the `lint` job
does.
