# Team & places: judge review (commits a4e63b0, 0f489f5, 17aa3fc)

Date: 2026-11-13. Reviewer: judge. Code is not edited; probes ran from a scratch module outside the repo.

## Verdict: REVISE

The permission core is sound: every team write is scoped by `clinic_id`, version-checked, audited, and goes
through `check_change`/`check_add`. I found no way for a non-owner to give themselves or anyone else a code,
touch OWNER_ONLY codes, or change the owner, and no way for an owner to reach another owner's place. Three
problems block approval. Two of them are business-rule problems found in the probes, and one is a one-tap
permission hazard in the table.

Tests: `clinics.test_team clinics.tests scheduling.test_own_visits web.test_team web.test_overview`, 84 tests, all pass
on PostgreSQL (`_judge` suffix).

## Scores

| # | Criterion | Score | Reason |
|---|-----------|-------|--------|
| 1 | Business rules | 5 | The grant rules are right. But an owner who treats patients loses Today and Prescription, and a section-limited assistant can still bill another section. |
| 2 | Concurrency | 8 | A conditional `UPDATE ... WHERE version` on every write, and places are visited in clinic_id order. The actor's own row isn't re-read under lock (small window). |
| 3 | Performance | 7 | Indexed and bounded. The /team page reads every owned place on every load (up to 50 shard round trips), and GET team silently cuts the list at 200. |
| 4 | Isolation / security | 8 | No cross-tenant path and no escalation path found. The read-only `_overview` and `places()` trust a possibly stale directory copy. |
| 5 | Operability | 7 | The 503 `places_incomplete` path resends idempotently. A place that is permanently `unavailable` makes it 503 forever. |
| 6 | Usability (ar/en) | 5 | Five of eight groups read "جزئي" for a fresh assistant. One tap on a partial group switches every code in it on, including owner-only money codes. |
| 7 | Simplicity | 7 | One rule function and one table. team.py (592 lines) is fine. |
| 8 | Tests | 7 | Escalation, stale, self, owner-fixed, places and the 503 path are covered. Nothing covers billing under a section limit, the owner-with-sessions home, or what a group tap grants. |

## Blocking problems (ranked)

1. **An owner who treats patients can't work their own day.** `v2/web/nav.py:38-43` (`is_manager` = every owner),
   `v2/web/pages.py:209` and `v2/web/rx_pages.py:58` send every owner from `/`, `/today` and `/prescriptions` to `/overview`
   (probe: 302 to /overview for an owner who has sessions). Sign-up still gives the owner doctor sessions
   (`v2/directory/signup.py:156,195`, `default_session=True`, so `v2/directory/flows.py:186` runs). The owner is
   therefore bookable, but has no screen to call in, finish, or write notes and prescriptions. A solo-doctor clinic,
   the commonest sign-up, can't book at all from the UI. This contradicts requirements line 40 ("The doctor who
   owns ... Everything"). Saged's words were "the owner never **books**". Fix: hide only booking and walk-in for the
   owner (`_perms["book"]` in `v2/web/pages.py:42` already does that), and keep Today's queue and Prescription for
   an owner who has sessions, i.e. restore the schedule check in `is_manager` (the 0f489f5 version). Alternatively,
   stop `default_session` for owners and leave the owner out of the doctor lists. Get Saged to confirm which, and
   add a test for each case.

2. **The section limit doesn't cover billing.** Design G1 says a limited assistant gets 404 on another section's
   appointment. Probe: that assistant gets 404 on `GET appointments/{id}`, but 201 on
   `POST appointments/{other-section id}/invoice` and on `/payments`. `v2/billing/services.py` (`pay`:185,
   `invoice_for_visit`:201, `add_line`:254, `remove_line`:309, `set_discount`:360) never calls
   `scheduling.rules.guard`/`check_scope`. Fix: call `check_scope` on the locked appointment row in the
   appointment-based billing services and in the invoice-id paths after the lock. Add a failing test first
   (CLAUDE.md: money bug).

3. **One tap on a partial group grants owner-only codes.** `v2/web/static/js/team.js:183` sends `groups[key] = (state !== "on")`,
   so tapping "جزئي" on Money for a doctor grants `billing.refund`, `finances.view` and `finances.pay` (probe
   confirmed). The doctor can then see the whole month's money and staff pay. Tapping a partial Today grants
   `appointments.others` without saying so. Fix: a tap on a partial group opens its codes instead of switching
   them, or asks "Switch on all 9, including refunds and staff pay?". Never switch OWNER_ONLY codes on from a
   group tap without that confirmation. Add a browser test.

## Answers to the specific checks

- **Tenant isolation**: `_target`, `_write` and `clean_sections` all filter by `ctx.clinic_id`, and another place's member
  id is 404 (tested). `set_places` only touches places where the directory says owner **and** the shard
  confirms an owner membership (`v2/clinics/team.py:481`). An owner can't reach another owner's place. The read-only
  `_overview` (`v2/web/team_pages.py:62`) and `places()` (`v2/clinics/team.py:439`) trust the directory alone. That is a stale-copy
  read of names and roles only, so it is not blocking.
- **Escalation**: `not_self`, `owner_fixed`, `owner_only_target`, `owner_only_code` and `not_held` are enforced in
  `v2/clinics/permissions.py:73-100`. Deactivation counts as removing everything, and reactivation counts as adding
  (`v2/clinics/team.py:193-200`). Adding is checked by `check_add`, places are owner-only, and reissuing a password goes
  through `_check`. I found no bypass.
- **Version and lock order**: key, then target, matches G1. The actor's codes come from the request-start read, so
  if the owner revokes `team.manage` mid-request, one in-flight change still lands. That is acceptable.
- **Audit**: `team.change` (before/after diff), `team.permissions` (added/removed), `team.add`, `team.invite`,
  `team.join`, `team.password`, `team.place` per place, and `team.places`. The coverage is complete.
- **Owner API still callable** (walk-in, book, arrive: probe 200): this is harmless for security because the owner
  holds every code. It is only a UI decision. It matters only because of blocking problem 1.
- **Dead links**: `forbidden.html` and `not_found.html` say "Back to Today", and `teeth.html` links "Today", but `/` sends the owner to
  /overview. There is no loop, just the wrong label. A member with neither queue, patients.view nor reports.view
  gets a 403 on `/`, which links back to `/`: a loop the owner can now cause from the table (nice-to-have 1).

## Proposed labels (the "جزئي" question)

Compare each group to the person's **role template**, not to "all":
- "مفتوح" / "On": every code. "مقفول" / "Off": none.
- "حسب الدور" / "As role": exactly the template's codes in that group. This is the default, so a fresh person reads
  calmly.
- "مخصّص ٣ من ٩" / "Custom 3 of 9": anything else. The count tells the owner what "partial" means.
- Keep the "owner only" mark next to owner-only codes when the group opens (`team_owner_code`).

## Nice-to-haves

1. `today_page` (`v2/web/pages.py:212-215`): send someone without the queue, patients or reports to their first visible
   section instead of a 403 page that links back to `/`.
2. `v2/clinics/team.py:587`: return `unavailable` as a per-place result without a 503 when the place is suspended
   (not moving), so a client doesn't retry forever.
3. `v2/web/team_pages.py` `_overview`: load the other places lazily (a fragment), not on every /team load. Say
   "more than 200" instead of truncating silently (`v2/clinics/team.py:104`).
4. Invitations: re-check the inviter's `team.manage` at accept, and offer a cancel.
5. `set_permissions` audit: drop the empty `before` and store the full before/after code sets.
6. `guard`/`check_scope` read the appointment without a lock before the conditional update. A reschedule to another
   doctor in between lets one action through. Re-check `doctor_id` in the conditional `UPDATE ... WHERE`.