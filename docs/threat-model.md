# Threat Model

## Assets

- Voter credentials and account state
- Voter eligibility records
- Anonymous ballot records
- Election configuration and state
- Candidate records
- Final results
- Audit records
- Application secrets and provider credentials

## Threats considered

### Unauthorized voting

Controls include authenticated voter sessions, election-state checks, eligibility checks, approved-candidate checks, and one-vote-per-position enforcement.

### Duplicate or concurrent voting

The voter participation relation is checked inside a transaction while the voter row is locked on PostgreSQL. Tests cover sequential duplicate attempts and concurrent attempts.

### Unauthorized election access

Views verify that the authenticated voter is eligible for the requested election (category-based rules), and the vote utility re-validates election state inside the transaction.

### Credential attacks

Django password hashing, OTP expiry, failed-attempt limits, and new-device verification reduce common account takeover paths. Production secrets are supplied through environment variables rather than source control.

### Administrative privilege abuse

Separate role decorators are used for school-admin and election-officer workflows. Audit logs record many privileged operations.

### Information leakage

Ballot records do not contain a voter foreign key. The ballot UI only reports whether the current voter has voted for a position; it does not infer a selected candidate from another ballot.

## Out of scope / not solved completely

This project is not a cryptographic end-to-end verifiable voting protocol, does not provide coercion resistance, and does not independently establish the correctness of an institution's voter-roll source data.
