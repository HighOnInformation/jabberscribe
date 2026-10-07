"""Checks against the real LiteLLM server. Skipped unless pointed at one:

    JABBERSCRIBE_LIVE_CONFIG=config/jabberscribe.yaml JABBERSCRIBE_LIVE_CLIP=clip.wav pytest -m slow

The clip should be a short real Hebrew recording. The transcription runs with
the production prompt, so a server that rejects the prompt or verbose_json fails here.
"""

import os
from pathlib import Path

import pytest

from jabberscribe.audio import prepare_for_stt
from jabberscribe.cli import doctor
from jabberscribe.config import load_config
from jabberscribe.llm import make_client
from jabberscribe.stt import LiteLLMTranscriber, Segment, build_prompt, load_vocabulary
from jabberscribe.summarize import LiteLLMSummarizer

pytestmark = pytest.mark.slow


def _hebrew(text: str) -> bool:
    return any("א" <= ch <= "ת" for ch in text)


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> tuple:
    config, clip = os.environ.get("JABBERSCRIBE_LIVE_CONFIG"), os.environ.get("JABBERSCRIBE_LIVE_CLIP")
    if not config or not clip:
        pytest.skip("set JABBERSCRIBE_LIVE_CONFIG and JABBERSCRIBE_LIVE_CLIP")
    cfg = load_config(Path(config))
    client = make_client(cfg.litellm)
    audio = prepare_for_stt(Path(clip), tmp_path_factory.mktemp("live"))
    prompt = build_prompt(load_vocabulary(cfg.stt.vocabulary_file))
    segments = LiteLLMTranscriber(client, cfg.stt.model, prompt).transcribe(audio)
    return cfg, client, segments


def test_doctor_probes_pass(live) -> None:
    cfg, client, _segments = live

    failed = [c for c in doctor(cfg, client) if c.name.startswith("litellm") and not c.ok]

    assert failed == []


def test_transcribes_hebrew_with_timestamps(live) -> None:
    _cfg, _client, segments = live

    assert segments
    assert any(_hebrew(s.text) for s in segments)
    assert all(isinstance(s, Segment) and s.end >= s.start for s in segments)


def test_summarizes_in_hebrew(live) -> None:
    cfg, client, segments = live

    summary = LiteLLMSummarizer(client, cfg.summary.model, cfg.summary.max_chunk_chars).summarize(segments)

    assert summary is not None
    assert _hebrew(summary.text)
