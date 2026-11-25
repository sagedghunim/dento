## 6. API

### 6.1 Conventions

- JSON action endpoints under `/api/v1/`. Pages and fragments are HTML (section 7). Browsers use the session cookie and send the CSRF token in a header, read from the cookie at each send. CSRF failures answer JSON (`CSRF_FAILURE_VIEW`), so the client can fetch a fresh token once and resend.
- Every view declares its method, its permission codes and whether it returns clinical data: `@api(method, perm=…, access=…)`. A test over the URL configuration checks that every view returning patient fields declares `access`.
- **Required headers:** `Idempotency-Key` on every mutating request; `X-Membership` on every request.
- **Errors:** `{"error": {"code": "slot_taken", "message": "<in the user's language>", "fields": {…}}}`. Statuses 400, 401, 403, 404, 409 (`status_changed`, `slot_taken`, `day_full`, `over_balance`, `refund_first`, `busy`, `clinic_changed`), 422 (`request_key_reused`), 429 (with `Retry-After`) and 503 (with `Retry-After`). A record of another clinic is 404, never 403.
- **Every answer** carries `Cache-Control: no-store` and `X-App-Version`. Every mutating answer echoes the request key.
- **Lists** take `?limit=` (at most 100) and `?after=<cursor>`, and return `{"items": […], "next": "<cursor>" | null}`. The cursor encodes the sort key and the id.
- **Times** in JSON are ISO 8601 with the clinic's offset (`2026-07-15T20:00:00+03:00`), from `zoneinfo` and the pinned `tzdata`. Days are business days (`YYYY-MM-DD`). Money is integer piastres. Display strings are formatted on the server.
- **Values:** one number parser, on the server and the client, accepts Arabic-Indic and extended Arabic-Indic digits and `٫`, `.` or `,` as the decimal point. Money becomes piastres by string arithmetic.

### 6.2 Endpoints

| Area | Endpoints | Permission | M |
|---|---|---|---|
| Sign-in | `POST auth/login`, `auth/totp`, `auth/logout`; `GET/POST me/totp` (set-up and recovery codes) | public, then signed in | M0 |
| Me | `GET me` (person, memberships, codes, `session_expires_at`, app version), `POST me/clinic`, `me/password`, `me/language` | signed in | M0 |
| Clinic configuration | `GET clinic` (active services and prices, doctors with mode and today's hours, medicines, payment methods, display settings), with an ETag | any member | M1 |
| Today | page `/today`, fragment `/today/queue` (ETag) | `appointments.book` or `.queue` | M1 |
| Appointments | `POST appointments` (with `arrive` and payment); `GET appointments/quote`; `GET appointments/{id}`; `POST appointments/{id}/{arrive, undo-arrive, call, finish, cancel, reschedule}`; `GET doctors/{id}/slots?day=` | `appointments.*`, plus body codes | M1 |
| Patients | `GET patients?q=`, `GET patients/by-phone?phone=`, `POST patients`, `GET patients/{id}` (access), `PATCH patients/{id}`, `GET patients/{id}/visits`, `/payments` (access) | `patients.view`, `.edit` | M1 |
| Billing | `POST appointments/{id}/payments`; `GET invoices/{id}` (access); `POST invoices/{id}/lines`, `DELETE invoices/{id}/lines/{line}`, `PUT invoices/{id}/discount`, `POST invoices/{id}/void`, `POST payments/{id}/refund`; `GET invoices?unpaid` (M3) | `billing.*` | M1 |
| Clinical | `GET/PUT appointments/{id}/note` (access), `POST prescriptions`, `GET prescriptions/{id}` (access), `GET patients/{id}/prescriptions` (access) | `clinical.view`, `.write` | M2 |
| Inventory | `GET inventory/items`, `POST inventory/items`, `PATCH inventory/items/{id}`, `POST inventory/purchases`, `inventory/uses`, `inventory/adjustments`, `GET inventory/movements` | `inventory.*` | M3 |
| Finance | `GET finances?month=`, `GET/POST expenses`, `POST expenses/{id}/void`, `GET/POST expense-categories`, `GET/PUT staff-pay` | `finances.view`, `expenses.manage`, `finances.pay` | M3 |
| Consultations | `GET consults`, `POST consults`, `POST consults/{id}/{accept, decline, start, end, cancel}` | `consults.manage` | M3 |
| Team and settings | `GET team`, `POST team/members`, `PATCH team/members/{id}`, invitations; `PUT settings/clinic`, services, schedules, medicines, specialty | `team.manage`, `settings.manage` | M3 |
| Audit | `GET audit/changes`, `GET audit/access?patient=` | `audit.view` | M3 |
| Platform | `GET/POST platform/clinics`, `POST platform/clinics/{id}/suspend`, `/reactivate`, support actions | platform admin, no patient data | M1 |
| Print | `GET /print/receipts/{invoice}?lang=&format=a5|80mm`, `GET /print/prescriptions/{id}?lang=` | `patients.view` (receipt), `clinical.view` (prescription); access | M1, M2 |
| Health | `GET /healthz` (process), `GET /readyz` (directory and shards) | public | M0 |
