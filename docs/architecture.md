# Architecture

## Components

### Web layer

Django views receive requests, resolve organization/election context, enforce authentication and role checks, validate forms, and invoke domain utilities.

### Domain utilities

`core/utils.py` contains reusable operations including:

- OTP generation and delivery
- audit logging
- device recognition
- election state transitions
- voter eligibility checks
- vote casting

### Persistence

PostgreSQL is the intended production database. Local development can use SQLite when `DEBUG=True` and no `DATABASE_URL` is supplied.

Major model groups:

- Organization and subscription state
- Users and role-specific profiles
- Election categories, elections, positions, and candidates
- Verified voter records and registered voters
- Anonymous votes
- OTP and recognized-device records
- Audit logs
- Administrative invitation codes

### Background processing

Celery uses Redis as its broker/result backend for scheduled election-transition and subscription-warning tasks.

## Vote request flow

```text
POST /org/<org>/election/<id>/position/<id>/vote/
        │
        ├── authenticate request
        ├── resolve organization/election/position
        ├── verify voter belongs to organization
        ├── verify election is LIVE
        ├── verify voter eligibility
        ├── verify candidate belongs to position and is APPROVED
        │
        ▼
    transaction.atomic()
        │
        ├── SELECT ... FOR UPDATE on voter row
        ├── check voter-position participation
        ├── create anonymous Vote(position, candidate)
        ├── record voted position on StudentVoter
        └── write audit event
```

The row lock is the concurrency guard because the anonymous `Vote` table deliberately does not identify the voter.
