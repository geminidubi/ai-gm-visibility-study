# HW5 report guide

Use this index with the final English report, *Hidden Information Control for AI Game Masters in Partially Observable TRPG Environments*. Section, figure and table numbers follow that report. Reference **[8]** points to this research archive.

## Reference [8] locations

| Location | Evidence in this archive |
| --- | --- |
| **§3.2 · Generation pilot** (p. 4) | [Matched pilot outputs and judgments](../results/g5/pilot/) · [Pilot rubric](g5-rubric.md) · [Frozen design and generation settings](g5-approved-study-2026-11-26.md) · [Final QA rubric](g5-formal-rubric.md) |
| **Table 4 · Full stratified QA results** (p. 8) | [Complete strata, including additional metrics](../results/hw5/formal-strata.md) · [Machine-readable metrics by stratum, family and authorization](../results/g5/approved-2026-11-26/final-scoring-summary.json) |
| **Table 5 · Selected original Chinese answers** (p. 9) | [Exact case and repeat IDs](#table-5-original-answers) · [Readable questions, source checks and unabridged answers](../results/hw5/qa-representative-answers.md) |
| **§4.8 · RP-005** (p. 9) | [Teahouse branch with the original C2 answer and ruling](../results/hw5/rp-scenes.md#rp-005) · [Original narration and decision record](../results/hw5/rp-narrations.jsonl) · [All RP judgments](../results/g5/approved-2026-11-26/rp-judgments.jsonl) |

## Method and implementation

| Report section | Code and inputs |
| --- | --- |
| **§§2.1–2.2 · Canonical facts and actor visibility** | [World, case and reference schemas](../src/ai_gm_visibility/schemas.py) · [Grant state and visibility queries](../src/ai_gm_visibility/visibility.py) · [Fixed QA snapshots](../src/ai_gm_visibility/snapshot_cases.py) |
| **§2.3 · C0, C1 and C2** | [Architecture assemblers](../src/ai_gm_visibility/architectures.py) · [Common and condition-specific prompts](../prompts/) · [Figure 1](figures/architecture.svg) |
| **§2.4 · Validated events and transitions** | [Event validation and update](../src/ai_gm_visibility/events.py) · [Nora's world, trajectory and visibility references](../data/dev/) · [Figure 2](figures/permissions.svg) |
| **§§2.5–2.6 · Ranking, history and provenance** | [Lexical overlap and clause-level BM25](../src/ai_gm_visibility/retrieval.py) · [Audience, time and source checks](../src/ai_gm_visibility/snapshot_cases.py) · [Run and request records](../src/ai_gm_visibility/g5_records.py) |
| **§3.7 · Post-ruling role-play** | [Ruling and history gates](../src/ai_gm_visibility/g5_rp.py) · [Six cases and three worlds](../data/g5-rp-dev/v0.2/cases.json) · [References and rubric](../data/g5-rp-dev/v0.2/reference.json) · [Figure 4](figures/role-play.svg) |

## Experiment design and review

- **§3.1 and §4.2.** The eight-turn dynamic replay starts with one world and grants F003 at T3 and T7. See [the event-by-event replay](../results/hw5/g4-dynamic-demo.md), [both model runs](../results/g4/) and the [development config](../configs/dev-openai.json). The [offline command](reproduction.md#run-the-offline-checks) uses the evidence-echo adapter.
- **§3.2.** The pilot compares `none` and `low` across 36 matched question–architecture pairs and keeps the three initial connection failures separate. The [pilot index](../results/g5/pilot/) links snapshots, matched responses, judgments and sampling.
- **§§3.3–3.4.** Formal QA has 120 fixed snapshots and nine batches of 120 answers. Role-play has one answer per architecture for each of six ruling branches. The [frozen design](g5-approved-study-2026-11-26.md), [generation summary](../results/g5/approved-2026-11-26/generation-summary.json) and [request records](../results/g5/approved-2026-11-26/formal-qa-responses/) record the actual settings. The historical plan template [configs/g5-formal.json](../configs/g5-formal.json) contains a disabled planning example. The [formal and RP execution code](../src/ai_gm_visibility/g5_study.py) applies the approved low-reasoning settings.
- **§§3.5–3.6.** [QA rubric](g5-formal-rubric.md) · [RP rubric](../data/g5-rp-dev/v0.2/review/judge-rubric.md) · [Calibration gate and CAL-07 resolution](../results/g5/approved-2026-11-26/calibration-summary.json) · [QA judge batch settings](../results/g5/approved-2026-11-26/formal-review-batches.json) · [RP judge batch settings](../results/g5/approved-2026-11-26/rp-review-batches.json) · [Review, sampling and adjudication method](../results/hw5/study-methods.md).

## Figures and tables

| Report item | Source |
| --- | --- |
| **Fig. 1** · Three input architectures | [Vector figure](figures/architecture.svg) · [Input assembly code](../src/ai_gm_visibility/architectures.py) |
| **Fig. 2** · Dynamic read access | [Vector figure](figures/permissions.svg) · [Eight turns and permission state](../results/hw5/g4-dynamic-demo.md) · [Second replay event log](../results/g4/20261124T200304Z-b87d5232/events.jsonl) |
| **Fig. 3** · Formal QA evaluation | [Vector figure](figures/evaluation.svg) · [Judgments](../results/g5/approved-2026-11-26/formal-qa-judgments.jsonl) · [Scoring eligibility and aggregation](../src/ai_gm_visibility/g5_metrics.py) |
| **Fig. 4** · Narration after a ruling | [Vector figure](figures/role-play.svg) · [Cases, histories and rulings](../data/g5-rp-dev/v0.2/cases.json) |
| **Fig. 5** · QA tradeoff and 29 C2 failures | [Vector figure](figures/formal-results.svg) · [Formal QA counts](../results/g5/approved-2026-11-26/final-scoring-summary.json) · [All 29 audited answers](../results/hw5/c2-reveal-failures.jsonl) |
| **Table 1** · QA eligible denominators | [Metric applicability and denominators](g5-formal-rubric.md) · [Per-snapshot eligibility](../data/g5/formal_candidate_gold.jsonl) |
| **Table 2** · Prototype checks | [24 original pipeline tests](../tests/test_pipeline.py) · [Full offline suite](reproduction.md#run-the-offline-checks) |
| **Table 3** · Formal QA final counts | [Readable final denominators and totals](../results/g5/approved-2026-11-26/README.md#formal-qa) · [All original and adopted labels](../results/g5/approved-2026-11-26/formal-qa-judgments.jsonl) |
| **Table 4** · QA by difficulty, secret type and query style | [Full strata and eligibility](../results/hw5/formal-strata.md) · [Machine-readable strata](../results/g5/approved-2026-11-26/final-scoring-summary.json) |
| **Table 5** · Representative answers | [Case IDs and original texts](#table-5-original-answers) |
| **Table 6** · Tokens and generation time | [Recorded usage and duration](../results/hw5/experiment-usage.md) · [Companion token illustration](figures/input-tokens.svg) · [Usage aggregates](../results/g5/approved-2026-11-26/generation-summary.json) |
| **Table 7** · RP results | [RP aggregate counts](../results/g5/approved-2026-11-26/README.md#role-play) · [Per-response labels and evidence](../results/g5/approved-2026-11-26/rp-judgments.jsonl) |

## Table 5 original answers

| Setting | Case and repeat | Original Chinese answer and source evidence |
| --- | --- | --- |
| Moss Lantern Harbor, unauthorized | `G5-724cc7f7db32`, 1 | [C0, C1 and C2](../results/hw5/qa-representative-answers.md#g5-724cc7f7db32) |
| Moss Lantern Harbor, authorized | `G5-7354f5d30832`, 1 | [C0, C1 and C2](../results/hw5/qa-representative-answers.md#g5-7354f5d30832) |
| Split-Tide Theater Dock, authorized | `G5-c3ba2adb3f60`, 1 | [C2 answer and F033 at rank 65](../results/hw5/qa-representative-answers.md#g5-c3ba2adb3f60) |
| Private commission, authorized | `G5-066150f53b96`, 2 | [C2 answer and retrieved F008](../results/hw5/qa-representative-answers.md#g5-066150f53b96) |

The [case JSONL](../results/hw5/qa-representative-answers.jsonl) adds batch IDs, request numbers and response hashes for joining these examples to the [complete generation batches](../results/g5/approved-2026-11-26/formal-qa-responses/).

## Additional diagnostics and reproduction

- **§4.6.** [C2 audit narrative](../results/hw5/c2-reveal-audit.md) and [all 29 failed disclosures](../results/hw5/c2-reveal-failures.jsonl) distinguish the 21 missing-input records from the eight records with the focal secret in the input.
- **§4.8.** [RP-005 through RP-006](../results/hw5/rp-scenes.md#竹雨茶舍留言板) compares two supplied branches. For `RP-005`, C2 preserves player B's private assignment and correctly states player A's commission. The ruling places B to the side, while C2's narration places the attendant there. The final label records a contradiction and unsupported event.
- **§§4.1 and 4.9.** [Run the existing tests, rebuild inputs and recompute recorded results offline](reproduction.md). [Research and publication provenance](provenance.md) describes the different levels of byte verification.
