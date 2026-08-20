# Contributing

Contributions are welcome through GitHub issues and pull requests.

```bash
uv sync --extra desktop --group dev
uv run pytest -q
```

Keep conversion behavior fail-closed: an output that fails post-write QA must
never be presented as a valid MCAP. New file grammars, schemas, or timing rules
must include regression tests and user-facing validation messages.
