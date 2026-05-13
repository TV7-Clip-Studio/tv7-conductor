# TV7 Conductor Bootstrap

## Prerequisites
- Python 3.11+
- Git
- Optional: `gh`, `ao`, `codex`, and `gemini` depending on the provider paths you plan to use.

## Local Setup
```bash
cd /Users/joaquinastelarra/Documents/TV7/tv7-conductor
make venv
make install-dev
make conductor-doctor
make conductor-test
```

## Web Dashboard Dependencies
The FastAPI dashboard stack is optional until the web interface is introduced:

```bash
make install-web
```

This installs the package with the `web` optional dependencies declared in `pyproject.toml`.

## First Run
```bash
make conductor-init
make conductor-status
```

For a local-only validation of the conductor loop:

```bash
PYTHONPATH=tooling python3 -m unittest tooling/tests/orchestrator/test_fixture_e2e_scenario.py
```

## Diagnostics
`make conductor-doctor` checks:
- Python version
- core/workspace/conductor/state paths
- workspace and provider config files
- required `git`
- optional `gh`, `ao`, `codex`, `gemini`
- optional web modules: `fastapi`, `jinja2`, `sqlmodel`, `uvicorn`
