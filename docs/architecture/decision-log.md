# Decision Log

Capture major decisions that affect model outcomes.

Template:
- Date:
- Decision:
- Context:
- Options considered:
- Chosen option:
- Impact:
- Follow-up:

---

- Date: 2026-02-26
- Decision: Normalise Azure Retail Prices API RI/SP prices from total-for-term to hourly
- Context: The API returns RI prices as total cost for the full 3-year term (e.g., $20,292) but misleadingly labels them with `unitOfMeasure: "1 Hour"`. This caused savings to appear as a flat 40%/30% instead of realistic values (41–63%).
- Options considered: (1) Trust the API unitOfMeasure label, (2) Detect term-based pricing and normalise by dividing by 26,280 hours (3yr × 8,760 hrs/yr)
- Chosen option: Option 2 — divide by 26,280 hours for 3-year terms, 8,760 for 1-year terms
- Impact: Savings estimates now reflect actual Azure RI/SP discounts per SKU rather than a uniform percentage
- Follow-up: Monitor for API changes; unit test with known SKU pricing

---

- Date: 2026-02-26
- Decision: Add disclaimer to all analysis output
- Context: Savings estimates are approximations based on public retail pricing. Users should not treat them as guaranteed savings without validation from their Microsoft account team.
- Options considered: (1) Disclaimer in README only, (2) Disclaimer in app UI and Excel output, (3) Both
- Chosen option: Option 3 — disclaimer in app sidebar, page footer, Excel Executive Summary, Strategy Comparison, and all savings sheets
- Impact: Users are clearly informed that estimates are demonstrative only
- Follow-up: None

---

- Date: 2026-02-26
- Decision: Document comprehensive RBAC requirements in all READMEs
- Context: Customer log analysis revealed that most Azure API data retrieval failures are caused by missing RBAC roles. The Reservations API silently returns empty data (no error) while Savings Plans returns 403.
- Options considered: (1) Minimal docs (just "Cost Management Reader"), (2) Full role × scope matrix
- Chosen option: Option 2 — documented all four required roles (Reader, Cost Management Reader, Reservations Reader, Savings Plan Reader) with their correct scopes and assignment instructions
- Impact: Customers can self-service RBAC setup without a support email
- Follow-up: Consider adding an in-app RBAC diagnostic check
