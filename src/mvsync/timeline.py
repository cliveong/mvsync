"""Pure timeline mapping between a music video and a Spotify track.

An ``Alignment`` maps points of the video timeline to the Spotify track
timeline. It is stored as anchors ``(video_s, spotify_s)`` sampled from the
DSP alignment, plus skip spans: video regions with no counterpart in the track
(intros, story breaks, outros). Holds are the opposite: Spotify regions with
no counterpart in the video, where the video stays on one frame (a cut or a
missing outro). Anchors are grouped into segments wherever a skip lies between
two consecutive anchors; holds split a segment into pre-hold, a rate-0 hold,
and post-hold pieces. Within a segment the mapping is linear with a rate close
to 1.0 (exactly 1.0 when the MV uses the album master, different when the MV
is a slightly different mix).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import median


@dataclass(frozen=True)
class Anchor:
    video_s: float
    spotify_s: float


@dataclass(frozen=True)
class SkipSpan:
    video_start_s: float
    video_end_s: float
    reason: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class HoldSpan:
    spotify_start_s: float
    spotify_end_s: float
    video_s: float
    reason: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class Segment:
    video_start_s: float
    video_end_s: float
    spotify_start_s: float
    spotify_end_s: float
    rate: float


def _segment_rate(anchors: Sequence[Anchor]) -> float:
    """Robust video-per-Spotify slope, ignoring contaminated edge anchors."""
    if len(anchors) == 1:
        return 1.0
    if len(anchors) == 2:
        first, last = anchors
        span_s = last.spotify_s - first.spotify_s
        return (last.video_s - first.video_s) / span_s if span_s > 0 else 1.0
    sample = anchors[1:-1] if len(anchors) >= 5 else anchors
    slopes = [
        (later.video_s - earlier.video_s) / (later.spotify_s - earlier.spotify_s)
        for earlier, later in combinations(sample, 2)
        if later.spotify_s > earlier.spotify_s
    ]
    return median(slopes) if slopes else 1.0


class Alignment:
    """Mapping between one music video and one Spotify track."""

    def __init__(
        self,
        *,
        video_id: str,
        track_id: str,
        video_duration_s: float,
        track_duration_s: float,
        anchors: Sequence[Anchor],
        skips: Sequence[SkipSpan] = (),
        holds: Sequence[HoldSpan] = (),
    ) -> None:
        if not anchors:
            raise ValueError("alignment needs at least one anchor")
        if video_duration_s <= 0 or track_duration_s <= 0:
            raise ValueError("durations must be positive")
        self._validate_anchors(anchors)
        self.video_id = video_id
        self.track_id = track_id
        self.video_duration_s = video_duration_s
        self.track_duration_s = track_duration_s
        self.anchors = tuple(anchors)
        self.skips = tuple(
            sorted(self._validate_skips(skips), key=lambda span: span.video_start_s)
        )
        self.holds = tuple(
            sorted(
                self._validate_holds(holds, anchors, self.skips),
                key=lambda span: span.spotify_start_s,
            )
        )
        self.segments = tuple(self._build_segments())

    @staticmethod
    def _validate_anchors(anchors: Sequence[Anchor]) -> None:
        for previous, current in zip(anchors, anchors[1:], strict=False):
            if current.video_s <= previous.video_s or current.spotify_s <= previous.spotify_s:
                raise ValueError("anchors must be strictly increasing in both timelines")

    def _validate_skips(self, skips: Sequence[SkipSpan]) -> list[SkipSpan]:
        result: list[SkipSpan] = []
        for span in skips:
            if span.video_end_s <= span.video_start_s:
                raise ValueError("skip span must have positive duration")
            result.append(span)
        return result

    def _validate_holds(
        self,
        holds: Sequence[HoldSpan],
        anchors: Sequence[Anchor],
        skips: Sequence[SkipSpan],
    ) -> list[HoldSpan]:
        ordered = sorted(holds, key=lambda span: span.spotify_start_s)
        previous_end = -1.0
        for hold in ordered:
            if hold.spotify_end_s <= hold.spotify_start_s:
                raise ValueError("hold span must have positive duration")
            if hold.spotify_start_s < -1e-9 or hold.spotify_end_s > self.track_duration_s + 1e-9:
                raise ValueError("hold must stay within the track duration")
            if not 0.0 <= hold.video_s <= self.video_duration_s:
                raise ValueError("hold video position must stay within the video")
            if hold.spotify_start_s < previous_end - 1e-9:
                raise ValueError("hold spans must not overlap")
            if any(
                span.video_start_s < hold.video_s < span.video_end_s for span in skips
            ):
                raise ValueError("hold video position lies inside a skip")
            if any(
                hold.spotify_start_s + 1e-9 < anchor.spotify_s < hold.spotify_end_s - 1e-9
                for anchor in anchors
            ):
                raise ValueError("hold spans must not contain anchors")
            previous_end = hold.spotify_end_s
        return ordered

    def _build_segments(self) -> list[Segment]:
        groups: list[list[Anchor]] = [[self.anchors[0]]]
        for previous, current in zip(self.anchors, self.anchors[1:], strict=False):
            if self._skip_between(previous.video_s, current.video_s) and groups[-1]:
                groups.append([])
            groups[-1].append(current)

        segments: list[Segment] = []
        for index, group in enumerate(groups):
            next_first = groups[index + 1][0] if index + 1 < len(groups) else None
            segments.extend(self._group_segments(group, next_first))
        return segments

    def _group_segments(
        self, group: Sequence[Anchor], next_first: Anchor | None
    ) -> list[Segment]:
        base_video_end = next_first.video_s if next_first is not None else self.video_duration_s
        base_spotify_end = (
            next_first.spotify_s if next_first is not None else self.track_duration_s
        )
        base_spotify_start = group[0].spotify_s
        relevant = [
            hold
            for hold in self.holds
            if hold.spotify_end_s > base_spotify_start
            and hold.spotify_start_s < base_spotify_end
        ]
        if not relevant:
            return [
                Segment(
                    video_start_s=group[0].video_s,
                    video_end_s=base_video_end,
                    spotify_start_s=base_spotify_start,
                    spotify_end_s=base_spotify_end,
                    rate=_segment_rate(group),
                )
            ]

        segments: list[Segment] = []
        cursor_video = group[0].video_s
        cursor_spotify = base_spotify_start
        for hold in relevant:
            if hold.spotify_start_s > cursor_spotify:
                piece = [
                    anchor
                    for anchor in group
                    if cursor_spotify <= anchor.spotify_s <= hold.spotify_start_s
                ]
                segments.append(
                    Segment(
                        video_start_s=cursor_video,
                        video_end_s=hold.video_s,
                        spotify_start_s=cursor_spotify,
                        spotify_end_s=hold.spotify_start_s,
                        rate=_segment_rate(piece),
                    )
                )
            segments.append(
                Segment(
                    video_start_s=hold.video_s,
                    video_end_s=hold.video_s,
                    spotify_start_s=hold.spotify_start_s,
                    spotify_end_s=hold.spotify_end_s,
                    rate=0.0,
                )
            )
            cursor_video = hold.video_s
            cursor_spotify = hold.spotify_end_s
        if cursor_spotify < base_spotify_end or cursor_video < base_video_end:
            piece = [
                anchor
                for anchor in group
                if cursor_spotify <= anchor.spotify_s <= base_spotify_end
            ]
            segments.append(
                Segment(
                    video_start_s=cursor_video,
                    video_end_s=base_video_end,
                    spotify_start_s=cursor_spotify,
                    spotify_end_s=base_spotify_end,
                    rate=_segment_rate(piece),
                )
            )
        return segments

    def _skip_between(self, video_start_s: float, video_end_s: float) -> bool:
        return any(
            span.video_start_s < video_end_s and span.video_end_s > video_start_s
            for span in self.skips
        )

    def _segment_for_spotify(self, spotify_s: float) -> Segment:
        for segment in reversed(self.segments):
            if spotify_s >= segment.spotify_start_s:
                return segment
        return self.segments[0]

    def rate_at(self, spotify_s: float, offset_s: float = 0.0) -> float:
        """Local video-per-Spotify rate for a track position."""
        return self._segment_for_spotify(spotify_s + offset_s).rate

    def _segment_for_video(self, video_s: float) -> Segment:
        for segment in reversed(self.segments):
            if video_s >= segment.video_start_s:
                return segment
        return self.segments[0]

    def spotify_to_video(self, spotify_s: float, offset_s: float = 0.0) -> float:
        """Where the video should be for a given Spotify position."""
        adjusted = spotify_s + offset_s
        segment = self._segment_for_spotify(adjusted)
        target = segment.video_start_s + (adjusted - segment.spotify_start_s) * segment.rate
        return max(0.0, min(target, self.video_duration_s))

    def video_to_spotify(self, video_s: float) -> float:
        segment = self._segment_for_video(video_s)
        return segment.spotify_start_s + (video_s - segment.video_start_s) / segment.rate

    def hold_at(self, spotify_s: float, offset_s: float = 0.0) -> HoldSpan | None:
        adjusted = spotify_s + offset_s
        for hold in self.holds:
            if hold.spotify_start_s <= adjusted < hold.spotify_end_s:
                return hold
        return None

    def skip_at(self, video_s: float) -> SkipSpan | None:
        for span in self.skips:
            if span.video_start_s <= video_s < span.video_end_s:
                return span
        return None

    def skip_exit(self, video_s: float) -> float | None:
        span = self.skip_at(video_s)
        return span.video_end_s if span else None
