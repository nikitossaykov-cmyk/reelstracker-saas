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
        cutaways=False,
    )
    assert "шёпот" in p.lower()
    assert "тысяча девятьсот девяносто рублей" in p
    assert "НЕ совершает действий" in p
    p2 = build_studio_script_prompt(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="normal",
        cutaways=False,
    )
    assert "шёпот" not in p2.lower()


def test_studio_script_prompt_cutaways_flag():
    from app.services.strategy_single_take.script import build_studio_script_prompt
    kw = dict(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="asmr",
    )
    p_off = build_studio_script_prompt(**kw, cutaways=False)
    assert "НЕ совершает действий" in p_off       # PR #68 full ban intact
    assert "обещани" in p_off.lower()

    p_on = build_studio_script_prompt(**kw, cutaways=True)
    assert "Сейчас открою" in p_on                # exactly one promise allowed
    assert "паузу" in p_on.lower()                # explicit long pause demanded
    assert "НЕ совершает действий" not in p_on


def test_pick_insert_gap_dominant_gap():
    from app.services.strategy_single_take.captions import pick_insert_gap
    # gaps: 5.0-7.5 (2.5s, midpoint 6.25 = 62.5% of 10) and 8.5-9.0 (0.5s)
    spans = [(0.0, 5.0), (7.5, 8.5), (9.0, 10.0)]
    assert pick_insert_gap(spans, total=10.0) == pytest.approx(6.25)


def test_pick_insert_gap_ignores_gap_outside_window():
    from app.services.strategy_single_take.captions import pick_insert_gap
    # only gap 0.5-1.5: midpoint 1.0 = 10% of 10 → before 20% window
    assert pick_insert_gap([(0.0, 0.5), (1.5, 10.0)], total=10.0) is None
    # only gap 9.0-9.8: midpoint 9.4 = 94% → after 85% window
    assert pick_insert_gap([(0.0, 9.0), (9.8, 10.0)], total=10.0) is None


def test_pick_insert_gap_too_short_or_none():
    from app.services.strategy_single_take.captions import pick_insert_gap
    # longest in-window gap is 0.4s < 0.5s min
    assert pick_insert_gap([(0.0, 5.0), (5.4, 10.0)], total=10.0) is None
    # single span → no gaps at all
    assert pick_insert_gap([(0.0, 10.0)], total=10.0) is None
    assert pick_insert_gap([], total=10.0) is None


def test_shift_captions():
    from app.services.strategy_single_take.captions import shift_captions
    aligned = [(0.0, 2.0, "до"), (3.0, 5.0, "после"), (6.0, 8.0, "хвост")]
    out = shift_captions(aligned, split_at=2.5, inserts_seconds=2.4)
    assert out == [
        (0.0, 2.0, "до"),
        (3.0 + 2.4, 5.0 + 2.4, "после"),
        (6.0 + 2.4, 8.0 + 2.4, "хвост"),
    ]


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


def test_cut_clip_cmd():
    from pathlib import Path
    from app.services.strategy_single_take.assemble import VF_NORMALIZE, cut_clip_cmd
    cmd = cut_clip_cmd(Path("in.mp4"), Path("out.mp4"), start=0.0, end=6.25)
    s = " ".join(cmd)
    assert "-ss 0.0" in s and "-to 6.25" in s
    assert VF_NORMALIZE in s          # re-encode keeps concat uniform
    # open-ended tail cut
    cmd2 = cut_clip_cmd(Path("in.mp4"), Path("out.mp4"), start=6.25)
    s2 = " ".join(cmd2)
    assert "-ss 6.25" in s2 and "-to" not in s2


def test_still_to_clip_cmd():
    from pathlib import Path
    from app.services.strategy_single_take.assemble import CUTAWAY_SECONDS, still_to_clip_cmd
    assert CUTAWAY_SECONDS == 1.2
    cmd = still_to_clip_cmd(Path("s.jpg"), Path("c.mp4"), seconds=1.2)
    s = " ".join(cmd)
    assert "-loop 1" in s
    assert "anullsrc" in s            # silent audio track
    assert "-t 1.2" in s


def test_normalize_clip_cmd_injects_silent_audio():
    from pathlib import Path
    from app.services.strategy_single_take.assemble import normalize_clip_cmd
    with_audio = " ".join(normalize_clip_cmd(Path("a.mp4"), Path("b.mp4"), has_audio=True))
    without = " ".join(normalize_clip_cmd(Path("a.mp4"), Path("b.mp4"), has_audio=False))
    assert "anullsrc" not in with_audio
    assert "anullsrc" in without      # Kling clips are silent
    assert "-shortest" in without


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
