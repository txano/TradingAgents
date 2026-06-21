"""Scoring weights for the allocation manager.

Weights control how much each analysis bucket (beat, guidance, setup,
fundamentals) contributes to the weighted_score passed to the council.
Defaults: beat=0.7, guidance=1.0, setup=1.0, fundamentals=1.5 — business
quality is weighted highest, a single quarter's beat lowest. Over time,
calibration runs can reveal which bucket is most predictive and weights
can be adjusted accordingly via `tradingagents weights`.

Stored at ~/.tradingagents/allocation_weights.json.
"""

import json
from pathlib import Path

_WEIGHTS_PATH = Path.home() / ".tradingagents" / "allocation_weights.json"

DEFAULTS = {"beat": 0.7, "guidance": 1.0, "setup": 1.0, "fundamentals": 1.5}

_DEFAULTS = DEFAULTS  # backward-compat alias


def load_weights() -> dict:
    """Return current weights, falling back to defaults on any error."""
    if _WEIGHTS_PATH.exists():
        try:
            raw = json.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
            return {k: float(raw.get(k, default)) for k, default in DEFAULTS.items()}
        except Exception:
            pass
    return DEFAULTS.copy()


def save_weights(beat: float, guidance: float, setup: float, fundamentals: float = DEFAULTS["fundamentals"]) -> None:
    _WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WEIGHTS_PATH.write_text(
        json.dumps(
            {"beat": beat, "guidance": guidance, "setup": setup, "fundamentals": fundamentals},
            indent=2,
        ),
        encoding="utf-8",
    )


def apply_weights(ctx: dict, weights: dict) -> dict:
    """Return a copy of ctx with weighted_score injected.

    weighted_score now includes fundamentals as a 4th term so business quality
    is baked into sizing decisions, not just used as a post-hoc modifier.
    """
    beat_w         = weights.get("beat",         DEFAULTS["beat"])
    guidance_w     = weights.get("guidance",     DEFAULTS["guidance"])
    setup_w        = weights.get("setup",        DEFAULTS["setup"])
    fundamentals_w = weights.get("fundamentals", DEFAULTS["fundamentals"])

    weighted = (
        beat_w         * ctx.get("beat_score",         0)
        + guidance_w   * ctx.get("guidance_score",     0)
        + setup_w      * ctx.get("setup_score",        0)
        + fundamentals_w * ctx.get("fundamentals_score", 0)
    )
    return {
        **ctx,
        "weighted_score":      round(weighted, 2),
        "beat_weight":         beat_w,
        "guidance_weight":     guidance_w,
        "setup_weight":        setup_w,
        "fundamentals_weight": fundamentals_w,
    }
