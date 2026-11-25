## 2. Scale model

Assumptions from R2: 250,000 doctors, 15 visits per doctor per working day, 26 working days a month: about 100M visits a month.

| Table | Rows per month at the target | 2 years | Notes |
|---|---|---|---|
| appointment | 110M | 2.6B | 1.1 per visit, with cancellations and no-shows |
| invoice | 100M | 2.4B | |
| invoice_line | 130M | 3.1B | |
| payment | 105M | 2.5B | refunds included |
| patient | 40M new files | 1B | |
| visit_note (+ revisions) | 60M | 1.4B | |
| prescription / items | 50M / 125M | 1.2B / 3B | |
| inventory_movement | 50M | 1.2B | |
| audit_access | 200–280M | 24 months online | partitioned, about 220 B a row |
| audit_change | about 3M | kept with the financial records | |
| doctor_day / clinic_day | 6.5M / 4M | 156M / 96M | |
| expense | 3M | 72M | |
| core_request_key | about 30M a day | about 90M live (72 hours) | about 18 GB live, spread over the shards |
| account_event (directory) | 15M+ | 12 months | sign-ins and account changes |
| procrastinate jobs (directory) | small until M4 | | reminders in M4 add about 110M a month; cleanup planned with them |

- **Storage:** plan for about **10 TB after 2 years** (the database review's measured bytes per row, with organic index growth), and 4–5 TB more each year, since nothing clinical is deleted. At 1,000 doctors: about 40 GB after 2 years.
- **Peak write rate:** the evening (17:00–23:00 Cairo time) carries 60% of the day: about 105 visits a second. **Assumption:** the busiest hour is 1.5 times the evening average, so design for 160–210 visits a second and 1,100–1,500 write transactions a second, each 3–10 statements, all by key.
- **Read rate:** 100,000 screens polling every 5 s while visible is 20,000 requests a second, plus about 8% from hidden tabs. About 98% answer 304 (an average clinic changes its queue about 15 times an hour at the peak, against 720 polls an hour per screen). Searches: about 150 a second after debouncing; file opens: about 300 a second.
- **The biggest clinic:** 30 doctors, about 600 visits a day, 100,000+ files within 2 years. Its `clinic_day` version changes every 5–10 s at the peak, so its screens fetch a full queue more often (per-doctor versions are a deferred option, 13).
- **What forces a split:** not throughput but **the 2-hour restore** (R5). A shard is kept small enough that its restore drill passes. At about 10 TB that is roughly 10 shards after 2 years.

These are estimates. M5 checks them with synthetic data and a load test (10).
