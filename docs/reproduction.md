# Reproduce the checks offline

Run the commands from the repository root with Python 3.12 and the dependencies in [uv.lock](../uv.lock). `uv sync --locked` installs the project and may use the package registry the first time. The checks after installation use existing evidence or synthetic fixtures and require no credentials.

## Run the offline checks

```sh
uv sync --locked
uv run --no-sync python -m unittest discover -s tests -p test_pipeline.py
uv run --no-sync python -m unittest discover -s tests
uv run --no-sync ai-gm check-data
uv run --no-sync ai-gm run --all --output-root runs/public-offline
uv run --no-sync ai-gm g5 validate
uv run --no-sync python scripts/check_g5_rp_v02.py --output runs/public-rp-inputs
uv run --no-sync python scripts/verify_public_archive.py
```

The first test command selects the 24 prototype tests in report Table 2. The full suite also covers the snapshot pipeline, scoring, replay, OpenAI client behavior with test doubles, and the separate study workflow. The mock replay produces 24 synthetic engineering records. The RP checker rebuilds 18 model inputs. Choose a fresh RP output directory for a second run.

## Recompute the published metrics

The [public verifier](../scripts/verify_public_archive.py) checks public SHA-256 values, reconstructs recorded requests and blinded packet hashes, joins the original adopted labels and frozen gold by `case_id`, `architecture` and `repeat_id`, and recomputes the formal strata with the original aggregation code. It checks the 29 C2 diagnoses, role-play metrics, matched pilot and observed token use. The independent human and adjudication records remain available alongside the primary judgments. [Metric implementation](../src/ai_gm_visibility/g5_metrics.py) · [Final scoring summary](../results/g5/approved-2026-11-26/final-scoring-summary.json)

C0 and C1 have no retrieval-ranking stage. C2's pooled hits count relevant authorized facts. Its macro recall averages individual answer recalls. Narrative consistency percentages use decided fields and report unresolved cases separately. QA and RP use their own rubrics and denominators. [QA rubric](g5-formal-rubric.md) · [RP rubric](../data/g5-rp-dev/v0.2/review/judge-rubric.md)

## Preview a fresh generation plan

The following preflight commands assemble inputs and calculate a fresh plan locally. They make zero model requests.

```sh
uv run --no-sync ai-gm g5 study formal plan --effort low
uv run --no-sync ai-gm g5 study rp plan --effort low
```

A fresh paid run requires an account with access to the requested model, a local `OPENAI_API_KEY`, a separate ledger and explicit approval for the new plan ID. The CLI accepts an approval JSON with `approved`, `scope`, `plan_id`, `approved_by` and `pilot_assessment`. Formal QA also requires `formal_dataset_and_rubric_decisions`. The paid entry point is `ai-gm g5 study <formal|rp> run` with the chosen `--effort`, `--authorize-paid` and `--approval` arguments. The recorded design dispatched 120 QA answers per batch and all 18 RP narrations in one batch. [Generation settings](g5-approved-study-2026-11-26.md) · [Approval and dispatch checks](../src/ai_gm_visibility/g5_study.py)

The historical export and orchestration scripts use private run directories and approvals. The public verifier works from checkout files. A new plan uses the current file fingerprints and any future model call can differ from the archived response. Save any fresh run under a separate output path.
