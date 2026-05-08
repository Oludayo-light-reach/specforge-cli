# Agent Instructions

When the user asks to capture the current Codex chat into Spec, including
phrases like `/capture`, `capture this chat`, or `send this chat to Spec`, run:

```bash
spec codex capture --index 1
```

If the local checkout is being used directly from source before the `spec`
console script is installed, use:

```bash
PYTHONPATH=src python3 -m spec_cli.cli codex capture --index 1
```

Use `--dry-run` first when the user asks to preview what would be captured.
