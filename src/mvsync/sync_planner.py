"""Pure decision logic for the playback follow loop.

The planner is a small state machine: given the Spotify master clock and the
current mpv state, it returns the next action (pause, seek, or a playback-rate
nudge). No IO, no timers: the runtime shell in ``sync_runtime`` executes the
actions. This keeps the tricky boundary cases unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mvsync.timeline import Alignment

MIN_PLAYBACK_RATE = 0.8
MAX_PLAYBACK_RATE = 1.2


@dataclass(frozen=True)
class PlannerConfig:
    rate_gain: float = 0.05
    max_rate_delta: float = 0.05
    noop_tolerance_s: float = 0.04
    seek_threshold_s: float = 0.8
    end_guard_s: float = 0.5


@dataclass(frozen=True)
class SyncState:
    spotify_position_s: float
    spotify_playing: bool
    video_position_s: float
    video_paused: bool


@dataclass(frozen=True)
class SyncAction:
    pause_video: bool | None = None
    seek_video_to_s: float | None = None
    rate: float | None = None


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _clamp_rate(rate: float) -> float:
    return max(MIN_PLAYBACK_RATE, min(MAX_PLAYBACK_RATE, rate))


def target_video_s(alignment: Alignment, spotify_position_s: float, offset_s: float = 0.0) -> float:
    """Video position the alignment wants for a Spotify position.

    Targets landing inside a skip span snap to the span exit so the video is
    never parked in a region the planner would immediately skip again.
    """
    hold = alignment.hold_at(spotify_position_s, offset_s)
    if hold is not None:
        return hold.video_s
    target = alignment.spotify_to_video(spotify_position_s, offset_s)
    skip = alignment.skip_at(target)
    if skip is not None:
        target = skip.video_end_s
    return target


def plan(
    state: SyncState,
    alignment: Alignment,
    offset_s: float = 0.0,
    config: PlannerConfig = PlannerConfig(),  # noqa: B008 - frozen dataclass default is safe
) -> SyncAction:
    """Compute the next action for the follow loop."""
    if not state.spotify_playing:
        return SyncAction(pause_video=True)

    hold = alignment.hold_at(state.spotify_position_s, offset_s)
    if hold is not None:
        seek = None
        if abs(state.video_position_s - hold.video_s) > config.noop_tolerance_s:
            seek = hold.video_s
        return SyncAction(pause_video=True, seek_video_to_s=seek, rate=1.0)

    base_rate = _clamp_rate(alignment.rate_at(state.spotify_position_s, offset_s))
    target = target_video_s(alignment, state.spotify_position_s, offset_s)

    if target >= alignment.video_duration_s - config.end_guard_s:
        return SyncAction(pause_video=True, rate=1.0)

    skip = alignment.skip_at(state.video_position_s)
    if skip is not None and target >= skip.video_start_s:
        return SyncAction(seek_video_to_s=skip.video_end_s, pause_video=False, rate=1.0)

    if state.video_paused:
        return SyncAction(pause_video=False, seek_video_to_s=target, rate=1.0)

    error = target - state.video_position_s
    if abs(error) >= config.seek_threshold_s:
        return SyncAction(seek_video_to_s=target, rate=1.0)
    if abs(error) <= config.noop_tolerance_s:
        return SyncAction(rate=base_rate)
    rate = base_rate + _clamp(error * config.rate_gain, config.max_rate_delta)
    return SyncAction(rate=_clamp_rate(rate))


def with_offset(alignment: Alignment, offset_s: float) -> Alignment:
    """Return an alignment materializing ``plan(..., offset_s)``.

    The planner computes ``spotify_to_video(s + offset)``; shifting the anchor
    Spotify times back by the offset makes plain queries equivalent, so a user
    trim can be baked into a cached alignment.
    """
    anchors = [
        replace(anchor, spotify_s=anchor.spotify_s - offset_s) for anchor in alignment.anchors
    ]
    holds = [
        replace(
            hold,
            spotify_start_s=hold.spotify_start_s - offset_s,
            spotify_end_s=hold.spotify_end_s - offset_s,
        )
        for hold in alignment.holds
    ]
    return Alignment(
        video_id=alignment.video_id,
        track_id=alignment.track_id,
        video_duration_s=alignment.video_duration_s,
        track_duration_s=alignment.track_duration_s,
        anchors=anchors,
        skips=alignment.skips,
        holds=holds,
    )
