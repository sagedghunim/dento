# Instinct round 2 (2026-11-23): logic issues to fix

Booking fix confirmed on live: new booking (25 Nov, 12:00, Dr Ahmed Samy) and next visit (26 Nov, 17:00, Dr Hamada, "visit 3") both saved on patient «اختبار حجز Instinct», no crash.

## 1. "Due" vs "owed" look like the same number
- Steps: sign in as desk (كسارة), open patient «اختبار حجز Instinct» (file 24).
- Today screen: the visit row says «مطلوب 300 ج.م». Patient file, Visits: «باقي 300 ج.م» on a visit with no bill yet.
- Patient file, Account: total 0, paid 0, remaining 0.
- Problem: «باقي 300» (expected price of the visit) next to «الباقي 0» (billed debt). The math is right; the same word suggests the same number.
- Wanted: an unbilled amount reads «متوقع تحصيله» (expected), with a hint that it is not a recorded debt yet.

## 2. Free consultation line disappears
- Steps: Collect → Bill and payments → Lines, on a bill with a consultation at 0 and a paid procedure (earlier test: bill 102, consultation 0 + filling 700; the pay dialog showed only the filling).
- Wanted by Instinct: show «كشف: مجانًا», total still 700.
- CONFLICT: Part 10 hid a 0 line next to charged ones in the card, pay dialog and receipt on Saged's request. Ask Saged which he wants (suggestion: show it as «كشف: مجانًا» instead of hiding it).
- Instinct also notes the desk path does not apply the "free consultation with a procedure" rule; zeroing in its last test is unproven for the current setting.

## 3. Today row freezes after "Wait" (live, cause known)
- Steps: desk sends the patient in and presses «اطلب من الدكتور»; sign in as Dr Ahmed, open Today, press «استنى» and keep the mouse on the same row.
- Expected: the row shows at once «الدكتور قال استنى» and the «استدعاء» button comes back.
- Actual: the reply is saved, but the row keeps showing «الاستقبال بيسأل: يدخل؟» with «دخّله / استنى» for about 14 s, until the mouse leaves.
- Cause: today.js holds a row under the mouse or focus so it does not move under the user's hand.
- Fix: a row changed by the user's own action is never held; apply the server's result to it at once.

## Demo data side effect
- While testing, Instinct recorded a test collection of 300 EGP on bill 103 («تحصيل الآن» was ticked). The demo cash box is 300 higher, not real money. Reversing it needs the owner. Ask Saged.

## 4. File upload returns 500 on live (Instinct, 01:15-01:30)
- A valid PNG upload failed with 500 three times (desk and doctor). Error page leaked nothing.
- Known cause: no file storage on Vercel (read-only filesystem; `core/storage.py` uses local storage). See HANDOFF: "No x-rays (no file storage on Vercel)".
- Fix now: catch the storage failure and show a clear message ("رفع الملفات مش متاح في الديمو") instead of a 500, and hide or disable the upload button when storage is not configured. Later: real object storage (Vercel Blob / S3 / R2), needed for before/after photos anyway.
- Rejections already work: wrong type, over 10 MB, renamed files.

## Security results so far (desk + doctor, owner screens untested)
- Money: negative, zero and over-due amounts rejected; a repeated payment request did not double the payment; refund, cancel and price change without permission rejected.
- Doctor: payment, bills and debts refused per permissions; another doctor's visit read and close refused; payments and attachments across the other clinic refused.
- Not tested yet: PDF upload, downloading a saved file, isolation between two clinics on the same shard, owner screens (needs 2FA).
- No working exploit found.

## Demo data side effects (to undo with the owner)
- Bill 103: test collection 300 EGP (by mistake).
- Bill 104, patient «اختبار أمان Instinct» (file 27): deliberate test payment 50 EGP (total 300, remaining 250).
