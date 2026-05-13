"""Parse the machine-readable score block from an earnings brief."""

import json
import re


def parse_score(brief: str) -> dict:
    """Extract the JSON score block from an earnings brief.

    Returns a dict with keys: earnings_date, beat_score, guidance_score,
    setup_score, total_score, signal, confidence, one_liner.
    Returns an empty dict if the block is missing or malformed.
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", brief, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}
