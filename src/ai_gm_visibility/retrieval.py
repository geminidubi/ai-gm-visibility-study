"""Receives already authorized facts only; no world/gold access or cache."""

import math
import re
from collections import Counter

SNAPSHOT_RANKERS = {"overlap_v01", "clause_bm25_v1", "all_authorized"}


class RetrievalError(RuntimeError):
    pass


def terms(text):
    # Chinese character bigrams plus Latin words; deterministic, not BM25.
    words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words


def retrieve(visible_facts, query, top_k=3):
    if type(top_k) is not int or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    query_terms = terms(query)
    ranked = []
    for fact in visible_facts:
        score = len(query_terms & terms(fact["content"]))
        score += 2 * len(query_terms & terms(" ".join(fact["entities"])))
        ranked.append((fact, score))
    ranked.sort(key=lambda item: (-item[1], item[0]["fact_id"]))
    return [{"fact": f, "score": score} for f, score in ranked[:top_k]]


def snapshot_terms(text):
    characters = re.findall(r"[\u3400-\u9fff]", text)
    return Counter(list(terms(text)) + characters)


def rank_snapshot(facts, query, method):
    """Order the authorized corpus using only current fact text and the question."""
    if method == "all_authorized":
        return [{"fact": fact, "score": None} for fact in facts]
    if method == "overlap_v01":
        def overlap(text):
            return terms(text) | set(re.findall(r"[\u3400-\u9fff]", text))

        query_terms = overlap(query)
        ranked = [{"fact": fact, "score": len(query_terms & overlap(fact["content"]))}
                  for fact in facts]
        return sorted(ranked, key=lambda row: (-row["score"], row["fact"]["fact_id"]))
    if method != "clause_bm25_v1":
        raise ValueError("Unknown snapshot ranker")
    if not facts:
        return []

    docs = [snapshot_terms(fact["content"]) for fact in facts]
    lengths = [sum(doc.values()) for doc in docs]
    mean_length = sum(lengths) / len(docs)
    frequency = Counter(token for doc in docs for token in doc)
    clauses = [set(snapshot_terms(text)) for text in re.split(r"[。！？?!]", query)]
    clauses = [clause for clause in clauses if clause]
    best = [0.0] * len(docs)
    for clause in clauses:
        weights = {token: math.log1p((len(docs) - frequency[token] + 0.5) /
                                    (frequency[token] + 0.5)) *
                   (0.35 if len(token) == 1 else 1.0) for token in sorted(clause)}
        scores = [
            sum(weight * doc[token] * 2.2 / (doc[token] + 1.2 * (0.25 + 0.75 * length / mean_length))
                for token, weight in weights.items() if token in doc)
            for doc, length in zip(docs, lengths)
        ]
        ceiling = max(scores)
        if ceiling:
            best = [max(previous, score / ceiling) for previous, score in zip(best, scores)]
    ranked = [{"fact": fact, "score": round(score, 10)} for fact, score in zip(facts, best)]
    return sorted(ranked, key=lambda row: (-row["score"], row["fact"]["fact_id"]))
