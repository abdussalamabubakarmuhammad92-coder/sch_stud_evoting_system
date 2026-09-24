# SUG E-Voting Platform

A Django-based electronic voting platform designed for student-government elections. The system models organizations, voter eligibility, role-based administration, election lifecycles, candidate screening, OTP verification, anonymous ballot records, audit logging, and final-tally integrity checks.

> **Project status:** portfolio / educational system. This repository is not presented as a certified or cryptographically verifiable public-election system.

## What this project demonstrates

- Django 5 + PostgreSQL architecture
- Multi-organization (tenant-scoped) data model
- Role-based workflows for platform owners, organization admins, election officers, and voters
- Voter registration against institution-supplied records
- OTP verification for registration, new-device login, and password reset
- Election lifecycle management: `DRAFT → LIVE → CLOSED → ARCHIVED`
- Candidate screening and approval
- One-vote-per-position participation enforcement
- Anonymous ballot records: `Vote` deliberately has no foreign key to a voter
- Audit logging for security-relevant operations
- Celery + Redis background tasks
- Result tally hashing for final-tally integrity checking
- CSV voter import and result/data export workflows

## Architecture

```text
Browser
   │
   ▼
Django views / forms / decorators
   │
   ├──────── Authentication & OTP
   ├──────── Organization / role authorization
   ├──────── Election lifecycle
   ├──────── Eligibility checks
   ├──────── Anonymous vote recording
   └──────── Audit logging
   │
   ▼
PostgreSQL
   │
   ├── Organization / users / voters
   ├── Elections / positions / candidates
   ├── Vote records
   └── Audit / verification records

Redis ───────────────► Celery workers / periodic tasks
```

See [`docs/architecture.md`](docs/architecture.md) for a more detailed model and request flow.

## Ballot anonymity model

`Vote` intentionally does **not** contain a foreign key to `StudentVoter` or `User`. Voter participation is tracked separately through `StudentVoter.voted_positions`.

This creates a separation between:

- **Eligibility / participation:** which positions a voter has already voted in.
- **Ballot record:** which candidate received a vote for a position.

The application therefore does not use the ballot table itself to identify the voter who cast a particular ballot.

This is an application-level anonymity design, **not a complete cryptographic voting protocol**. See [`docs/anonymity-model.md`](docs/anonymity-model.md).

## Vote concurrency protection

The voting invariant is enforced inside a database transaction. Because the anonymous `Vote` table cannot carry a voter foreign key, the voter row is locked with `select_for_update()` while the participation record is checked and updated.

This serializes simultaneous vote attempts from the same voter on PostgreSQL while preserving the separation between voter identity and ballot records.

Automated tests cover sequential duplicate attempts and a PostgreSQL-only concurrent-vote test.

## Security considerations

The public repository intentionally contains no real credentials, uploaded voter/candidate data, production media, or application logs.

Before deployment, configure secrets through environment variables. See [`.env.example`](.env.example).

See [`docs/threat-model.md`](docs/threat-model.md) for the security assumptions, assets, threats, and mitigations documented for this project.

## Local development

### 1. Clone

```bash
git clone <your-public-repository-url>
cd sug-evoting-platform
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it using your operating system's normal virtual-environment command.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

Copy `.env.example` to `.env` and set at least:

```text
DEBUG=True
DJANGO_SECRET_KEY=<long-random-development-secret>
```

When `DEBUG=True` and no `DATABASE_URL` is supplied, the project uses local SQLite. Production must use PostgreSQL.

### 5. Migrate and run

```bash
python manage.py migrate
python manage.py runserver
```

## Production configuration

When `DEBUG=False`, the application requires explicit production configuration for:

- `DJANGO_SECRET_KEY`
- `ALLOWED_HOSTS`
- PostgreSQL (`DATABASE_URL` or the `DB_*` variables)
- `REDIS_URL`
- SMTP email settings

HTTPS, secure session/CSRF cookies, and HSTS are enabled by default in production mode. Configure `CSRF_TRUSTED_ORIGINS` for the deployment's HTTPS origins.

## Tests

Run the full test suite with:

```bash
python manage.py test
```

The concurrency test is intended for PostgreSQL. It is skipped on SQLite because SQLite does not provide the same row-locking semantics used by the production voting invariant.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system components and request/data flows
- [`docs/threat-model.md`](docs/threat-model.md) — assets, threats, assumptions, and mitigations
- [`docs/anonymity-model.md`](docs/anonymity-model.md) — ballot/voter separation and its limits
- [`docs/authentication-flow.md`](docs/authentication-flow.md) — admin/voter authentication and OTP paths
- [`docs/otp-flow.md`](docs/otp-flow.md) — OTP lifecycle, expiry, and failed-attempt controls
- [`docs/election-lifecycle.md`](docs/election-lifecycle.md) — election state transitions
- [`docs/data-model.md`](docs/data-model.md) — major entities and relationships
- [`docs/audit-logging.md`](docs/audit-logging.md) — audit events and privacy considerations
- [`docs/known-limitations.md`](docs/known-limitations.md) — what this project does not claim to solve
- [`docs/credential-rotation.md`](docs/credential-rotation.md) — action required because the original private repository contained historical secrets

## Responsible disclosure

If you discover a security issue in this educational project, please avoid publishing active credentials or personal data in an issue. Contact the repository owner privately where possible.

## License

MIT — see [`LICENSE`](LICENSE).
