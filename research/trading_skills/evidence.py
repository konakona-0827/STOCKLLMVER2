"""Point-in-time news/fundamental adapters; absence is explicitly unknown."""
from .market import instant


def available_evidence(records, at):
    cutoff, accepted, rejected = instant(at), [], []
    for row in records:
        try:
            published, received = instant(row["published_at"]), instant(row["received_at"])
            if not published <= received <= cutoff:
                raise ValueError("NOT_KNOWN_AT_DECISION")
            if not row.get("evidence_id") or not row.get("source_url", "").startswith("https://"):
                raise ValueError("SOURCE_AND_ID_REQUIRED")
            accepted.append(dict(row))
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append(dict(evidence_id=row.get("evidence_id"), reason=str(exc)))
    # Last revision actually known at t wins; later restatements stay excluded.
    revisions = {}
    news = []
    for row in accepted:
        if row.get("kind") == "fundamental":
            key = (row["symbol"], row["metric"], row["period_end"])
            if key not in revisions or instant(row["published_at"]) > instant(revisions[key]["published_at"]):
                revisions[key] = row
        else:
            news.append(row)
    return dict(records=news + list(revisions.values()), rejected=rejected,
                completeness="UNKNOWN_UNLESS_SEPARATELY_ATTESTED", missing_data=[] if accepted else ["NO_VERIFIED_NEWS_OR_FUNDAMENTALS"])
