# Frozen formal generation and review design

This public note summarizes the frozen 26 November 2026 design and the settings recovered from the executed requests. The [generation summary](../results/g5/approved-2026-11-26/generation-summary.json) records both original plan IDs and hashes of the frozen inputs.

## Generation settings

| Stage | Data and runs | Requested model and settings |
| --- | --- | --- |
| Dynamic permission replay (G4) | Nora world, eight turns, three architectures and two runs | GPT-6 Luna, `none`, 1,024 output tokens, overlap ranking, top three |
| Matched QA pilot (G5) | 12 development snapshots, three architectures and two reasoning conditions | GPT-6 Luna, `none` and `low`, 2,048 output tokens, clause-level BM25, top ten |
| Formal QA | 15 story families, 120 fixed snapshots, three architectures and three repeats | GPT-6 Luna, `low`, 2,048 output tokens, clause-level BM25, top ten |
| Post-ruling role-play | Three worlds, two engine ruling branches per world and three architectures | GPT-6 Luna, `low`, 2,048 output tokens, clause-level BM25, top ten |

All use a 60-second timeout, zero SDK retries and `store=false`. The formal benchmark uses audience-, source- and time-scoped history. Each response receives the recorded context for that snapshot. Earlier generated answers do not enter later prompts. [Formal request assembly and ranking](../src/ai_gm_visibility/g5_study.py) · [Observed token usage and duration](../results/hw5/experiment-usage.md)

## Frozen benchmark and sampling

- The [formal snapshots](../data/g5/formal_candidate_cases.jsonl), [gold references](../data/g5/formal_candidate_gold.jsonl) and [formal rubric](g5-formal-rubric.md) include the frozen clarifications for docktheatre F033, lanternferry F007, copperfeast F019 and winterqueue F047. The five affected pairs preserve the required fact IDs, permissions and metric denominators. Historical filenames retain the candidate label. The case, gold and rubric hashes match the executed plans.
- Each formal generation batch covers 40 questions from five families of one difficulty, one per secret type. Every batch contains both authorization states, all four query styles and all three architectures for one repeat index. [Recorded batch order](../results/g5/approved-2026-11-26/archive-provenance.json)
- The formal prespecified human sample has 45 answers, one per family and architecture. Seed `202611260518` balances authorization within families and repeats within architectures. Six additional independently reviewed answers were targeted for ambiguity. The [QA judgments](../results/g5/approved-2026-11-26/formal-qa-judgments.jsonl) preserve sample membership, source judgments, ten adjudications and two approved unresolved narrative fields.
- The RP sample uses seed `202611260519` for one answer per architecture, spanning three worlds and full and non-full success. The [RP judgments](../results/g5/approved-2026-11-26/rp-judgments.jsonl) preserve the three human ratings and the one adjudication. The handwritten CAL-07 judgment is a [separate calibration item](../results/g5/approved-2026-11-26/calibration-summary.json).
- The [pilot archive](../results/g5/pilot/) records the evidence for selecting `low`. Development results and role-play results use their own denominators.
