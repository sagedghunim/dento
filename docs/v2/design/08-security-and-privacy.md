## 8. Security and privacy

- **Tenant isolation, in five layers:**
  1. the clinic comes from the session's membership, re-checked on the shard in every request, with `X-Membership` guarding against another tab's clinic;
  2. every query filters on `clinic_id`, and a test-time check fails any statement whose root tenant table isn't filtered on it;
  3. composite foreign keys stop cross-clinic references;
  4. forced row-level security refuses other clinics' rows;
  5. tests try every endpoint with another clinic's ids, in paths and in bodies.
- **Grants:** the rules in 5.8 stop escalation. Services check codes that depend on the request body.
- **Accounts:** clinic endpoints never change accounts. Platform admins hold no memberships. Support actions need two admins and leave a trail in the clinic's own log (5.12).
- **Passwords:** Django's PBKDF2 hasher, at least 10 characters, the common-password check. Temporary passwords expire in 72 hours.
- **Two-factor codes:** 5.9. Secrets are encrypted; recovery codes are hashed.
- **Sessions:** 12 hours, a new key at sign-in and after the code, `Secure`, `HttpOnly`, `SameSite=Lax`, CSRF on every mutating request.
- **Headers:** a content security policy that allows only this site, HSTS, `nosniff`, frames denied, referrer `same-origin`, and `Cache-Control: no-store` on every API, page and print answer.
- **Logs:** no query strings or bodies in the proxy, Gunicorn or application logs. Sentry, when used, has `send_default_pii=False` and scrubbing.
- **Audit:** `audit_change` records permissions, members, prices, discounts, refunds, voids, stock adjustments, patient identity edits, settings, specialty changes, exports and support actions, with before and after. `audit_access` records every read of clinical data, declared per view (6.1).
- **Before real patients (M5):** a per-membership limit on file opens with an alert; an idle lock on shared desk PCs; sessions that end at the clinic's day end.
- **Data minimisation:** no national ID numbers in v2.
- **Open:** the legal review under Law 151 of 2020 (R5).
