# Known Limitations

This project is a portfolio/educational implementation, not a certified public-election platform.

1. **No cryptographic end-to-end verifiability.** Final tally hashing provides an integrity checksum, not a proof that every ballot was correctly recorded or counted. The hash is stored in the same database as the tally, so it cannot by itself resist someone who can modify that database; its value depends on comparing it against a copy kept outside the system (the results CSV embeds it — archive that file at close time).
2. **No coercion resistance.** The system does not solve coercion or vote-buying scenarios.
3. **Application-level anonymity.** Removing the voter foreign key from `Vote` reduces direct linkage but does not eliminate all possible side channels.
4. **Voter-roll trust.** Eligibility depends on the institution-supplied `VerifiedVoterRecord` data.
5. **External delivery providers.** OTP delivery depends on configured email/SMS infrastructure.
6. **Deployment security is configuration-dependent.** Production secrets, HTTPS origins, database access, Redis, SMTP, and provider credentials must be configured correctly.
7. **PostgreSQL is the intended concurrency environment.** SQLite is suitable for local development, not production election traffic.
8. **Audit-log durability is transactional for critical events only.** Vote casting and election state changes write their audit record inside the same transaction as the action — a failed audit write aborts the action. Lower-stakes events are best-effort: the action stands and the failure is logged. The audit trail is strong where it matters most, not absolute for every event.
