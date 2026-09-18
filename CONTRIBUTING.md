# Contributing

Thanks for looking. Two kinds of contributions matter most here.

## Strategy plugins

1. Add the strategy under `app/strategies/` (subclass `BaseStrategy`).
2. Register it in `app/strategies/registry.py` with **all** metadata filled in: execution status, hold horizon, risk unit, capital lock-up class, and the validation gates it must pass to graduate from shadow to paper. A strategy without gates will not be merged.
3. Start it as `shadow`. Paper status needs numbers from a lab report.
4. Add a test under `tests/`.

## Lab reports

1. Put the script under `scripts/` and the report under `labs/YYYY-MM-DD-slug/` (see `labs/README.md` for the layout).
2. State the data window, the filters and the losing cases. A report that only shows winners will be sent back.
3. Keep the script runnable against a local SQLite database placed under `data/`.

## Ground rules

- Live trading stays hard-blocked. Pull requests that remove the guard will be closed.
- No secrets in the repo. `.env` is git-ignored; add new settings to `app/config.py` and `.env.example`.
- Run `pytest -q` before opening a pull request.
