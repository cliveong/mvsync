"""Pure timeline mapping between a music video and a Spotify track.

An ``Alignment`` maps points of the video timeline to the Spotify track
timeline. It is stored as anchors ``(video_s, spotify_s)`` sampled from the
DSP alignment, plus skip spans: video regions with no counterpart in the track
(intros, story breaks, outros). Anchors are grouped into segments wherever a
skip lies between two consecutive anchors; within a segment the mapping is
linear with a rate close to 1.0 (exactly 1.0 when the MV uses the album
master, different when the MV is a slightly different mix).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


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
class Segment:
    video_start_s: float
    video_end_s: float
    spotify_start_s: float
    spotify_end_s: float
    rate: float


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

    def _build_segments(self) -> list[Segment]:
        groups: list[list[Anchor]] = [[self.anchors[0]]]
        for previous, current in zip(self.anchors, self.anchors[1:], strict=False):
            if self._skip_between(previous.video_s, current.video_s) and groups[-1]:
                groups.append([])
            groups[-1].append(current)

        segments: list[Segment] = []
        for index, group in enumerate(groups):
            first, last = group[0], group[-1]
            span_s = last.spotify_s - first.spotify_s
            rate = (last.video_s - first.video_s) / span_s if span_s > 0 else 1.0
            if index + 1 < len(groups):
                next_first = groups[index + 1][0]
                video_end = next_first.video_s
                spotify_end = next_first.spotify_s
            else:
                video_end = self.video_duration_s
                spotify_end = self.track_duration_s
            segments.append(
                Segment(
                    video_start_s=first.video_s,
                    video_end_s=video_end,
                    spotify_start_s=first.spotify_s,
                    spotify_end_s=spotify_end,
                    rate=rate,
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

    def skip_at(self, video_s: float) -> SkipSpan | None:
        for span in self.skips:
            if span.video_start_s <= video_s < span.video_end_s:
                return span
        return None

    def skip_exit(self, video_s: float) -> float | None:
        span = self.skip_at(video_s)
        return span.video_end_s if span else None
