Full instructions: [AGENTS.md](AGENTS.md). Read it before changing anything.

The three that cost the most when skipped:

1. **Verify against a running Dify.** There is a checkout at `../dify-oss`;
   its controllers are the specification. Mocking the server and calling it
   verified is how this package accumulated ~1,100 lines of endpoints Dify has
   never served, and seventeen methods pointing console paths at `/v1`.
2. **`uv run pytest -m "not live"`** must pass, as must ruff, black, isort and
   mypy. Bare `pytest` is not on PATH.
3. **Never collapse two states into one.** A node and one execution of it;
   importing and publishing; a paused run and a failed one; absent and
   not-determined. Each was a bug. AGENTS.md has the table.
