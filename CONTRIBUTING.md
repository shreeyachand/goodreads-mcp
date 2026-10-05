# Contributing

Thanks for helping improve goodreads-mcp! A few ground rules to streamline PR review:

- **Request review** from @shreeyachand so I get notified that you opened a PR!
- **On AI-generated code**: Use whatever tools you like, what I care about is that you understand and can defend the result!

- **Justify your design decisions.** Explain *why* you chose an approach in the PR description — especially when you're adding or changing something non-obvious (a custom implementation, a new dependency, a caching layer, retry behavior). "It works" isn't enough; say why this way over the alternatives.
- **Prefer well-tested libraries** over custom implementations unless there's a concrete reason not to. If you do write it yourself, say what edge cases it handles.
- **Keep changes reviewable.** Split unrelated work into separate PRs. If a PR grows beyond one coherent feature, expect to split it.
- **Test behavior, not libraries.** Tests should cover your integration and error handling, not re-verify a dependency's internals.
- Verify in a real MCP client (e.g. Claude) before requesting review, and note anything you couldn't reproduce.

## Versioning and releases

The `Release MCPB` GitHub Action packs and publishes a new `.mcpb` whenever the project version changes on `main`.

- **Significant changes must bump the version.** Update `version` in **both** `pyproject.toml` and `manifest.json` (they must match — the workflow fails if they don't). Use [semver](https://semver.org): patch for fixes, minor for new tools/features.
- If you don't bump the version, no release is cut. Small docs/CI/typo PRs can skip it.

## PR titles

Prefix titles with a conventional-commit type: `feat:`, `fix:`, `chore:`, or `ci/cd:`. Write the rest as a plain description of what the PR does (e.g. `fix: list_shelves returns [] for everyone`).

## Before you open a PR

- Run the tests: `pytest -q`
- Confirm the version bump (when applicable) is present in both files.
