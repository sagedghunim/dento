# Hamada v2: requirements

Status: draft 2, 2026-11-07, for Saged's review. Draft 2 adds the business day, a new follow-up assumption and the questions raised by the design review (7.8).
**Assumption** marks a default to use until someone confirms it. **Open** marks a question that still needs an answer.

## 1. Purpose

Hamada is a clinic management platform for private clinics, Egypt first. One platform serves many clinics and many specialties. Each clinic sees the services, medicines and stock list for its specialty.

v2 rebuilds the platform from scratch so that it holds up to 250,000 doctors. The current Django prototype (`clinic/`, `static/`) is a domain reference only.

**The first demo is the model for the screens** (Saged, 2026-11-07): `C:\work\hamada-app`, published at https://hamada-app-ghunim.vercel.app/. v2 keeps its sections (Today, Inventory, Finances, Prescription, Online consultations, Team & settings), its layout and its look: the colours, cards, side navigation and the way each screen works. Arabic and right to left come first, with English as the second language. The data is real and saved on the server, where the demo keeps it in memory. Each milestone's screens are checked side by side against the demo before they are approved.

What a clinic does in Hamada:
- book patients and run the day's queue;
- collect fees and print receipts;
- keep the patient file: visits, notes, prescriptions, payments;
- write and print prescriptions;
- track consumables;
- track expenses and see the month's money;
- manage the team and who can do what;
- handle online consultation requests.

## 2. Scale

- **Target: 250,000 doctors without a redesign.** Launch is tens to hundreds of clinics, so the system must also be cheap and simple when small.
- Assumptions for the target:
  - About 150,000 clinics (1.7 doctors on average). Most have one doctor and one or two assistants.
  - About 250,000 assistants and receptionists, so about 500,000 accounts. About 600,000 memberships, because many doctors work in two or three clinics.
  - 15 visits per doctor per working day on average and up to 20, over 26 working days a month: about 100M visits a month at the target, up to 130M.
  - Clinic hours in Egypt peak in the evening. **Assumption:** 17:00–23:00 Cairo time carries 60% of the day's visits.
  - About 100,000 front-desk screens open at once at the peak, each showing the live queue.
  - Each clinic adds 1,500–4,000 patient files a year.
- Keep at least 2 years of operational history online. Medical records (visits, notes, prescriptions) are never deleted automatically. **Open:** the legal retention period for medical records in Egypt.

## 3. Users and roles

| Role | Who | Uses Hamada for |
|---|---|---|
| Owner | The doctor who owns the clinic. Exactly one per clinic | Everything, including team, permissions, prices and finances |
| Doctor | Associate doctors | Their queue, patient files, visit notes and prescriptions; more if granted |
| Assistant | Receptionists and assistants | Booking, the queue, payments, stock use; more if granted |
| Platform admin | Hamada staff | Creating clinics and owners, suspending clinics, support. No access to patient data |

- A person can belong to several clinics, with a different role in each. After signing in they pick a clinic, and everything they see belongs to that clinic.
- **Permissions are per person.** Each role is a template. The owner can grant or remove single permissions for one person. The owner always has everything, and only the owner can grant team management. Every permission change goes to the audit log.
- **Assumption:** doctors see all of the clinic's patients, as small clinics expect. A setting that limits a doctor to their own patients comes later.
- Patients don't use Hamada in the first release. Online booking by patients comes later, and the design leaves room for it.

## 4. Flows

### 4.1 Clinic setup

- Clinic profile: name, city, address, phone, time zone (default Africa/Cairo) and specialty.
- Services and prices per clinic, copied from the specialty's template when the clinic is created: name, kind (consultation, follow-up or procedure), price and duration.
- **Follow-up rule.** **Assumption** (draft 2, for Saged to confirm): a consultation booked within 14 days of the patient's last consultation with the same doctor that arrived or was paid uses the follow-up price. The days are counted from the new appointment's day. Each consultation service names its follow-up service, and a follow-up doesn't start a new window. The number of days is a clinic setting.
- **The business day.** Evening sessions often run past midnight, and in Ramadan many clinics work until 1–2 AM. Each clinic's day ends at a set hour. **Assumption:** 04:00. A visit after midnight belongs to the session it started in.
- Doctor schedules: weekly working hours per doctor, and a booking mode:
  - **queue**: numbered turns for the session, the common case in Egypt;
  - **slots**: fixed-length appointments at set times.
- A medicine list per clinic, copied from the specialty's template.

### 4.2 Patients

- One patient file per person per clinic: name, mobile phone, sex, date of birth or age, optional address, notes, allergies and chronic conditions.
- A file number per clinic, printed on receipts and prescriptions.
- Search by the first letters of any part of the name, or by phone or file number. Arabic-aware: the forms of alef, taa marbuta, alef maqsura and diacritics all match. Arabic-Indic digits are accepted.
- Booking a new name with a phone that already has a file in this clinic suggests that file. Families share phones, so it never blocks.
- Merging two files of the same person comes later.

### 4.3 Booking and the queue

- Book: the patient (an existing file or a new one), the doctor, the service, the day, and a time for doctors who use slots. The source is at the clinic, by phone, online or WhatsApp.
- Queue doctors: each booking gets the next turn number for that doctor and day. A number is never given twice.
- Slot doctors: one booking per slot. The front desk can add an "extra" booking to a full slot on purpose.
- Statuses: booked → arrived (waiting) → with the doctor → done. A booking can be cancelled before arrival, and marked no-show after its day. Only these changes are allowed. A double click, or two staff acting at once, changes the status once.
- Reschedule: move a booked appointment to another day, time or doctor.
- **Today** shows each doctor's queue: who is waiting, who is in, who is done, and the counts. Every screen in the clinic updates by itself within a few seconds of a change.
- Finishing a visit can collect the fee in the same step, or leave it owed.

### 4.4 Billing and payments

- Every visit has an invoice: the service at its price, extra procedures and materials, and a discount (with permission). Invoice numbers are sequential per clinic.
- Payment methods: cash, card, mobile wallet, InstaPay or other. Partial payments are allowed, and a visit can stay unpaid.
- A receipt prints on A5 or on an 80 mm receipt printer.
- Refunds and voids need a permission, and each one goes to the audit log.
- Later: a day closing per staff member, showing the cash each person collected.

### 4.5 Patient file and the doctor's work

- Per patient: visits, payments and prescriptions, newest first, a page at a time.
- Visit notes per visit: complaint, diagnosis and free-text notes. Specialty charts (teeth, growth, vision) come later.
- Prescription: pick from the clinic's medicine list or type a new medicine; set dose, amount and days; add instructions; print on A5 in Arabic or English. A printed prescription never changes. "Repeat the last prescription" takes one tap.
- Opening or printing a patient's file is recorded in the access log: who, when and which patient.

### 4.6 Inventory

- Items with a unit, pack, quantity, alert level and unit cost.
- A purchase adds stock and records an expense in one step.
- Use deducts stock for a patient or a visit. Stock never goes below zero, even when two people record the last unit at once.
- An adjustment after a count needs a reason and a permission, and goes to the audit log.
- Low-stock alerts on Today and Inventory.
- Usage report: what was used, for whom, when and at what cost, a page at a time.

### 4.7 Expenses and finances

- Expenses by category (rent, salaries, stock, electricity, other, plus the clinic's own), with a date and an amount.
- The month at a glance: collected, refunds, expenses by category, what is left, and revenue per doctor.
- Staff pay: a fixed monthly amount, or a percentage of what a doctor collected in the month.
- Only people with the finances permission see any of this.

### 4.8 Online consultations

- Requests with the patient, a reason and a time: accept, decline, start and end. This records the status only; real video calling comes later.

### 4.9 Team and settings

- Members: add a person (a new account, or an existing account by username), set the role, deactivate. Grant or remove single permissions.
- Change the specialty, services and prices, schedules, the medicine list and the clinic profile.

### 4.10 Accounts

- Sign in with a username or mobile number and a password. Owners also enter a code from an authenticator app.
- Change password. An account created by someone else must change its password at first sign-in.
- Later: self-service clinic sign-up, and password reset by SMS or email.

### 4.11 Platform admin

- Create a clinic with its owner, suspend or reactivate a clinic, and see usage counts. No access to patient data.

### 4.12 Reminders (later)

- An SMS or WhatsApp reminder the day before each booking. **Open:** which provider.

## 5. Non-functional requirements

- **Performance at the target scale:** p95 server time under 200 ms for Today, booking, status changes, finishing a visit and patient search. Every screen runs a fixed number of indexed queries, whatever the data size.
- **Availability:** 99.9% during clinic hours. Backups with point-in-time restore: lose at most 5 minutes of data, and restore within 2 hours.
- **Security and privacy:** this is health data.
  - Tenant isolation is enforced by the server and by the database.
  - Least privilege, enforced on the server.
  - An audit log for sensitive changes and an access log for reads of patient files.
  - Encryption in transit, and encrypted backups.
  - Egypt's Personal Data Protection Law (Law 151 of 2020) treats health data as sensitive. **Open:** a qualified legal review before real patients: consent, licensing, and where the data may be hosted.
- **Language:** Arabic (default, right to left) and English, chosen per user.
- **Devices:** current Chrome, Edge and Safari on Windows PCs, Android and iPad tablets and phones. It tolerates weak connections but is not offline-first.
- **Time and money:** a time zone per clinic, everything stored in UTC. Money in EGP as integer piastres.
- **Operable by a small team:** one deployable app, PostgreSQL as the only source of truth, and written runbooks.

## 6. Out of scope for the first release

Patient app and public online booking; real video calls; insurance and claims; lab and pharmacy integrations; e-invoicing with the Egyptian Tax Authority; payroll; specialty charts; currencies other than EGP; native mobile apps (the API is ready for them).

## 7. Open questions

1. Queue or slots as the default booking mode for a new doctor? **Assumption:** queue.
2. The follow-up price rule. **Assumption:** the follow-up price within 14 days.
3. Can a doctor see other doctors' patients? **Assumption:** yes.
4. The retention period for medical records.
5. Hosting location and the data protection licence.
6. The SMS or WhatsApp provider, for reminders and password reset.
7. Subscription plans and billing the clinics. Not in v2's scope.
8. Raised by the design review, all for Saged:
   - Is the follow-up rule in 4.1 right?
   - May the desk undo "arrived" and "called in" when tapped by mistake? (4.3 allows no other changes yet.)
   - May a visit be finished straight from "waiting", when the assistant skips "call in"?
   - At the end of the business day: booked patients who never came become no-shows. Patients still waiting are closed as "left without being seen", and visits still with the doctor are finished and flagged. Is that right?
   - Should a booking be marked no-show before the day ends, when the doctor's session is over?
