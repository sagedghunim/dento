## Appendix D: draft 2.2, centers with sections and self-service sign-up

Saged asked for both on 2026-11-07 (requirements 4.1 and 4.10). The tenant stays the place: one `clinic` row, one shard, one row-level security key. A center is a place with several sections. This keeps patients, the front desk, money and the team shared across the center, and changes nothing in routing, RLS or moving a clinic between shards.

### D1. Data model (shard)

- `clinic.kind`: `clinic` or `center`, default `clinic`. `clinic.specialty_code` stays as the main specialty, which is the first section's.
- New table `clinics_section`: `id` (from the shard's range), `clinic_id`, `name`, `specialty_code`, `is_active`, `sort`, `created_at`. `UNIQUE (clinic_id, id)` for composite foreign keys, plus a unique active name per clinic. RLS is forced.
- Every place has at least one section. A clinic has exactly one, created with it and named after its specialty. Screens hide sections when there is only one.
- `service.section_id` NOT NULL, with a composite key to `(clinic_id, section_id)`. Templates are copied per section. Medicines (M2) and stock items (M3) will also carry `section_id`.
- `schedule.section_id` NOT NULL: a doctor's session belongs to one section, so a doctor's sections are the sections of their sessions. There is no doctor-to-section table.
- `appointment.section_id` NOT NULL, copied from the service at booking. The index that serves Today gains no column: Today reads the clinic's day and filters by section in memory, since a large center has a few hundred rows a day.
- The migrations add the columns, create one section per existing clinic from `specialty_code`, fill `section_id`, then set NOT NULL. Applied migrations are not edited.

### D2. Sign-up (directory, then shard)

- `GET /signup` is public and server-rendered, in the demo's look. It has four steps on one page:
  1. clinic or center;
  2. the specialty as cards (one for a clinic, two or more for a center, each card becoming a section with an editable name);
  3. the place's name, city and phone;
  4. the owner's full name, mobile, username and password.
- `POST /api/v1/signup` (public, CSRF, request key) takes `{kind, sections: [{specialty, name?}], name, city, phone?, owner: {full_name, mobile, username, password}}`.
  - It answers 201 `{next: "security"}` and signs the new owner in, who must then set up two-step sign-in as any owner does.
  - It validates the same way as the platform admin's form. It answers 409 with `fields` for a taken username or mobile.
  - A clinic takes exactly one section; a center takes 2–20.
- `POST /api/v1/me/places` lets a signed-in account add a place with the same body without `owner`. The account becomes the owner. Platform admins are refused.
- The place goes to the least full shard through `directory.flows.create_clinic`, which gains `kind` and `sections`. The platform admin's form gains the same choice.
- Abuse:
  - Throttles in PostgreSQL per client IP: 3 sign-ups an hour and 10 a day.
  - A hidden honeypot field.
  - Django's password validators.
  - An account event `signup` with the IP.
  - SMS checks wait for the provider (requirements 7.6).

### D3. Screens

- The sign-in page links to sign-up ("عيادة أو مركز جديد؟ سجّل الآن"). The clinic picker shows a center badge and has "add a place".
- In a center, Today shows section chips above the list (all sections, then each one). The booking dialog asks for the section first, and skips that question when there is only one.

### D4. Undo on the desk (answers requirements 7.8)

- `POST appointments/{id}/undo-arrive` takes `{"status": "waiting"}` and moves the visit back to `booked`, clearing `arrived_at`.
- `POST appointments/{id}/undo-call` takes `{"status": "in_room"}` and moves it back to `waiting`, clearing `called_at`.
- Both need `appointments.queue`. They work only from the status right after the step being undone, keep the turn number, update the day's counts in the same transaction and bump Today's version. Each undo is written to the audit log.
