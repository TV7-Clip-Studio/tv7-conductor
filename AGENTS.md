# TV7 Conductor Agent Notes

- Runtime source-of-truth for Project Conductor is in this repo.
- Shared governance and global roadmap policy stays in `tv7-core`.
- Use deterministic lifecycle stages and avoid ad-hoc state transitions.
- Run tests with:
  - `PYTHONPATH=tooling python3 -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'`
