# Audit Logging

The `AuditLog` model records organization-scoped events with:

- action type
- description
- actor label
- optional election
- optional structured metadata
- timestamp

Examples include election state changes, candidate actions, imports, result hashing, password-related events, and vote-casting events.

The audit record for a vote deliberately records the event and election context rather than a voter identity or candidate selection, preserving the application's ballot/identity separation at this layer.

Logs are emitted to application stdout/stderr in the public configuration. Persistent private production logging should be handled by the deployment platform with an appropriate retention and access policy.
