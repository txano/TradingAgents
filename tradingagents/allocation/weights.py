"""Scoring weights for the allocation manager.

Weights control how much each analysis bucket (beat, guidance, setup)
contributes to the weighted_score passed to the council. Defaults are 1.0
for all buckets. Over time, calibration runs can reveal which bucket is
most predictive and weights can be adjusted accordingly.

Stored at ~/.tradingagents/allocation_weights.json.
"""

import json
from pathlib import Path

_WEIGHTS_PATH = Path.home() / ".tradingagents" / "allocation_weights.json"

_DEFAULTS = {"beat": 1.0, "guidance": 1.0, "setup": 1.0}


def load_weights() -> dict:
    """Return current weights, falling back to defaults on any error."""
    if _WEIGHTS_PATH.exists():
        try:
            raw = json.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
            return {
                "beat":     float(raw.get("beat",     1.0)),
                "guidance": float(raw.get("guidance", 1.0)),
                "setup":    float(raw.get("setup",    1.0)),
            }
        except Exception:
            pass
    return _DEFAULTS.copy()


def save_weights(beat: float, guidance: float, setup: float) -> None:
    _WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WEIGHTS_PATH.write_text(
        json.dumps({"beat": beat, "guidance": guidance, "setup": setup}, indent=2),
        encoding="utf-8",
    )


def apply_weights(ctx: dict, weights: dict) -> dict:
    """Return a copy of ctx with weighted_score injected."""
    beat_w     = weights.get("beat",     1.0)
    guidance_w = weights.get("guidance", 1.0)
    setup_w    = weights.get("setup",    1.0)

    weighted = (
        beat_w     * ctx.get("beat_score",     0)
        + guidance_w * ctx.get("guidance_score", 0)
        + setup_w    * ctx.get("setup_score",    0)
    )
    return {
        **ctx,
        "weighted_score":    round(weighted, 2),
        "beat_weight":     beat_w,
        "guidance_weight": guidance_w,
        "setup_weight":    setup_w,
    }
