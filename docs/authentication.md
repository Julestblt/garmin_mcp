# Connection authentication

Stride sends its Supabase access token as `Authorization: Bearer ...` from its trusted backend. The service checks that token against Supabase Auth `/auth/v1/user` on each request. It takes the returned `id` as the user ID; client-supplied user IDs are ignored. The service role key never goes to the browser.

`POST /v1/connections/garmin/start` accepts Garmin email and password over TLS. They exist only in request memory and in the temporary Garmin client. On success, only serialized Garmin session material is encrypted and saved. A new start replaces the previous connection and its token.

When Garmin requires MFA, the response contains `mfa_required` and an opaque UUID. The challenge stores encrypted HTTP cookies and the Garmin continuation fields with a ten-minute expiry. `POST /v1/connections/garmin/mfa` consumes it atomically for the same Stride user and connection ID. Invalid OTP consumes the challenge; start again to request a new code. Passwords and OTPs are absent from challenge state and API responses.

Garmin's current MFA continuation is implemented in private client fields. The adapter serializes only the necessary values, including the widget CSRF token when applicable. Upgrade `garminconnect` with MFA compatibility tests. Delete removes local token and connection state; Garmin does not expose a reliable remote token revocation API here.
