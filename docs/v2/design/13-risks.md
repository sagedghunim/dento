## 13. Risks

- **A very large clinic** (30+ doctors) changes its version every few seconds at the peak, so its screens fetch full fragments often. Mitigation if needed: per-doctor versions (an added column).
- **Cross-database flows** can stop halfway. Each step is idempotent, and a nightly reconciliation repairs them.
- **Moving a clinic** needs a short freeze. It runs in the early-morning window, and M5 rehearses it.
- **RLS mistakes:** a missing policy or a query that loses its index range. Covered by the policy test and the M1 plan check.
- **Clinic PCs with wrong clocks or old browsers:** the server decides the business day and formats times; the browser floor is stated.
- **The data protection review** could change hosting or consent flows.
