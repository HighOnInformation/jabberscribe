"""Near/far-end speaker labels for dual-track calls.

The recorder captures each end of a call on its own channel, so who said what
is known without diarization: each channel is transcribed on its own, every
segment is labelled with that channel's speaker, and the two transcripts are
merged by start time. Labels come from the sidecar -- the recorded line's
display name for the near end, the other party's for the far end of a 1:1
call, generic labels when a name is missing. A conference's far channel is the
bridge mix of everyone else, so it is labelled as a group; telling those
voices apart would need diarization.

A channel without signal is not sent to Whisper at all: on silence it invents
words, and the STT hallucination filters catch most but not all of them.
Labels are personal data, so they are never logged.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from jabberscribe.audio import AudioError, channel_count, channel_peak_db, prepare_channel_for_stt, prepare_for_stt
from jabberscribe.config import SttConfig
from jabberscribe.sidecar import Sidecar
from jabberscribe.stt import Segment, Transcriber

log = logging.getLogger(__name__)

NEAR_FALLBACK = "צד א"
FAR_FALLBACK = "צד ב"
CONFERENCE_FAR = "משתתפים"
#: A channel whose loudest sample stays below this carries no speech: a dead or muted leg.
SILENT_CHANNEL_DB = -50.0

#: One file to transcribe and the speaker its segments are labelled with (None: an unlabelled downmix).
SttInput = tuple[Path, str | None]


def _name(value: str | None, fallback: str) -> str:
    return (value or "").strip() or fallback


def speaker_labels(sidecar: Sidecar) -> tuple[str, str]:
    """(near, far) labels for a dual-track recording."""
    near = _name(sidecar.line_owner.display_name, NEAR_FALLBACK)
    if sidecar.kind == "conference" or sidecar.conference_id is not None:
        return near, CONFERENCE_FAR
    if len(sidecar.parties) == 1:
        return near, _name(sidecar.parties[0].display_name, FAR_FALLBACK)
    return near, FAR_FALLBACK


def stt_inputs(audio: Path, work: Path, sidecar: Sidecar, cfg: SttConfig) -> list[SttInput]:
    """Prepare the files the stt stage sends to Whisper. Idempotent; the audio stage calls it first.

    A dual-track call that cannot be split (a mono file behind a "dual"
    sidecar) falls back to the downmix: wrong metadata must not cost the
    transcript, nor put every word in the near end's mouth. Truly broken audio
    fails in the downmix as well, as before.
    """
    if not (cfg.split_channels and sidecar.tracks == "dual"):
        return [(prepare_for_stt(audio, work), None)]
    near, far = speaker_labels(sidecar)
    inputs: list[SttInput] = []
    try:
        channels = channel_count(audio)
        if channels != 2:
            raise AudioError(f"expected 2 channels, found {channels}")
        for channel, label in ((cfg.near_channel, near), (1 - cfg.near_channel, far)):
            peak = channel_peak_db(audio, channel)
            if peak < SILENT_CHANNEL_DB:
                log.info("channel %d of %s is silent (%.1f dB); not transcribed", channel, audio, peak)
                continue
            inputs.append((prepare_channel_for_stt(audio, work, channel), label))
    except AudioError as exc:
        log.warning("cannot split %s into channels (%s); transcribing the downmix instead", audio, exc)
        return [(prepare_for_stt(audio, work), None)]
    return inputs


def transcribe_inputs(transcriber: Transcriber, inputs: list[SttInput]) -> list[Segment]:
    """Transcribe every input, label its segments, and merge them by start time (near end first on a tie)."""
    segments: list[Segment] = []
    for path, label in inputs:
        found = transcriber.transcribe(path)
        segments += found if label is None else [replace(s, speaker=label) for s in found]
    return sorted(segments, key=lambda s: (s.start, s.end))
