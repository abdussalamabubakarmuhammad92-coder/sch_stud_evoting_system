# Credential Rotation Checklist

The original private project archive contained an `.env` file and its Git history includes that file. The public release therefore treats every historical credential as potentially exposed.

## Required external actions

Rotate/revoke any real credentials that were ever stored in the original `.env`, including:

- Django `DJANGO_SECRET_KEY`
- PostgreSQL password/user credentials
- Redis credentials/connection URL if authenticated
- SMTP account password/API credential
- SMS provider API key
- Any other third-party provider secrets

These provider-side rotations cannot be performed from this repository because they require access to the respective service accounts.

## Public repository safeguards

- `.env` is absent.
- `.env` is ignored by Git.
- `.env.example` contains placeholders only.
- The sanitized release has a fresh Git history with no historical `.env`.
- Uploaded real-world media and logs are excluded.
