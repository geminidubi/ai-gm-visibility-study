# Final formal QA and role-play results

QA and role-play were generated on 26 November 2026 with GPT-6 Luna, low reasoning, a 2,048-output-token cap, a 60-second timeout, zero SDK retries and `store=false`. All 1,080 QA requests and all 18 RP requests succeeded. [Generation settings and token totals](generation-summary.json) · [Run and archive hashes](archive-provenance.json)

## Formal QA

| Outcome | C0 | C1 | C2 | All |
| --- | ---: | ---: | ---: | ---: |
| Direct leakage | 120/180 | 11/180 | 0/180 | 131/540 |
| Implicit-only leakage | 0/180 | 0/180 | 0/180 | 0/540 |
| Correct disclosure | 161/180 | 174/180 | 151/180 | 486/540 |
| Over-withholding | 17/360 | 6/360 | 26/360 | 49/1,080 |
| Cross-player leakage | 6/36 | 1/36 | 0/36 | 7/108 |
| Narrative consistent | 349/359 | 356/360 | 340/359 | 1,045/1,078 |
| Narrative inconsistent | 10/359 | 4/360 | 19/359 | 33/1,078 |
| Narrative unresolved | 1/360 | 0/360 | 1/360 | 2/1,080 |
| All required facts correct | 341/360 | 354/360 | 331/360 | 1,026/1,080 |
| Relevant authorized fact hits | — | — | 519/540 | — |

C2 mean per-answer retrieval recall was 97.08%. The two approved unresolved narrative fields concern a handoff's timing and a platform's placement or attribution. The final decision records include 51 independently reviewed answers, ten adjudications and the retained primary and human judgments. [Original answers in nine batch files](formal-qa-responses/) · [QA judgments](formal-qa-judgments.jsonl) · [All strata](../../hw5/formal-strata.md)

## Role-play

| Outcome | C0 | C1 | C2 | All |
| --- | ---: | ---: | ---: | ---: |
| Ruling fidelity | 6/6 | 6/6 | 5/6 | 17/18 |
| Narrative consistent | 6/6 | 6/6 | 5/6 | 17/18 |
| Leakage | 0/6 | 0/6 | 0/6 | 0/18 |
| Correct disclosure | 2/2 | 2/2 | 2/2 | 6/6 |
| Over-withholding | 1/6 | 1/6 | 2/6 | 4/18 |
| Required background hits | — | — | 6/6 | — |

[All 18 responses and input records](rp-responses.jsonl) · [RP judgments](rp-judgments.jsonl) · [Readable narrations and engine rulings](../../hw5/rp-scenes.md)

## Review records

[Original final scoring summary](final-scoring-summary.json) · [QA primary-review batch settings](formal-review-batches.json) · [RP primary-review batch settings](rp-review-batches.json) · [23 calibration packets and judgments](calibration.jsonl) · [Calibration comparison and CAL-07](calibration-summary.json)
