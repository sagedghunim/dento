## Appendix G: team, places and English names

Saged asked on 2026-11-12 for the demo's "Who can do what" table, for one person working in several places of one owner, for assistants limited to sections, and for English names everywhere in English mode. 5.8 and 5.12 still hold; this appendix adds to them.

### G1. Team and permissions (shard)

- `clinics_membership` gains `display_name_en`, `sections bigint[]` (empty = every section, at most 20) and `version` (every team write is a conditional `UPDATE … WHERE version = %s`, 409 `stale` otherwise).
- **Groups.** `clinics/permissions.py` `GROUPS` splits every code into the side navigation's eight groups: today, patients, visit, money, stock, reports, settings, team. A test checks they partition `CODES`. `GET team` answers `{groups: [{key, label, description, codes: [{code, label, owner_only}]}], members: [{…, codes, groups: {key: "on" | "off" | "partial"}, editable}]}`: rows are groups, columns are members.
- **Writes** all go through `check_change` (5.8): `PUT team/members/{id}/permissions` takes `{version, groups: {key: bool}, codes: {code: bool}}` (codes after groups) and stores the result as `granted = wanted − template`, `revoked = template − wanted`. `PATCH team/members/{id}` takes `{version, role?, is_active?, sections?, display_name?, display_name_en?, title?}`. A role change resets the person to the new role's template. Deactivating counts as removing every code the person holds, so a non-owner can only deactivate someone whose codes they hold. A person may change only their own names, never their role, codes, sections or status.
- **Adding** (`POST team/members`): `mode: "new"` creates the account in the directory first; the account stores the clinic and request key that created it, so a retry of the same request reuses it (B6) and shows no password; any other taken username is 409 `username_taken`, as at public sign-up (D2), which already tells. `mode: "invite"` answers 202 whatever the username: an existing account gets a `directory_invitation` (7 days), listed by `GET me/invitations` and accepted by `POST me/invitations/{id}/accept` (shard membership, then `user_clinic`). Invitations live in the directory, not on the shard (4.2 said shard): the invitee has no shard to look in. `POST team/members/{id}/temporary-password` reissues a temporary password only for an account this place created that has never signed in.
- **Lock order:** key → the target membership (conditional `UPDATE`). It conflicts with a booking's `FOR SHARE` on a doctor, which only makes them take turns.
- **Sections.** Only assistants carry `sections`; each id must be an active section of the place (checked in the service; an array can't hold a composite key, and a section is never deleted). For a limited assistant Today, the queue fragment, the sections, services and doctors lists, slots and booking show only those sections; an appointment of another section answers 404, like another place's. Doctors keep their sections from their sessions. Today's ETag includes the limit.

### G2. One person in several places (directory, then each shard)

- Only the owner, and only across places they own (`user_clinic.role = owner`), confirmed on each shard by their owner membership there.
- `GET team/members/{id}/places` reads the directory only: the owner's places and this person's links (two queries).
- `PUT team/members/{id}/places` takes `{places: [{clinic_id, role?}], copy: bool}`: a ticked place gets a membership for the **same** account (insert `ON CONFLICT (clinic_id, user_id) DO NOTHING`, or reactivate); an unticked one is deactivated, never deleted. `copy` copies this place's role and codes; otherwise `role` per place, or this place's role with its template. Sections are never copied (ids are per place). The current place is changed only through `PATCH`.
- Each place is its own step: on another shard its own transaction, then its `user_clinic` row; on the request's shard a savepoint with `app.clinic_id` switched and restored, and its `user_clinic` written after the request commits. Each step writes absolute values, so it is idempotent and needs no request key of its own; each place's log gets its own `audit_change`.
- **Failure:** if any place fails (shard down, lock timeout, the place moving), the answer is 503 `places_incomplete` with each place's result and `Retry-After`. The request's own transaction rolls back with its key, so the client resends the same key; steps that committed on other shards find their rows already right and change nothing. A lost `user_clinic` write leaves the picker stale until the next run or the nightly reconciliation; the shard stays the authority.

### G3. English names

- Optional English text beside the typed text: `clinic.name_en`, `section.name_en`, `membership.display_name_en`, `service.name_en`, `medicine.form_en / default_dose_en / default_note_en`, `prescription_template.name_en / notes_en`, `procedure.description_en` (its `name_en` exists), `inventory_item.name_en`, `finance_category.name_en`, `patients_tag.name_en`, and the directory's `clinic.name_en` for the picker. Patients, visits, prescriptions and invoices stay as typed.
- Seeds fill both languages from `clinics/catalogue/` (`english.py` holds the English of every Arabic catalogue phrase; a test fails on a missing one). Sign-up names sections in both languages.
- Reads return each text in the request's language, falling back to the other when empty (`core.i18n.pick`), and also return the raw pair (`name_ar`, `name_en`, and so on) for edit screens. Edit APIs accept both.

### G4. A doctor's own visits

- New code `appointments.others` ("Handle other doctors' patients", group today): off in the doctor template; the owner holds every code.
- A member with role `doctor` without it may call in, finish, undo a call and write clinical data (note, vitals, prescription, attachment, tooth, recorded procedure) only on visits whose `doctor_id` is their membership: 403 `not_your_patient` otherwise. Arrive, the assistant's stages, booking, cancel, reschedule and payments stay the desk's. Plans and the chart without a visit stay with their codes. Reading the patient's shared file stays with `clinical.view`.
- Checked in the services (`scheduling.rules.guard` / `check_scope`), with one extra read only for a bound doctor or a limited assistant. Today answers `own_only`, `my_doctor_id` and a `can_act` flag per visit; its ETag includes the membership.

### G5. Walk-in ("دخول فوري")

- `POST appointments/walk-in` (`appointments.book` and `appointments.queue`) takes the patient (`patient_id` or `new_patient`) and optional `doctor_id`, `service_id`, `section_id`, `payment` and `note`.
- The doctor defaults to: a bound doctor themselves when they work today; else the only doctor working today; else the one with the fewest waiting, then the lowest id; only doctors working today in the member's sections count. The service defaults to the first active consultation of the doctor's first section today.
- It books today at the next turn with no time, even for a slot doctor, marked arrived, through the same booking code and lock order (5.0), in one request under its key. It answers the visit, `turn` and `ahead` (how many wait for that doctor before it).
