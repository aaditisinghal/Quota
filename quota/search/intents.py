"""What an off-contract promise sounds like, one intent per kind (ELASTIC_TASKS E3). The sweep and the payout
gate run every intent and merge the results by best rank."""

INTENTS = {
    "early_exit": "customer can cancel, terminate or walk away early without penalty or with a refund",
    "free_period": "free months, months on the house, or service not billed",
    "credit": "a credit, discount or concession against an invoice",
    "price_lock": "price will not increase at renewal or is locked for years",
    "payment_terms": "pay later than normal terms, extended or delayed payment",
    "free_addon": "an extra product or module provided for free or not billed",
}


def merge(results: dict[str, list[dict]]) -> list[dict]:
    """Hits from several intents, one per doc, at the best rank any intent gave it (ties by score)."""
    best: dict[str, dict] = {}
    for intent, hits in results.items():
        for hit in hits:
            seen = best.get(hit["doc_id"])
            if seen is None or (hit["rank"], -hit["score"]) < (seen["rank"], -seen["score"]):
                best[hit["doc_id"]] = {**hit, "intent": intent}
    return sorted(best.values(), key=lambda h: (h["rank"], -h["score"], h["doc_id"]))
