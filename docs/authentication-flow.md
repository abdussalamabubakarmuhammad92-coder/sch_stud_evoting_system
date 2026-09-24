# Authentication Flow

## Voters

1. Voter enters organization-specific login details.
2. Existing credentials are authenticated through Django's authentication system.
3. Recognized-device information is checked.
4. New devices can require OTP verification.
5. Successful authentication creates the Django session.

## Administrators

1. Administrator authenticates through the admin login form.
2. The account must be active and have an allowed administrative role.
3. The application redirects the user according to their role.
4. Organization-scoped decorators protect organization administration views.

## Registration

1. Student supplies matric number, email, and phone.
2. The system matches the matric number against the organization's verified voter records.
3. A strict phone/email match is required according to the available official record.
4. An OTP is generated and stored as a hash.
5. Successful OTP verification activates the next registration step.
