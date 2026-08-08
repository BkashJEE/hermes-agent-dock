# Contributing to Hermes Agent Dock

Thanks for helping improve Hermes Agent Dock. Bug reports, feature suggestions, documentation improvements, tests, and focused pull requests are welcome.

## Before opening an issue

1. Search existing issues to avoid duplicates.
2. Confirm the behavior against the latest `main` branch or newest release.
3. Remove credentials, private conversations, profile names, session titles, local paths, account data, and unrelated notifications from logs and screenshots.
4. For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

Use the repository's **Bug report** template for reproducible defects and **Feature suggestion** template for product ideas.

## Pull-request workflow

1. Fork the repository and create a focused branch from current `main`.
2. Make the smallest change that solves the stated problem. Avoid unrelated refactors or formatting churn.
3. Preserve the boundaries documented in [PRODUCT_SPEC.md](PRODUCT_SPEC.md) and [SECURITY.md](SECURITY.md).
4. Add or update tests for behavior changes.
5. Run the verification commands below.
6. Open a pull request using the provided template, including the goal, implementation summary, evidence, risks, and screenshots where relevant.

Do not move existing release tags or change version metadata unless the maintainers explicitly request a release change.

## Verification

```bash
node --input-type=module --check < plugin.js
node --test tests/test_dock_state.mjs
python -m unittest discover -s tests -v
python -m py_compile backend/dashboard/plugin_api.py backend/dashboard/dock_runner.py install.py uninstall.py
git diff --check
```

For installation changes, also test repeat install, rollback, and reversible uninstall behavior. For UI changes, verify floating and docked modes, reduced motion, compact sizing, and privacy-safe screenshots.

## Security and privacy requirements

Pull requests must not include:

- passwords, tokens, API keys, cookies, certificates, or credentials;
- private chat content, session titles, prompts, profile names, avatars, account information, or personal paths;
- local databases, telemetry exports, caches, generated logs, or unrelated application state;
- hidden network calls, telemetry, broad filesystem scanning, or package-manager lifecycle scripts.

Agent Dock uses Hermes's configured profiles and official profile metadata inventory. Contributions must not read profile conversations, memories, credentials, or arbitrary files to discover agents.

## AI-assisted contributions

AI-assisted contributions are welcome when the contributor has inspected the resulting diff, understands the change, removes private data, runs the required checks, and can respond to review feedback. Generated output is not a substitute for verification.

## Review expectations

Maintainers may ask for a smaller scope, additional tests, privacy-safe evidence, or changes that preserve compatibility with Hermes Desktop's public plugin contracts. A pull request is not considered complete until required checks pass and review findings are resolved.
