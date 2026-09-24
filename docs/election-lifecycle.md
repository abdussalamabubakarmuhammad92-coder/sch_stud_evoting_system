# Election Lifecycle

The supported state machine is:

```text
DRAFT → LIVE → CLOSED → ARCHIVED
```

Invalid transitions are rejected by `Election.can_transition_to()`.

### DRAFT

Configuration and candidate preparation occur here.

### LIVE

Eligible voters can cast ballots. The vote utility verifies that the election remains live before creating a ballot.

### CLOSED

Voting has ended. Public result views become available.

### ARCHIVED

The election is retained as historical data and cannot transition further.
