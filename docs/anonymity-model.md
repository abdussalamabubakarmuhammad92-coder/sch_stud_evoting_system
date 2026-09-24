# Anonymity Model

The central design choice is that `Vote` contains only:

- `position`
- `candidate`
- `created_at`

It intentionally has no `StudentVoter` or `User` foreign key.

The voter's participation state is stored separately in `StudentVoter.voted_positions`. This allows the application to enforce one vote per position without putting a direct identity reference on the ballot row.

## What this design provides

- The normal ballot table cannot be queried with `vote.voter` because no such relationship exists.
- The ballot UI can tell a voter that a position has already been voted in without selecting a candidate based on somebody else's vote.
- Concurrent attempts by the same voter can be serialized by locking the voter row.

## What it does not provide

This is not a formal cryptographic anonymity guarantee. Database administrators, application operators, logs, timestamps, backups, network infrastructure, or other side channels may still create information that could be correlated outside the `Vote` model.

The project therefore documents this as an **application-level voter/ballot separation model**, not as a complete anonymous voting protocol.
