"""Compare recorded G5 inputs with the reconstructed snapshot and dispatch payload."""

from .llm.openai_backend import MODEL, response_parameters
from .logging_io import digest
from .schemas import require

RECORD_VERSION = "g5-request-v1"
PACKET_FIELDS = (
    "allowed_candidate_ids", "selected_fact_ids", "input_source_fact_ids",
    "candidate_fact_ids", "history_ids", "ranked_facts", "retrieval_status",
)


def dispatch_payload(config, packet):
    if config["provider"] == "openai":
        return {"model": MODEL, "input": packet["messages"], **response_parameters(config["openai"])}
    return packet


def reconcile_attempt(row, config, case, packet, version):
    require(all(field in row and row[field] == case[field] for field in
                ("actor_id", "as_of", "dataset_version")), "Attempt snapshot identity mismatch")
    require(row.get("visible_fact_ids") == packet["allowed_candidate_ids"], "Attempt visible facts mismatch")
    fields = (*PACKET_FIELDS, "rendered_messages")
    if version == RECORD_VERSION:
        fields += ("history_sources", "generation_request", "generation_request_sha256")
    for field in fields:
        require(field in row, f"Missing attempt evidence: {field}")

    if row["retrieval_status"] == "failed":
        require(row["architecture"] == "C2" and row["status"] == "error"
                and row.get("error_type") == "retrieval_failure" and row.get("retrieval_error_type")
                and row.get("response_text") is None and row.get("response_metadata") is None
                and row.get("returned_model") is None
                and all(row[field] is None for field in fields if field != "retrieval_status"),
                "Retrieval failure has unexpected evidence or dispatch")
        require(config["provider"] == "mock", "Paid retrieval failure should stop before dispatch")
        return

    for field in PACKET_FIELDS:
        require(digest(row[field]) == digest(packet[field]), f"Attempt evidence mismatch: {field}")
    require(row["rendered_messages"] == packet["messages"], "Attempt evidence mismatch: rendered_messages")
    if version == RECORD_VERSION:
        require(row["history_sources"] == packet["history_sources"], "Attempt evidence mismatch: history_sources")
        payload = dispatch_payload(config, packet)
        require(digest(row["generation_request"]) == row["generation_request_sha256"] == digest(payload),
                "Attempt evidence mismatch: generation_request")
        if config["provider"] == "openai":
            require(row.get("requested_model") == MODEL
                    and row.get("parameter_effective_values") == response_parameters(config["openai"])
                    and type(row.get("g5_request_number")) is int and row["g5_request_number"] > 0,
                    "Paid attempt metadata mismatch")
