# Field-operations agents: design notes, diagrams and test evidence

Nine AI systems built as independent engineering projects around field-service work (sales, scheduling, permits, follow-up, safety, verification) plus two tools built for the day job. What each does and why is on [rajranpariya.com](https://rajranpariya.com); this repo holds the architecture diagrams, the design decisions and the real verification harnesses with product-specific names generalized. The product code is private by design.

| Project | Folder | What is here |
|---|---|---|
| Voice to structured action | [voice-to-action/](voice-to-action/) | No product code here by design; the harnesses that verify this path are in ../evals. |
| Scheduling and work orders | [scheduling-workorders/](scheduling-workorders/) | Design notes only. |
| Permit and inspection lifecycle | [permit-lifecycle/](permit-lifecycle/) | Design notes only. |
| Post-job follow-up: accounting, reviews, referrals | [post-job-followup/](post-job-followup/) | Design notes only. |
| Safety layers for unattended operation | [safety-layer/](safety-layer/) | excerpt of the real test suite (29 of 119 tests) |
| Verification and evals | [evals/](evals/) | fleet_smoke_test.py and voice_regression_harness.py are the real harnesses with product-specific names generalized. |
| Comparison and presentation tool for the sales conversation | [comparison-presentation-tool/](comparison-presentation-tool/) | Design notes only; the file carries manufacturer material and is not published. |
| Field app with a 3D crew packet | [field-app/](field-app/) | Design notes only; the app belongs to the day job. |
| Backorder Buster | [backorder-buster/](backorder-buster/) | Design notes only. |

Built by directing AI to implement, then verifying against the running system rather than trusting a report that says it works.
