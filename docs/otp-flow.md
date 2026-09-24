# OTP Flow

OTP codes are generated using Python's `secrets` module and stored as SHA-256 hashes rather than plaintext.

Each verification record contains:

- voter
- purpose
- delivery method
- hashed code
- expiry timestamp
- used state
- failed-attempt counter

Default expiry is 10 minutes. After five failed attempts, the verification is locked.

The repository tests the expiry and failed-attempt lock behavior at the model level. Actual SMS/email delivery remains an external-provider integration and should be tested separately in staging.
