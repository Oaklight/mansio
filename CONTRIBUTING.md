# Contributing to mansio

## Repository layout

This repository ships **two** packages, and there is no `pyproject.toml` at the
repository root:

| Path | Package | What it is |
| --- | --- | --- |
| `mansio/` | `mansio` | The server: backends, bus, frontends, CLI, admin panel, MCP server |
| `mansio-client/` | `mansio-client` | The client SDK that agents install to talk to a server |
| `shared/` | — | Source of files that both packages need; **generated copies live in each package** |

The two packages are published independently. That has consequences for how you
write and test code, described below.

## Development setup

Install both packages in editable mode:

```bash
pip install -e "mansio[dev,test,irc]"
pip install -e "mansio-client/"
```

Then install the git hooks:

```bash
pre-commit install
```

## Running tests

The two test suites run separately, because CI runs them in different
environments:

```bash
python -m pytest mansio/tests/         # server suite
python -m pytest mansio-client/tests/  # client suite
```

Some tests need things that may not be present on your machine and skip
themselves when they are missing — the NATS backend tests need a `nats-server`
binary on `PATH`, and the IRC frontend tests need the `irc` extra. A skipped
test is not a passing test; if you are changing one of those areas, install what
it needs and confirm the tests actually ran.

## `shared/` is generated into both packages

`shared/` is the source. A pre-commit hook (and the `sync-check` CI job) copies
each file into both packages, substituting the `__PKG__` placeholder with the
importing package's name. For example `shared/transport.py` becomes
`mansio/src/mansio/transport_http.py` and
`mansio-client/src/mansio_client/transport.py`.

**Edit `shared/` only.** Hand-editing a generated copy will be overwritten by
the hook, and `sync-check` fails the build when a copy does not match what the
script produces. To regenerate by hand:

```bash
bash scripts/sync-shared.sh
git diff   # must be empty afterwards
```

## The client must work without the server installed

`mansio-client` is installed on its own by people running agents against a
server elsewhere. They have no reason to install `mansio`, so the client package
must be usable — and testable — with the server absent.

Concretely: **nothing under `mansio-client/tests/` may import `mansio`.** Only
`mansio_client` and the standard library.

This is easy to violate without noticing, because a normal development
environment has both packages installed, so a client test that starts a real
server passes locally. CI catches it in the `client-check` job, which installs
only the client:

```bash
pip install mansio-client/    # note: mansio is NOT installed
pip install pytest
python -m pytest mansio-client/tests/
```

To exercise client behavior that would otherwise need a server, either drive
`HttpTransport` against a `http.server` stub from the standard library, or
substitute a fake transport into `MansioClient`.

To check your work the way CI does, run the client suite in a throwaway
environment with only the client installed:

```bash
python -m venv /tmp/client-only
/tmp/client-only/bin/pip install ./mansio-client pytest
/tmp/client-only/bin/python -m pytest mansio-client/tests/
```

## Checks that run on a pull request

| Job | What it does |
| --- | --- |
| `lint` | Runs the pre-commit hooks: shared-file sync, `ruff` (lint and format), `ty` type check, `complexipy` complexity limit on `mansio/src/mansio/` |
| `sync-check` | Regenerates `shared/` copies and fails if anything differs |
| `test` | Runs the server suite |
| `client-check` | Installs the client alone, imports it, runs its CLI and test suite, and builds the package |

Running `pre-commit run --all-files` locally covers everything the `lint` job
does.
