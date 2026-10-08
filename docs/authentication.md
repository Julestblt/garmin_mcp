# Connection authentication

Stride sends its Supabase access token as `Authorization: Bearer ...` from its trusted backend. The service checks that token against Supabase Auth `/auth/v1/user` on each request. It takes the returned `id` as the user ID; client-supplied user IDs are ignored. The service role key never goes to the browser.

`POST /v1/connections/garmin/start` accepts Garmin email and password over TLS. They exist only in request memory and in the temporary Garmin client. On success, only serialized Garmin session material is encrypted and saved. Reconnect authenticates the replacement first, then atomically swaps the active connection and secret. Invalid credentials or MFA leave the previous connection usable.

When Garmin requires MFA, the response contains `mfa_required` and an opaque UUID. The challenge stores encrypted HTTP cookies and Garmin continuation fields with a ten-minute expiry. `POST /v1/connections/garmin/mfa` consumes it atomically for the same Stride user. A connection-version check rejects a challenge if another reconnect or disconnect completed first. Invalid OTP consumes the challenge; start again to request a new code. Passwords and OTPs are absent from challenge state and API responses.

Garmin's current MFA continuation is implemented in private client fields. The adapter serializes only the necessary values, including the widget CSRF token when applicable. Upgrade `garminconnect` with MFA compatibility tests. `DELETE /v1/connections/garmin` disconnects, removes the secret, and stops new sync while retaining imported history. `DELETE /v1/connections/garmin/data` also deletes Garmin imports and jobs. Garmin does not expose a reliable remote token revocation API here.
