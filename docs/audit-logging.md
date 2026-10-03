# Audit Logging

The `AuditLog` model records school-wide events with:

- action type
- description
- actor label
- optional election
- optional structured metadata
- timestamp

Examples include election state changes, candidate actions, imports, result hashing, password-related events, and vote-casting events.

The audit record for a vote deliberately records the event and election context rather than a voter identity or candidate selection, preserving the application's ballot/identity separation at this layer.

Logs are emitted to application stdout/stderr in the public configuration. Persistent private production logging should be handled by the deployment platform with an appropriate retention and access policy.

## Tamper evidence

Every audit entry is hash-chained: each row stores the hash of the previous
entry in its chain plus the SHA-256 hash of its own contents. Election-scoped
events chain per election; school-wide events chain separately. Writers of an
election's chain are serialized on the election row lock, so the chain cannot
fork. `verify_audit_chain()` (exposed as **Verify chain integrity** on the
audit page) re-walks every chain and pinpoints any entry that was edited or
deleted after being written. The CSV export embeds each entry's hash so the
register can be archived and checked outside the system.

## Severity levels

Entries carry a severity: `INFO` for routine operations, `SECURITY` for
denied or suspicious activity (failed logins, OTP failures and lockouts,
ineligible ballot access, technical-panel changes) and `CRITICAL` for actions
that change election state (votes cast, election open/close, double-vote
attempts).

## Durability semantics

Audit writes for **vote casting** and **election state changes** are critical:
they run inside the same database transaction as the action itself, so a
failed audit write aborts the action — the system never records a vote or a
state change without its audit entry. All other audit events are best-effort:
if their write fails, the action stands and the failure is logged to the
application log.
