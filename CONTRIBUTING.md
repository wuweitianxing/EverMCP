# Contributing to EverMCP

Thanks for your interest in contributing! This document covers the basics.

## Development setup

```bash
git clone <repo-url>
cd EverMCP
pip install -e ".[dev]"
```

This installs the framework plus `pytest`, `ruff`, and `mypy`.

Python 3.11+ is required (uses `datetime.UTC`, `tomllib`, PEP 695 generics).

## Running checks

```bash
# Tests
pytest -q

# Lint (must be clean before commit)
ruff check evermcp/ tests/
ruff format --check evermcp/ tests/

# Type check (best-effort — not all modules are fully annotated yet)
mypy evermcp/
```

CI runs `pytest`, `ruff check`, and `ruff format --check` on every push and
pull request (Python 3.11 / 3.12 / 3.13 matrix). Make sure they pass locally
before opening a PR.

## Project layout

```
evermcp/
├── core/          @tool decorator, Capability model, providers, registry
├── protocol/      Coordinator, MCP server (stdio+HTTP), WS channel, REST API
├── security/      SafePath, SafeURL, Config, auth
├── web/           FastAPI Web UI (node tree + form declarations)
├── connect/       stdio-WS bridge (evermcp-connect)
└── cli.py         CLI entry point
tools/             your tools (gitignored — point --tools-dir here)
examples/tools/    2 reference tools (shipped)
tests/             unit / integration / e2e / security / registry / worker
docs/              gateway plan, adding-tools spec, stage reviews
```

## Writing tools

Tools live under `tools/<category>/<name>.py` and use the `@tool` decorator.
See [`docs/adding-tools.md`](docs/adding-tools.md) for the full spec and
[`examples/tools/`](examples/tools/) for copy-and-go references.

Key rules:
- Touch the filesystem? Use `ctx.safe_path.validate(...)`.
- Touch the network? Use `ctx.safe_url.validate(...)`.
- Spawn subprocesses? Use argv lists — never `shell=True`, never string concat.
- Let exceptions raise — the framework wraps them into typed error envelopes
  (`-32001`..`-32005`).

## Architecture context

- [`docs/gateway-plan.md`](docs/gateway-plan.md) — the roadmap (S0–S3 stages).
- [`docs/reviews/`](docs/reviews/) — stage code reviews and implementation
  summaries (archived).
- [`SECURITY.md`](SECURITY.md) — trust boundaries and the tool-author security
  checklist.

## Pull request checklist

- [ ] Tests pass (`pytest -q`)
- [ ] Lint passes (`ruff check evermcp/ tests/`)
- [ ] Format passes (`ruff format --check evermcp/ tests/`)
- [ ] New tools have unit tests covering the happy path + at least one
      security-rejection path
- [ ] No secrets / API keys / `.env` files committed
- [ ] `CHANGELOG.md` updated if user-facing behavior changed

## Commit style

No strict convention — write a clear, concise commit message that describes
*what* changed and *why*. Reference the relevant stage (`S0`/`S1`/`S2`) or
issue number if applicable.

## Reporting issues

Open a GitHub issue. For security-sensitive reports, see
[`SECURITY.md`](SECURITY.md).
