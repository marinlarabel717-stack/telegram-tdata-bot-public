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


## web直连

Click **web直连** in the main menu, then privately upload a single `.session`
or a ZIP containing sessions (up to 200 accounts, 100MB upload, 300MB declared
uncompressed size). Existing access checks and proxy mode apply.
The bot uses the supplied script's `messages.RequestUrlAuth` flow and only accepts
an official Telegram Web URL from `UrlAuthResultAccepted`.
It does not log out the original session or change 2FA.

Each upload returns exactly one UTF-8 TXT file privately to the uploading user,
after all accounts have been processed (100 accounts still means one file).
Internal bounded concurrency does not split the output. Each entry has its source
filename and either a login link or a failure reason. Copy the **entire** link to
the browser address bar; links are short-lived and may need regeneration.
Only use sessions you are authorized to access. Login URLs are bearer credentials:
results are not logged or retained by the bot after sending, and temporary uploads
are cleaned when the task exits. Telegram retains the delivered message/file.
Mocked tests do not prove live token issuance or browser login acceptance.


### Session schema compatibility

Web direct reads named Telethon auth fields from SQLite in read-only mode, then
uses a `MemorySession`. Extra columns and differing session schema versions do
not trigger Telethon's positional SQLite unpacking or migration. The uploaded
SQLite file is never opened by the Telegram client or modified. Pyrogram schemas,
missing auth keys and malformed SQLite files receive explicit private errors.
Other failures identify the stage and exception class without exposing exception
messages that may contain bearer links. Offline fixtures cover old/new schemas;
actual user sessions and Telegram Web login still need live acceptance testing.


### Web direct login failure handling

Web direct login is conditional on Telegram returning an accepted URL with a
valid Web auth token. It is not guaranteed for every authorized session.
Unauthorized sessions require a fresh authorized export; the bot does not relogin
or modify the original file. Flood errors (including generic RPC 420) stop further
URL attempts for that account immediately, report an available wait time, and
never trigger an automatic retry. Unknown wait times are not guessed. Batch
concurrency is two; every upload still returns exactly one consolidated TXT.
Default/request/accepted-without-token results are distinguished in the report
without logging their URLs or raw exception text. An interactive authorization
request is reported, not automatically approved. Tests use offline sessions and
mocked Telegram responses; live token generation/browser login remains unverified.
