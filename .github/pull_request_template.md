## Summary

<!-- What the code does now, and why it changed. -->

## Checks

Run from the checkout, as in [CONTRIBUTING.md](../CONTRIBUTING.md#checks):

- [ ] `uv run ruff check src tests examples scripts` and `uv run ruff format --check src tests examples scripts`
- [ ] `uv run ty check src tests`
- [ ] `uv run python scripts/check_architecture.py`
- [ ] `uv run pytest -q -m unit`, or the tests that cover the change
- [ ] For changes under `js/`: `npm run test:coverage --prefix js` and `npm run test:browser --prefix js`, and the Speed gates workflow passes

## Docs

- [ ] User-facing changes are reflected in `docs/`, or none are needed
