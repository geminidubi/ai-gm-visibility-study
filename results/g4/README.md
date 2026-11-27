# Dynamic permission replay

Two eight-turn runs of the same Nora world cover C0, C1 and C2, yielding 48 responses. Each run uses GPT-6 Luna, reasoning `none` and a 1,024-token output limit. The event trace illustrates reading, active-player switching, validated sharing and duplicate event handling. Formal semantic assessment is `NOT_RUN`.

| Run | Evidence |
| --- | --- |
| First | [Readable comparison](20261124T200154Z-b6f043b4/comparison.md) · [24 answers](20261124T200154Z-b6f043b4/records.jsonl) · [Events](20261124T200154Z-b6f043b4/events.jsonl) |
| Second | [Readable comparison](20261124T200304Z-b87d5232/comparison.md) · [24 answers](20261124T200304Z-b87d5232/records.jsonl) · [Events](20261124T200304Z-b87d5232/events.jsonl) |

The original worktree was dirty at generation time, as recorded in the run manifests. The public response files include exact prompts, answers, visibility and source identifiers, and measured usage. [Original archive byte checksums](raw-sha256.json) · [Eight-turn walkthrough](../hw5/g4-dynamic-demo.md) · [Public archive provenance](../../docs/provenance.md)
