# Research and publication provenance

| Material | Recorded source |
| --- | --- |
| Final research baseline | `7360e44125bc187790ff0ad58550d843b037a367`, the finalized HW5 evidence and reporting snapshot |
| Formal QA and RP generation | `5984c28cbfb79f270a190b472226998097f75d08`, as recorded by all ten sealed manifests |
| Matched pilot | Local three-batch archive with separate initial failures, two completed runs and their finalized human-review comparison |
| Dynamic replay | Archived G4 manifests, including the original code commit, dirty-worktree flag and source file hashes |
| Figures | Standalone SVGs from the final HW5 figure package. Figures 1–5 are cross-referenced in the [report guide](report-guide.md). The input-token illustration is a companion to Table 6. |

Apart from the private handoff URL removed from the [historical orchestration script](../scripts/run_g5_approved.py), the formal implementation, frozen cases, gold, prompts and rubrics retain their baseline bytes. The executed plans match the published frozen data and rubric SHA-256s. The URL edit changes fresh plan fingerprints because the plan hashes the historical script. The original plan IDs remain in the [generation summary](../results/g5/approved-2026-11-26/generation-summary.json). Candidate filenames are retained so existing code and archived references resolve. The development manifest under `results/g5/v0.2/` records an earlier candidate release. Its checksums document that release.

The final report and any document containing its student information stay with the submission. The standalone figures accompany the independent public repository. [Source attribution](sources.md)

## Public field selection

| Source | Retained in the public copy | Removed or represented by a public identifier |
| --- | --- | --- |
| Successful and failed generation records | Case, world, family, pair, actor and repeat IDs, original response text and rendered messages, dispatch payload and its SHA-256, requested and reported model, settings, source fact and history IDs, full ranking, RP ruling source IDs, provider status, completion reason, output parts, error class, retries, tokens and latency | Provider response and request IDs, estimated, cumulative and reserved cost fields. Original run ID and request order are published under `source.batch_id` and `source.study_request_number`. |
| Run manifests and review snapshots | Original commit, times, stage settings, archived plan IDs, original file and snapshot digests, per-batch identity and time interval | Local absolute paths, ledger and billing totals, authorizations and machine-specific fields. Public provenance uses batch IDs with repository-relative paths in the documented archive layout. Historical executable configuration keeps its generic budget guardrails. |
| Blind judgments | Anonymous IDs and packet hashes, rubric version, original primary and independent human judgments, adjudications, adopted labels, quoted evidence, reasons, review flags, sample membership, disagreements and approved unresolved fields | Reviewer and orchestration identifiers, local import or output paths, timestamps for internal import operations, private keys and raw workspace receipts. The original blinded packet content is reconstructible from public inputs and IDs. |
| Primary reviewer batches | Package and batch IDs, accepted anonymous IDs, counts and source checksums. Reported model and reasoning launch settings remain linked to each judgment. The formal and RP records also retain the explicit unknown status of independently verified runtime details. | Agent names, workspace/task identifiers, reader and writer paths, and workflow messages. |
| Human audit and adjudication | Anonymous independent ratings, evidence, decision reasons, first-pass versus targeted sample fields, type of adjudication and disposition, including whether AI assistance was human-approved | Personal identifiers, original approval messages and internal attachments. Relevant source receipt checksums are recorded separately. |
| Documents | Original HW5 result tables, case transcripts, diagnostic records, timing and usage. Public navigation summarizes the frozen design and names each historical stage. | Internal handoffs, superseded process notes, detailed cost tables, private links, report PDF, credentials, installations, raw recordings and video production files. |

The [original archive digests](../results/g5/approved-2026-11-26/archive-provenance.json) are for the private source records. The [public file digest manifest](../results/g5/approved-2026-11-26/public-sha256.json) verifies the independent public files. The unchanged response text has its own UTF-8 digest. Public verification rebuilds the dispatch payload and blinding packet from the frozen data, so their hashes remain independently checkable. The byte-level checks against the original full records and private review receipts require the source archives. [Run the verifier](reproduction.md#recompute-the-published-metrics)
