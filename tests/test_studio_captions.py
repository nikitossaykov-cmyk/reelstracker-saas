"""Unit tests for the Studio caption pipeline (pure math, no ffmpeg)."""
from __future__ import annotations

import pytest

from app.services.strategy_single_take.captions import (
    align_sentences,
    build_ass,
    parse_silencedetect,
    speech_spans,
    split_sentences,
    _ts,
)


FFMPEG_STDERR = """\
[silencedetect @ 0x5555] silence_start: 1.10
[silencedetect @ 0x5555] silence_end: 1.60 | silence_duration: 0.50
[silencedetect @ 0x5555] silence_start: 6.20
[silencedetect @ 0x5555] silence_end: 6.50 | silence_duration: 0.30
size=N/A time=00:00:10.00 bitrate=N/A speed= 500x
"""


def test_parse_silencedetect():
    assert parse_silencedetect(FFMPEG_STDERR) == [(1.10, 1.60), (6.20, 6.50)]


def test_parse_silencedetect_unclosed_final_silence():
    # trailing silence with no silence_end (audio ends silent)
    stderr = "[x] silence_start: 8.0\n"
    assert parse_silencedetect(stderr) == [(8.0, None)]


def test_speech_spans_inverts_silences():
    spans = speech_spans([(1.10, 1.60), (6.20, 6.50)], total=10.0)
    assert spans == [(0.0, 1.10), (1.60, 6.20), (6.50, 10.0)]


def test_speech_spans_trailing_silence():
    spans = speech_spans([(8.0, None)], total=10.0)
    assert spans == [(0.0, 8.0)]


def test_speech_spans_no_silence():
    assert speech_spans([], total=10.0) == [(0.0, 10.0)]


def test_split_sentences():
    text = "Я это заказала. Первый раз в жизни! Ну что?"
    assert split_sentences(text) == [
        "Я это заказала", "Первый раз в жизни", "Ну что?",
    ]


def test_split_sentences_strips_eleven_tags():
    text = "[whispers] Только не за 16 тысяч. [curious] Ну что?"
    assert split_sentences(text) == ["Только не за 16 тысяч", "Ну что?"]


def test_align_sentences_exact_match():
    sents = ["раз", "два"]
    spans = [(0.0, 1.0), (2.0, 3.0)]
    assert align_sentences(sents, spans) == [
        (0.0, 1.0, "раз"), (2.0, 3.0, "два"),
    ]


def test_align_sentences_mismatch_falls_back_to_proportional():
    # 3 sentences, 2 speech spans → distribute by char length over
    # [first_start, last_end] with a small gap between sentences.
    sents = ["ab", "ab", "ab"]
    spans = [(0.0, 4.0), (5.0, 9.0)]
    out = align_sentences(sents, spans)
    assert len(out) == 3
    assert out[0][0] == 0.0
    assert out[-1][1] == pytest.approx(9.0, abs=0.01)
    # monotonic, non-overlapping
    for (s1, e1, _), (s2, e2, _) in zip(out, out[1:]):
        assert e1 <= s2
        assert s1 < e1 and s2 < e2


def test_ts_format():
    assert _ts(0.0) == "0:00:00.00"
    assert _ts(83.5) == "0:01:23.50"


def test_build_ass_word_timing_proportional():
    ass = build_ass([(0.0, 3.0, "яя ббб")])
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(lines) == 2
    # weights: len+1 → 3 and 4; word1 gets 3/7 of 3.0s ≈ 1.29
    assert lines[0].endswith(",яя")
    assert lines[1].endswith(",ббб")
    assert "0:00:00.00,0:00:01.29" in lines[0]
    assert "0:00:01.29,0:00:03.00" in lines[1]


def test_build_ass_header_style():
    ass = build_ass([(0.0, 1.0, "слово")])
    assert "PlayResX: 720" in ass
    assert "PlayResY: 1280" in ass
    # v36 style: DejaVu Sans 58, Alignment 2, MarginV 215
    assert "Style: cap,DejaVu Sans,58," in ass
    style_line = next(l for l in ass.splitlines() if l.startswith("Style:"))
    fields = style_line.split(",")
    assert fields[18] == "2"    # Alignment
    assert fields[21] == "215"  # MarginV


def test_apply_asmr_tags():
    from app.services.strategy_single_take.voiceover import apply_asmr_tags
    text = "Я это заказала. Ну что?"
    assert apply_asmr_tags(text) == "[whispers] Я это заказала. [whispers] Ну что?"


def test_apply_asmr_tags_idempotent_on_tagged_text():
    from app.services.strategy_single_take.voiceover import apply_asmr_tags
    text = "[whispers] Уже с тегом."
    assert apply_asmr_tags(text) == "[whispers] Уже с тегом."


def test_studio_portrait_prompt_contains_banlist_and_framing():
    from app.services.strategy_single_take.portrait import build_studio_prompt
    p_asmr = build_studio_prompt(
        product_name="WHITE CHOCOLATE", brand="dose", asmr=True,
    )
    assert "WHITE CHOCOLATE" in p_asmr
    assert "dose" in p_asmr
    assert "misspell" in p_asmr.lower()
    assert "microphone" in p_asmr.lower()  # ASMR mic prop

    p_norm = build_studio_prompt(
        product_name="WHITE CHOCOLATE", brand="dose", asmr=False,
    )
    assert "microphone" not in p_norm.lower()


def test_studio_script_prompt_asmr_vs_normal():
    from app.services.strategy_single_take.script import build_studio_script_prompt
    p = build_studio_script_prompt(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="asmr",
    )
    assert "шёпот" in p.lower()
    assert "тысяча девятьсот девяносто рублей" in p
    assert "НЕ совершает действий" in p
    p2 = build_studio_script_prompt(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="normal",
    )
    assert "шёпот" not in p2.lower()


def test_cutaway_still_prompts():
    from app.services.strategy_single_take.cutaways import build_cutaway_still_prompt
    cap = build_cutaway_still_prompt(
        kind="cap_off", product_name="WHITE CHOCOLATE", brand="dose",
    )
    assert "SAME woman" in cap
    assert "second reference image" in cap
    assert "WHITE CHOCOLATE" in cap and "dose" in cap
    assert "lifting" in cap.lower() and "cap" in cap.lower()
    spray = build_cutaway_still_prompt(
        kind="spray", product_name="WHITE CHOCOLATE", brand="dose",
    )
    assert "mist" in spray.lower()
    assert "pump" in spray.lower()
    with pytest.raises(ValueError):
        build_cutaway_still_prompt(kind="sniff", product_name="X", brand="Y")


def test_cutaway_motion_prompts():
    from app.services.strategy_single_take.cutaways import MOTION_PROMPTS, NEGATIVE_PROMPT
    assert set(MOTION_PROMPTS) == {"cap_off", "spray"}
    assert "mist" in MOTION_PROMPTS["spray"].lower()
    assert "drinking" in NEGATIVE_PROMPT
    assert "kissing" in NEGATIVE_PROMPT


def test_polish_filter_hook_untouched_body_sped_up():
    from app.services.strategy_single_take.assemble import build_polish_filter
    fc = build_polish_filter(hook_seconds=3.204)
    assert "trim=0:3.204" in fc
    assert "setpts=(PTS-STARTPTS)/1.05" in fc
    assert "noise=alls=5:allf=t" in fc
    assert "atempo=1.05" in fc
    assert "concat=n=2:v=1:a=1" in fc


def test_polish_filter_no_hook():
    from app.services.strategy_single_take.assemble import build_polish_filter
    fc = build_polish_filter(hook_seconds=0.0)
    assert "trim=0:" not in fc      # no hook split
    assert "concat" not in fc       # single chain
    assert "setpts=(PTS-STARTPTS)/1.05" in fc
    assert "noise=alls=5:allf=t" in fc
