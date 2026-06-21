"""Small helpers shared across the allocation package."""

import json
import re


def cut(text: str, max_chars: int) -> str:
    """Truncate at the last sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    last = max(chunk.rfind("."), chunk.rfind("!"), chunk.rfind("?"))
    if last > max_chars // 2:
        return chunk[: last + 1].strip()
    return chunk.strip() + "…"


def parse_allocation(report: str) -> dict:
    """Extract the JSON allocation block from the report.

    Prefers the block under the '### Allocation Score' heading; falls back to
    scanning all ```json blocks from the last one backwards, so a stray JSON
    snippet earlier in the report can't be picked up by mistake.
    """
    candidates = []
    anchored = re.search(
        r"###\s*Allocation Score\s*```json\s*(\{.*?\})\s*```", report, re.DOTALL
    )
    if anchored:
        candidates.append(anchored.group(1))
    candidates.extend(reversed(re.findall(r"```json\s*(\{.*?\})\s*```", report, re.DOTALL)))

    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    return {}
