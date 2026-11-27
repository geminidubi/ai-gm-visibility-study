# Matched generation pilot

The pilot compared `none` and `low` reasoning on 12 development snapshots across C0, C1 and C2. There are 36 matched pairs and 72 valid GPT-6 Luna answers. Three earlier connection failures remain separately recorded.

| Setting | Model input, result and retrieval sources | Primary and human judgments |
| --- | --- | --- |
| None | [36 valid responses](none-responses.jsonl) | [36 judgments](none-judgments.jsonl) |
| Low | [36 valid responses](low-responses.jsonl) | [36 judgments](low-judgments.jsonl) |
| Initial failures | [Three connection attempts](connection-failures-responses.jsonl) |  |

The [12 development snapshots](../../../data/g5/dev_cases.jsonl) and [gold references](../../../data/g5/dev_gold.jsonl) use the [pilot rubric](../../../docs/g5-rubric.md). All valid answers were scored blind in six batches with GPT-6.2 Astra launched at high. Twelve independently reviewed examples agreed on semantic decisions, evidence fact IDs and leakage type.

Among 18 leakage-eligible pairs, direct leakage fell from 8 under `none` to 4 under `low`. Four pairs improved and zero regressed. In both settings, correct disclosure was 17/18 and narrative consistency 35/36. One C2 pair regressed on both despite unchanged aggregate rates. The prespecified priority on confidentiality favored `low`. [Generation settings, tokens and time](../../hw5/experiment-usage.md) · [Frozen formal plan](../../../docs/g5-approved-study-2026-11-26.md)
