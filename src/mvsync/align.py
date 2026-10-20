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
while the video advances) and high cosine cost steps. A gap needs both, and
expands over adjacent high-cost frames so the skip reaches the real edit
boundaries. Low-cost stalls outside gaps are repaired instead of dropped; a
rejected gap must never become an invisible hole in the anchor timeline. On
each accepted gap the anchors after it are re-anchored: the song position of
the last honest frame before the gap is also the song position of the first
honest frame after it (the audio edit is seamless), so the mapping stays exact
at the cut. The opposite edit, a track-side cut where Spotify advances while
the video barely moves, becomes a ``HoldSpan`` so the video freezes while the
missing song section plays.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mvsync.timeline import Alignment, Anchor, HoldSpan, SkipSpan

SR = 22050
HOP_LENGTH = 2048
ANCHOR_STEP_S = 2.0
GAP_THRESHOLD_S = 0.75
GAP_EXPAND_MIN_COST = 0.05
GAP_EXPAND_PEAK_SHARE = 0.2
EDGE_THRESHOLD_S = 1.0
UNMATCHED_COST_THRESHOLD = 0.22
CUT_SLOPE_THRESHOLD = 0.7
CUT_BRIDGE_STEPS = 1
CUT_MIN_SPAN_S = 1.0
CUT_MIN_MISSING_S = 0.75
TAIL_REPAIR_MAX_S = 2.0
TAIL_BAD_RATE = 0.75
TAIL_FAST_RATE = 1.25

MIN_SANE_ANCHORS = 4
MIN_SANE_COVERAGE_FULL = 0.6
MIN_SANE_COVERAGE_PARTIAL = 0.5
MIN_PARTIAL_COVERAGE_S = 30.0
MAX_SANE_RATE_DEVIATION = 0.1
MAX_PLAY_RATE_DEVIATION = 0.2
MAX_OFF_RATE_SPAN_S = 15.0
MIN_SANE_RATE_SHARE = 0.5
RATE_EPSILON = 1e-9


def looks_sane(
    alignment: Alignment,
    track_duration_s: float,
    *,
    coverage_span_s: float | None = None,
    full_scan: bool = True,
) -> bool:
    """Cheap heuristic that the video really matches the recorded song.

    A wrong pick (a different song, a live take with different audio) yields a
    sparse map covering little of the recording, or nonsensical segment rates.
    An MV that re-edits the arrangement still passes: its cuts smear a minority
    of the covered span into off-rate segments while the rest keeps rate 1.
    Scans must cover most of the whole track; a first listen that starts late
    is judged against the captured span instead.
    """
    anchors = alignment.anchors
    if len(anchors) < MIN_SANE_ANCHORS:
        return False
    covered = anchors[-1].spotify_s - anchors[0].spotify_s
    coverage_span = track_duration_s if coverage_span_s is None else coverage_span_s
    if full_scan:
        required = MIN_SANE_COVERAGE_FULL * coverage_span
    else:
        required = max(MIN_PARTIAL_COVERAGE_S, MIN_SANE_COVERAGE_PARTIAL * coverage_span)
    if covered < required:
        return False

    covered_start = anchors[0].spotify_s
    covered_end = anchors[-1].spotify_s
    sane_span_s = 0.0
    for segment in alignment.segments:
        overlap = max(
            0.0,
            min(segment.spotify_end_s, covered_end)
            - max(segment.spotify_start_s, covered_start),
        )
        if overlap <= 0.0 or segment.rate == 0.0:
            continue
        deviation = abs(segment.rate - 1.0)
        segment_span_s = segment.spotify_end_s - segment.spotify_start_s
        if (
            segment_span_s > MAX_OFF_RATE_SPAN_S
            and deviation > MAX_PLAY_RATE_DEVIATION + RATE_EPSILON
        ):
            return False
        if deviation <= MAX_SANE_RATE_DEVIATION + RATE_EPSILON:
            sane_span_s += overlap
    return sane_span_s >= MIN_SANE_RATE_SHARE * covered


def anchors_and_skips_from_path(
    path: Sequence[tuple[int, int]],
    *,
    spotify_hop_s: float,
    video_hop_s: float,
    video_duration_s: float,
    track_duration_s: float | None = None,
    step_costs: Sequence[float] | None = None,
    cost_threshold: float = UNMATCHED_COST_THRESHOLD,
    anchor_step_s: float = ANCHOR_STEP_S,
    gap_threshold_s: float = GAP_THRESHOLD_S,
    edge_threshold_s: float = EDGE_THRESHOLD_S,
) -> tuple[list[Anchor], list[SkipSpan]]:
    """Anchors and video-only skips; track-side holds are dropped."""
    anchors, skips, _ = anchors_skips_and_holds_from_path(
        path,
        spotify_hop_s=spotify_hop_s,
        video_hop_s=video_hop_s,
        video_duration_s=video_duration_s,
        track_duration_s=track_duration_s,
        step_costs=step_costs,
        cost_threshold=cost_threshold,
        anchor_step_s=anchor_step_s,
        gap_threshold_s=gap_threshold_s,
        edge_threshold_s=edge_threshold_s,
    )
    return anchors, skips


def anchors_skips_and_holds_from_path(
    path: Sequence[tuple[int, int]],
    *,
    spotify_hop_s: float,
    video_hop_s: float,
    video_duration_s: float,
    track_duration_s: float | None = None,
    step_costs: Sequence[float] | None = None,
    cost_threshold: float = UNMATCHED_COST_THRESHOLD,
    anchor_step_s: float = ANCHOR_STEP_S,
    gap_threshold_s: float = GAP_THRESHOLD_S,
    edge_threshold_s: float = EDGE_THRESHOLD_S,
) -> tuple[list[Anchor], list[SkipSpan], list[HoldSpan]]:
    """Convert a DTW warping path of frame pairs into anchors, skips, and holds.

    ``path`` items are ``(spotify_frame, video_frame)`` pairs ordered by
    non-decreasing video frames. An arrival (step into a path point) counts as
    gap evidence when it stalls (Spotify frame unchanged, video advancing) or,
    with ``step_costs`` given, when its match cost exceeds ``cost_threshold``.
    Leading and trailing consumption are detected from stalls alone as well. An
    internal gap must contain a stalling arrival and, with costs, a flagged
    arrival, so freely flowing music and low-cost video repeats are never
    skipped.

    A low-slope stretch where Spotify advances while the video barely moves is
    a track-side cut. It becomes a ``HoldSpan`` instead of an anchor run, so the
    video keeps its frame while the missing track section plays. Video before
    the first honest frame becomes a leading skip, video after the last honest
    frame a trailing skip. With ``step_costs`` given, accepted gaps expand over
    adjacent high-cost frames, low-cost stalls outside gaps are repaired to the
    local rate, and anchor Spotify coordinates after each gap are shifted so
    the last honest frame before the gap maps to the same Spotify position as
    the first honest frame after it. A compressed path tail on a video repeat
    is snapped back to the track end when the last stable segment supports it.
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

    gaps = _select_gaps(
        points,
        costs,
        evidence,
        stalled,
        flagged,
        first_honest,
        last_honest,
        video_hop_s=video_hop_s,
        gap_threshold_s=gap_threshold_s,
        cost_threshold=cost_threshold,
    )
    for start, end in gaps:
        skips.append(
            SkipSpan(
                points[start - 1][1] * video_hop_s,
                points[end + 1][1] * video_hop_s,
                reason="internal",
                confidence=1.0,
            )
        )

    trailing_start_s = points[last_honest][1] * video_hop_s
    if video_duration_s - trailing_start_s >= edge_threshold_s:
        skips.append(
            SkipSpan(trailing_start_s, video_duration_s, reason="trailing", confidence=1.0)
        )

    holds, cut_members, cut_forced = _detect_cuts(
        points,
        costs,
        flagged,
        first_honest,
        last_honest,
        skips,
        spotify_hop_s=spotify_hop_s,
        video_hop_s=video_hop_s,
        track_duration_s=track_duration_s,
    )

    repaired = _repair_spotify_coordinates(
        points,
        stalled,
        flagged,
        gaps,
        spotify_hop_s=spotify_hop_s,
        video_hop_s=video_hop_s,
        enabled=costs is not None,
    )
    deltas = _correction_deltas(repaired, gaps, first_honest)
    gap_members = _gap_member_mask(len(points), gaps)

    forced = sorted(
        {index for start, end in gaps for index in (start - 1, end + 1)} | cut_forced
    )
    forced_position = 0
    anchors: list[Anchor] = []
    last_sampled_s: float | None = None
    hold_members = [
        any(
            hold.spotify_start_s < spotify_frame * spotify_hop_s < hold.spotify_end_s
            for hold in holds
        )
        for spotify_frame, _ in points
    ]
    if costs is not None:
        skipped_for_anchors = [
            gap or cut or held
            for gap, cut, held in zip(gap_members, cut_members, hold_members, strict=True)
        ]
    else:
        skipped_for_anchors = [
            evidence_frame or cut or held
            for evidence_frame, cut, held in zip(
                evidence, cut_members, hold_members, strict=True
            )
        ]
    for index, (_, video_frame) in enumerate(points):
        if skipped_for_anchors[index]:
            continue
        spotify_s = repaired[index] + deltas[index]
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
            spotify_s=repaired[last_honest] + deltas[last_honest],
        ),
    )

    filtered = [anchor for anchor in anchors if not _inside_skip(anchor.video_s, skips)]
    _repair_tail(filtered, skips, track_duration_s, video_duration_s)
    return filtered, skips, holds


def _detect_cuts(
    points: Sequence[tuple[int, int]],
    costs: Sequence[float] | None,
    flagged: Sequence[bool],
    first_honest: int,
    last_honest: int,
    skips: Sequence[SkipSpan],
    *,
    spotify_hop_s: float,
    video_hop_s: float,
    track_duration_s: float | None,
) -> tuple[list[HoldSpan], list[bool], set[int]]:
    """Find low-slope path runs that mean the video omitted part of the track."""
    candidates: list[int] = []
    for index in range(1, len(points)):
        delta_spotify = points[index][0] - points[index - 1][0]
        delta_video = points[index][1] - points[index - 1][1]
        if delta_spotify > 0 and delta_video <= CUT_SLOPE_THRESHOLD * delta_spotify:
            candidates.append(index)
    if not candidates:
        return [], [False] * len(points), set()

    runs: list[tuple[int, int]] = []
    start = previous = candidates[0]
    for step in candidates[1:]:
        if step - previous <= CUT_BRIDGE_STEPS + 1:
            previous = step
        else:
            runs.append((start, previous))
            start = previous = step
    runs.append((start, previous))

    holds: list[HoldSpan] = []
    cut_members = [False] * len(points)
    cut_forced: set[int] = set()
    for start_step, end_step in runs:
        before = start_step - 1
        after = end_step
        if before < first_honest or after >= last_honest:
            continue
        if _inside_skip(points[before][1] * video_hop_s, skips):
            continue
        spotify_span_s = (points[after][0] - points[before][0]) * spotify_hop_s
        video_span_s = (points[after][1] - points[before][1]) * video_hop_s
        missing_s = spotify_span_s - video_span_s
        if spotify_span_s < CUT_MIN_SPAN_S or missing_s < CUT_MIN_MISSING_S:
            continue
        if costs is not None and not any(flagged[before + 1 : after + 1]):
            continue
        hold_start_s = points[before][0] * spotify_hop_s
        hold_end_s = hold_start_s + missing_s
        if track_duration_s is not None and hold_end_s > track_duration_s:
            hold_end_s = track_duration_s
        if hold_end_s <= hold_start_s:
            continue
        holds.append(
            HoldSpan(
                hold_start_s,
                hold_end_s,
                points[before][1] * video_hop_s,
                reason="track-cut",
                confidence=1.0,
            )
        )
        for index in range(before + 1, after):
            cut_members[index] = True
        cut_forced.update((before, after))
    return holds, cut_members, cut_forced


def _repair_tail(
    anchors: list[Anchor],
    skips: list[SkipSpan],
    track_duration_s: float | None,
    video_duration_s: float,
) -> None:
    """Snap a compressed trailing path back to the track end when stable."""
    if track_duration_s is None or len(anchors) < 2:
        return
    trailing_index = next(
        (index for index, span in enumerate(skips) if span.reason == "trailing"),
        None,
    )
    if trailing_index is None:
        return
    last = anchors[-1]
    stable = anchors[-2]
    tail_span_s = last.spotify_s - stable.spotify_s
    if tail_span_s <= 0.0:
        return
    tail_rate = (last.video_s - stable.video_s) / tail_span_s
    if TAIL_BAD_RATE <= tail_rate <= TAIL_FAST_RATE:
        return
    stable_rate = 1.0
    if len(anchors) >= 3:
        earlier = anchors[-3]
        earlier_span_s = stable.spotify_s - earlier.spotify_s
        if earlier_span_s > 0.0:
            candidate = (stable.video_s - earlier.video_s) / earlier_span_s
            if candidate > 0.0:
                stable_rate = candidate
    expected_video_s = stable.video_s + (track_duration_s - stable.spotify_s) * stable_rate
    if expected_video_s <= last.video_s or expected_video_s > video_duration_s:
        return
    if expected_video_s - last.video_s > TAIL_REPAIR_MAX_S:
        return
    anchors[-1] = Anchor(video_s=expected_video_s, spotify_s=track_duration_s)
    trailing = skips[trailing_index]
    skips[trailing_index] = SkipSpan(
        video_start_s=expected_video_s,
        video_end_s=trailing.video_end_s,
        reason=trailing.reason,
        confidence=trailing.confidence,
    )


def _select_gaps(
    points: Sequence[tuple[int, int]],
    costs: Sequence[float] | None,
    evidence: Sequence[bool],
    stalled: Sequence[bool],
    flagged: Sequence[bool],
    first_honest: int,
    last_honest: int,
    *,
    video_hop_s: float,
    gap_threshold_s: float,
    cost_threshold: float,
) -> list[tuple[int, int]]:
    """Pick internal evidence runs and expand them over adjacent bad matches."""
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
        if costs is not None and not any(flagged[start : end + 1]):
            continue
        seed_span_s = (points[end][1] - points[start - 1][1]) * video_hop_s
        if seed_span_s < gap_threshold_s:
            continue
        if costs is not None:
            start, end = _expand_gap(
                points,
                costs,
                evidence,
                start,
                end,
                first_honest,
                last_honest,
                cost_threshold=cost_threshold,
            )
        if gaps and start <= gaps[-1][1] + 1:
            gaps[-1] = (gaps[-1][0], max(gaps[-1][1], end))
        else:
            gaps.append((start, end))
    return gaps


def _expand_gap(
    points: Sequence[tuple[int, int]],
    costs: Sequence[float],
    evidence: Sequence[bool],
    start: int,
    end: int,
    first_honest: int,
    last_honest: int,
    *,
    cost_threshold: float,
) -> tuple[int, int]:
    """Walk a seed outward while the video still looks unlike the track."""
    peak = max(costs[start : end + 1])
    expand_threshold = min(
        cost_threshold, max(GAP_EXPAND_MIN_COST, GAP_EXPAND_PEAK_SHARE * peak)
    )
    while start - 1 > first_honest and (
        evidence[start - 1] or costs[start - 1] > expand_threshold
    ):
        start -= 1
    while end + 1 < last_honest and (
        evidence[end + 1] or costs[end + 1] > expand_threshold
    ):
        end += 1
    return start, end


def _repair_spotify_coordinates(
    points: Sequence[tuple[int, int]],
    stalled: Sequence[bool],
    flagged: Sequence[bool],
    gaps: Sequence[tuple[int, int]],
    *,
    spotify_hop_s: float,
    video_hop_s: float,
    enabled: bool,
) -> list[float]:
    """Advance Spotify through low-cost stalls that were not accepted as gaps."""
    raw = [spotify_frame * spotify_hop_s for spotify_frame, _ in points]
    if not enabled:
        return raw
    gap_members = _gap_member_mask(len(points), gaps)
    repaired = list(raw)
    repaired_any = False
    for index in range(1, len(points)):
        delta_spotify = (points[index][0] - points[index - 1][0]) * spotify_hop_s
        delta_video = (points[index][1] - points[index - 1][1]) * video_hop_s
        if gap_members[index]:
            step = delta_spotify
        elif stalled[index] and not flagged[index]:
            step = delta_video
            repaired_any = True
        else:
            step = delta_spotify
        repaired[index] = repaired[index - 1] + step
    return repaired if repaired_any else raw


def _gap_member_mask(count: int, gaps: Sequence[tuple[int, int]]) -> list[bool]:
    members = [False] * count
    for start, end in gaps:
        for index in range(start, end + 1):
            members[index] = True
    return members


def _correction_deltas(
    spotify_s: Sequence[float],
    gaps: Sequence[tuple[int, int]],
    first_honest: int,
) -> list[float]:
    """Spotify coordinate shift for every path point.

    Leading consumption shifts the whole honest region so the first honest
    frame lands on song position 0. Each internal gap shifts the region after
    it back to the Spotify position of the last honest frame before the gap.
    """
    deltas = [0.0] * len(spotify_s)
    events: list[tuple[int, float]] = []
    current = 0.0
    if first_honest > 0:
        current = -spotify_s[first_honest]
        events.append((first_honest, current))
    for start, end in gaps:
        honest_before = start - 1
        honest_after = end + 1
        spotify_before = spotify_s[honest_before] + current
        spotify_after = spotify_s[honest_after]
        current = spotify_before - spotify_after
        events.append((honest_after, current))
    event_position = 0
    value = 0.0
    for index in range(len(spotify_s)):
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
    anchors, skips, holds = anchors_skips_and_holds_from_path(
        points,
        spotify_hop_s=hop_length / sr,
        video_hop_s=hop_length / sr,
        video_duration_s=video_duration_s,
        track_duration_s=track_duration_s,
        step_costs=step_costs,
    )
    return Alignment(
        video_id=video_id,
        track_id=track_id,
        video_duration_s=video_duration_s,
        track_duration_s=track_duration_s,
        anchors=anchors,
        skips=skips,
        holds=holds,
    )
