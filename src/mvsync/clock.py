"""Wall-clock helpers for the Spotify master clock.

SMTC timeline updates arrive infrequently (seconds apart), so the playback
position is advanced at real time between updates. Kept pure for testability.
"""

from __future__ import annotations

MAX_EXTRAPOLATION_S = 30.0


def extrapolate(
    position_s: float,
    updated_epoch_s: float,
    now_epoch_s: float,
    playing: bool,
    max_extrapolation_s: float = MAX_EXTRAPOLATION_S,
) -> float:
    """Advance a playback position captured at ``updated_epoch_s`` to ``now``.

    The advance is capped so a stale session cannot produce runaway values.
    """
    if not playing:
        return position_s
    delta = max(0.0, now_epoch_s - updated_epoch_s)
    return position_s + min(delta, max_extrapolation_s)
