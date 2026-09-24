# Data Model

## Organization

Tenant boundary for elections, voters, administrators, and audit records.

## User

Django custom user model. Role-specific relationships determine organization-admin, election-officer, voter, and platform-owner behavior.

## Election

Belongs to an organization and category. Contains state, eligibility configuration, timing, and final tally integrity metadata.

## Position

A contest within an election.

## Candidate

A candidate for a position. Candidates pass through screening states before becoming eligible for voting.

## VerifiedVoterRecord

Institution-supplied voter source data used as the registration authority.

## StudentVoter

A registered voter account. Its `voted_positions` relation records participation without linking a ballot to a voter.

## Vote

Anonymous ballot record containing position and candidate only.

## OTPVerification

Hashed, expiring verification codes with usage and failed-attempt state.

## AuditLog

Organization-scoped security and administrative event record.
