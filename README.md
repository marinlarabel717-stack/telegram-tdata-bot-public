# Telegram TData Bot Public

A public, cleaned-up snapshot of a Telegram account utility bot focused on TData / session workflows.

## Included
- Telegram bot entrypoint (`tdata.py`)
- Login API service (`login_api.py`)
- Account classification helpers
- TRON payment helper module
- i18n resources
- non-sensitive device parameter templates

## Not Included
This public repo intentionally excludes private runtime data such as:
- `.env`
- local databases
- proxy lists
- session files
- cached outputs
- private API credential bundles

## Quick start
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python tdata.py
```

## Notes
- This project currently ships as a large single-file main bot and may need refactoring before production reuse.
- Review configuration values carefully before running.
- Keep your own API keys, bot token, proxy lists, and wallet settings private.
