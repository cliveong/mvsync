"""DSP alignment between a Spotify recording and an MV audio track.

Two layers:
- Pure helpers turning a DTW warping path (with optional per-step match costs)
  into anchors and skip spans, unit-tested without touching librosa.
- A librosa-based wrapper (imported lazily) computing chroma CENS features and
  running subsequence DTW. The Spotify track is the query; the MV audio is the
  longer sequence the query is aligned inside of, so unmatched MV regions
  (intros, talk, outros) naturally fall out of the path.

Match costs matter: plain DTW happily consumes a few seconds of unmatched
video diagonally, so a gap can leak into the warping path. Unmatched video is
therefore detected from two signals: stalling arrivals (Spotify frame frozen
while the video advances) and high cosine cost steps. On each accepted gap the
anchors after it are re-anchored: the song position of the last honest frame
before the gap is also the song position of the first honest frame after it
(the audio edit is seamless), so the mapping stays exact at the cut.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mvsync.timeline import Alignment, Anchor, SkipSpan

SR = 22050
HOP_LENGTH = 2048
ANCHOR_STEP_S = 2.0
GAP_THRESHOLD_S = 0.75
EDGE_THRESHOLD_S = 1.0
UNMATCHED_COST_THRESHOLD = 0.22

MIN_SANE_ANCHORS = 4
MIN_SANE_COVERAGE = 0.25
MAX_SANE_RATE_DEVIATION = 0.1
MIN_SANE_RATE_SHARE = 0.5


def looks_sane(alignment: Alignment, track_duration_s: float) -> bool:
    """Cheap heuristic that the video really matches the recorded song.

    A wrong pick (a different song, a live take with different audio) yields a
    sparse map covering little of the track, or nonsensical segment rates. An
    MV that re-edits the arrangement still passes: its cuts smear a minority
    of the covered span into off-rate segments while the rest keeps rate 1.
    """
    anchors = alignment.anchors
    if len(anchors) < MIN_SANE_ANCHORS:
        return False
    covered = anchors[-1].spotify_s - anchors[0].spotify_s
    if track_duration_s > 0.0 and covered < MIN_SANE_COVERAGE * track_duration_s:
        return False
    sane_span_s = sum(
        segment.spotify_end_s - segment.spotify_start_s
        for segment in alignment.segments
        if abs(segment.rate - 1.0) <= MAX_SANE_RATE_DEVIATION
    )
    return sane_span_s >= MIN_SANE_RATE_SHARE * covered


def anchors_and_skips_from_path(
    path: Sequence[tuple[int, int]],
    *,
    spotify_hop_s: float,
    video_hop_s: float,
    video_duration_s: float,
    step_costs: Sequence[float] | None = None,
    cost_threshold: float = UNMATCHED_COST_THRESHOLD,
    anchor_step_s: float = ANCHOR_STEP_S,
    gap_threshold_s: float = GAP_THRESHOLD_S,
    edge_threshold_s: float = EDGE_THRESHOLD_S,
) -> tuple[list[Anchor], list[SkipSpan]]:
    """Convert a DTW warping path of frame pairs into anchors and skips.

        ``path`` items are ``(spotify_frame, video_frame)`` pairs ordered by
        non-decreasing video frames. An arrival (step into a path point) counts
        as gap evidence when it stalls (Spotify frame unchanged, video
        advancing) or, with ``step_costs`` given, when its match cost exceeds
        ``cost_threshold``. Evidence frames are never honest, so leading and
        trailing consumption are detected from stalls alone as well. An internal
        gap must contain at least one stalling arrival, so freely flowing music
        is never skipped.

    Video before the first honest frame becomes a leading skip, video after
    the last honest frame a trailing skip. With ``step_costs`` given, anchor
    Spotify coordinates after each gap are shifted so the last honest frame
    before the gap maps to the same Spotify position as the first honest
    frame after it.
    """
    if step_costs is None:
        points = sorted(set(path))
        costs: list[float] | None = None
    else:
        paired = list(zip(path, step_costs, strict=True))
        points = [point for point, _ in paired]
        costs = [float(cost) for _, cost in paired]
    if not points:
        raise ValueError("warping path is empty")
    if any(later[1] < earlier[1] for earlier, later in zip(points, points[1:], strict=False)):
        raise ValueError("warping path must be ordered by non-decreasing video frames")

    stalled = [False] * len(points)
    flagged = [False] * len(points)
    for index in range(1, len(points)):
        delta_spotify = points[index][0] - points[index - 1][0]
        delta_video = points[index][1] - points[index - 1][1]
        stalled[index] = delta_spotify == 0 and delta_video > 0
        flagged[index] = costs is not None and costs[index] > cost_threshold
    evidence = [stall or flag for stall, flag in zip(stalled, flagged, strict=True)]
    honest = [index for index, is_evidence in enumerate(evidence) if not is_evidence]
    if not honest:
        raise ValueError("warping path has no matched frames")
    first_honest = honest[0]
    last_honest = honest[-1]

    skips: list[SkipSpan] = []
    leading_end_s = points[first_honest][1] * video_hop_s
    if leading_end_s >= edge_threshold_s:
        skips.append(SkipSpan(0.0, leading_end_s, reason="leading", confidence=1.0))

    gaps: list[tuple[int, int]] = []
    index = first_honest + 1
    while index < last_honest:
        if not evidence[index]:
            index += 1
            continue
        start = index
        while index < last_honest and evidence[index]:
            index += 1
        end = index - 1
        if not any(stalled[start : end + 1]):
            continue
        span_start_s = points[start - 1][1] * video_hop_s
        span_end_s = points[end][1] * video_hop_s
        if span_end_s - span_start_s >= gap_threshold_s:
            gaps.append((start, end))
            skips.append(SkipSpan(span_start_s, span_end_s, reason="internal", confidence=1.0))

    trailing_start_s = points[last_honest][1] * video_hop_s
    if video_duration_s - trailing_start_s >= edge_threshold_s:
        skips.append(
            SkipSpan(trailing_start_s, video_duration_s, reason="trailing", confidence=1.0)
        )

    deltas = _correction_deltas(points, gaps, first_honest, spotify_hop_s)

    forced = sorted(
        index for start, end in gaps for index in (start - 1, end + 1)
    )
    forced_position = 0
    anchors: list[Anchor] = []
    last_sampled_s: float | None = None
    for index, (spotify_frame, video_frame) in enumerate(points):
        if evidence[index]:
            continue
        spotify_s = spotify_frame * spotify_hop_s + deltas[index]
        is_forced = forced_position < len(forced) and forced[forced_position] == index
        if is_forced or last_sampled_s is None or spotify_s - last_sampled_s >= anchor_step_s:
            _push_anchor(
                anchors, Anchor(video_s=video_frame * video_hop_s, spotify_s=spotify_s)
            )
            last_sampled_s = spotify_s
            if is_forced:
                forced_position += 1
    _push_anchor(
        anchors,
        Anchor(
            video_s=points[last_honest][1] * video_hop_s,
            spotify_s=points[last_honest][0] * spotify_hop_s + deltas[last_honest],
        ),
    )

    filtered = [anchor for anchor in anchors if not _inside_skip(anchor.video_s, skips)]
    return filtered, skips


def _correction_deltas(
    points: Sequence[tuple[int, int]],
    gaps: Sequence[tuple[int, int]],
    first_honest: int,
    spotify_hop_s: float,
) -> list[float]:
    """Spotify coordinate shift for every path point.

    Leading consumption shifts the whole honest region so the first honest
    frame lands on song position 0. Each internal gap shifts the region after
    it back to the Spotify position of the last honest frame before the gap.
    """
    deltas = [0.0] * len(points)
    events: list[tuple[int, float]] = []
    current = 0.0
    if first_honest > 0:
        current = -points[first_honest][0] * spotify_hop_s
        events.append((first_honest, current))
    for start, end in gaps:
        honest_before = start - 1
        honest_after = end + 1
        spotify_before = points[honest_before][0] * spotify_hop_s + current
        spotify_after = points[honest_after][0] * spotify_hop_s
        current = spotify_before - spotify_after
        events.append((honest_after, current))
    event_position = 0
    value = 0.0
    for index in range(len(points)):
        if event_position < len(events) and events[event_position][0] == index:
            value = events[event_position][1]
            event_position += 1
        deltas[index] = value
    return deltas


def _push_anchor(anchors: list[Anchor], anchor: Anchor) -> None:
    """Append keeping both timelines strictly increasing.

    Duplicate Spotify positions happen naturally at a segment boundary: the
    end of one music part and the start of the next share the same track time.
    The later video position wins so the skip between the parts stays visible
    to the planner.
    """
    if anchors and anchor.spotify_s <= anchors[-1].spotify_s:
        if anchor.video_s > anchors[-1].video_s:
            anchors[-1] = anchor
        return
    if anchors and anchor.video_s <= anchors[-1].video_s:
        return
    anchors.append(anchor)


def _inside_skip(video_s: float, skips: Sequence[SkipSpan]) -> bool:
    """True only for positions strictly inside a skip.

    Anchors landing exactly on a skip boundary come from the warping path and
    pin the last or first aligned video moment, so they are kept.
    """
    return any(span.video_start_s < video_s < span.video_end_s for span in skips)


def compute_alignment(
    spotify_wav: Path,
    video_wav: Path,
    *,
    video_id: str,
    track_id: str,
    video_duration_s: float,
    track_duration_s: float,
    sr: int = SR,
    hop_length: int = HOP_LENGTH,
    spotify_offset_s: float = 0.0,
) -> Alignment:
    """Align a recorded Spotify track to an MV audio file via chroma + DTW.

    ``spotify_offset_s`` skips leading pre-roll in the recording (silence
    captured before playback started); all Spotify coordinates are relative to
    the audio after the offset.
    """
    import librosa

    y_spotify, _ = librosa.load(str(spotify_wav), sr=sr, mono=True, offset=spotify_offset_s)
    y_video, _ = librosa.load(str(video_wav), sr=sr, mono=True)
    return compute_alignment_from_audio(
        y_spotify,
        y_video,
        video_id=video_id,
        track_id=track_id,
        video_duration_s=video_duration_s,
        track_duration_s=track_duration_s,
        sr=sr,
        hop_length=hop_length,
    )


def compute_alignment_from_audio(
    y_spotify,
    y_video,
    *,
    video_id: str,
    track_id: str,
    video_duration_s: float,
    track_duration_s: float,
    sr: int = SR,
    hop_length: int = HOP_LENGTH,
) -> Alignment:
    import librosa
    import numpy as np
    from scipy.spatial.distance import cdist

    chroma_spotify = librosa.feature.chroma_cens(y=y_spotify, sr=sr, hop_length=hop_length)
    chroma_video = librosa.feature.chroma_cens(y=y_video, sr=sr, hop_length=hop_length)
    cost_matrix = cdist(chroma_spotify.T, chroma_video.T, metric="cosine")
    cost_matrix = np.nan_to_num(cost_matrix, nan=1.0, posinf=1.0)
    _, path = librosa.sequence.dtw(C=cost_matrix, subseq=True)
    path_array = np.asarray(path)
    if cost_matrix.shape[0] > cost_matrix.shape[1]:
        # librosa flips a taller-than-wide subseq path assuming the caller
        # transposed the matrix; ours is built (query, sequence) already.
        path_array = path_array[:, ::-1]
    if path_array[0, 0] > path_array[-1, 0] or path_array[0, 1] > path_array[-1, 1]:
        path_array = path_array[::-1]
    points = [(int(spotify_frame), int(video_frame)) for spotify_frame, video_frame in path_array]
    step_costs = [
        float(cost_matrix[spotify_frame, video_frame]) for spotify_frame, video_frame in points
    ]
    anchors, skips = anchors_and_skips_from_path(
        points,
        spotify_hop_s=hop_length / sr,
        video_hop_s=hop_length / sr,
        video_duration_s=video_duration_s,
        step_costs=step_costs,
    )
    return Alignment(
        video_id=video_id,
        track_id=track_id,
        video_duration_s=video_duration_s,
        track_duration_s=track_duration_s,
        anchors=anchors,
        skips=skips,
    )
