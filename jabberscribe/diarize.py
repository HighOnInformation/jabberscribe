"""Speaker labels: exact or absent.

With dual-track audio, the channel a segment came from *is* the speaker -- a
fact, not an inference. With a mixed track we emit no label at all. Guessing
turns from pause length produces confidently wrong attributions, and a
compliance transcript must never fabricate who said what.
"""

from __future__ import annotations

from jabberscribe.stt import Segment

MIXED_LABEL = "mixed"


def merge_tracks(per_track: dict[str, list[Segment]]) -> list[Segment]:
    """Merge per-channel segments into one timeline.

    Overlapping speech is preserved as separate segments: on a real call people
    talk over each other, and dropping either side loses content.
    """
    labelled: list[Segment] = []
    for label, segments in per_track.items():
        speaker = None if label == MIXED_LABEL else label
        labelled.extend(Segment(start=s.start, end=s.end, text=s.text, speaker=speaker) for s in segments)
    labelled.sort(key=lambda s: (s.start, s.speaker or "", s.end))
    return labelled
