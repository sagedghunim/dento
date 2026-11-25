## Appendix E: M2, the doctor's visit, Dento, and the sign-up cards

Saged's decisions of 2026-11-11. The final look comes later from Saged's own UI plan, so the screens built now must be functional and plain; the work is in the data, the rules and the API. Product rules: simple and few icons, no important feature removed, and every clinic list editable by the clinic itself, with a clear "+" (add), edit and remove.

### E1. Product name

The product is **Dento** (Arabic «دينتو»). It appears in page titles, the sign-in page, the shell, receipts and prescriptions. Code, database and app names stay as they are.

### E2. Sign-up: clinic or center, then specialty cards

1. **Clinic or center.**
2. **Specialty cards.** There are 10, in this order: dental, pediatrics, dermatology & cosmetic, OB/GYN, ENT, ophthalmology, orthopedics, cardiology, physiotherapy, veterinary.
   - A clinic picks one card.
   - A center gives each card a count, from 0 to 20, with − and +, such as dental × 8 and pediatrics × 2. The total must be at least 2.
   - A count of N creates N sections named «أسنان 1» … «أسنان N». A count of 1 gives just «أسنان». The names are editable.
3. **Center only: "Are all the sections in one place, with one front desk and one cash desk?"**
   - **Yes:** one center with these sections. Patients, the front desk, money and the team are shared (Appendix D).
   - **No (separate branches):** one place per card, under the same owner account. A card with count N becomes that place's N sections. The owner moves between places from the picker. A cross-place owner summary comes in M3.
4. The place's details and the owner's account, as before.

Request: `sections: [{specialty, count, name?}]` plus `layout: "one_place" | "branches"`. Each count is from 1 to 20, and a place has at most 20 sections. `veterinary` is a card now, but the animal file (species, breed, owner with several animals) comes after M3. Until then a vet place uses the patient file as is.

### E3. The visit (shard)

- **Stages:** `booked → waiting → with_assistant (optional) → ready → in_room → done`. Each has its own timestamp column.
  - The desk or an assistant moves a visit to `with_assistant` and `ready`. "Call in" works from `waiting` or `ready`.
  - Undo works one step back from each new stage.
  - Today and the visit screen show the waiting time (arrival to call-in), the doctor time (call-in to done) and the total.
- **Visit record:**
  - **Chief complaint:** a short text.
  - **Notes:** design 4.5's versioned note, with `If-Match`.
  - **Vitals:** blood pressure (systolic and diastolic), pulse, temperature, weight, height, SpO₂ and blood sugar. All are optional, and anyone with `visits.vitals` may enter them, assistants included.
  - **Attachments:** images and PDFs up to 10 MB, on the patient and optionally on the visit. Local storage now, behind a storage class, so S3 can replace it later. Opening one writes an access event.
  - **Timeline:** a read view of the patient's visits, payments, prescriptions and procedures, newest first.
- **Medicines library**, per section:
  - Fields: `name` (required), `strength`, `form`, `default_dose`, `default_note`, `is_active`.
  - Copied from the specialty template, and fully editable by anyone with `settings.medicines`, which doctors have.
  - A doctor can add a medicine from inside the prescription, and it joins the library.
- **Prescription:**
  - Lines carry the medicine, the dose and a note.
  - Templates are named sets of lines, per section, editable.
  - It prints on the A5 letterhead, as in design 7.
  - The patient's recorded allergies are matched against each line's name and shown as a warning. Interaction checks need a drug database and come later.
  - Writing one needs `prescriptions.write`, which only doctors have by default.
- **Procedure catalogue**, per section:
  - Fields: `name_ar`, `name_en`, `category`, `unit` (`tooth`, `quadrant`, `arch`, `session`, `unit` or `case`), `price`, `typical_min`, `typical_max` (the market range shown as a hint), `description`, `internal_cost`, `is_active` and `sort`.
  - Copied from the specialty template and editable with `settings.manage`.
  - Services (design 4.1) stay for what the desk books. Procedures are what the doctor records during a visit; each one recorded adds an invoice line.
- **Dental chart:**
  - A per-patient tooth state, using FDI numbers: 11–48 permanent, 51–85 primary.
  - Conditions: healthy, caries, pulp caries, planned, filling, crown, root canal, open root canal, extracted, missing, implant, bridge, fractured and impacted. Caries and filling take surfaces (M, O, D, B, L).
  - Recording a procedure on a tooth sets its new state, and writes a tooth history row (visit, procedure, surfaces, note).
  - "In progress" means the tooth is in an open plan item.
  - A tooth's history is that tooth's rows, newest first.
- **Treatment plan:**
  - Items are a procedure, a tooth or area, and a price, with a status of planned, in progress, done or dropped.
  - Each plan shows its estimated total, and visits link to it.
  - "Unfinished treatment" means a plan with open items.
- **Finishing the visit:**
  - One request with a request key.
  - It shows the lines, total, paid and remaining.
  - Payment methods: cash, card, transfer or wallet.
  - **"Also collect the earlier balance"** pays the patient's older unpaid invoices, oldest first.
  - **"Book the next visit"** creates the booking in the same transaction.
  - R2's rules apply (`already_finished`).
  - The cash desk's open and closed state comes with the cash register in M3; the payment method is recorded now.
- **Patient balance:** what a patient owes across visits, shown on the visit, the file and the finish dialog.
- **Permissions:** add `visits.vitals`, `visits.notes`, `prescriptions.write`, `settings.medicines` and `procedures.record` to the templates.
  - Doctors get all five.
  - Assistants get `visits.vitals` and the stages.

### E4. Specialty data

- Each of the 10 specialties ships a procedure catalogue and a medicine library, written by us, with Egyptian trade names and market price ranges.
- Dental has about 60 procedures and 30 medicines. Each other specialty has about 15–30 of each.
- A doctor must review them before real patients.
- They live in `clinics/specialties.py`, or split per specialty if it grows.
