"""Word-by-word ASS captions for the single-take pipeline.

Recipe from vault wc_single_take v35/v36: per-word duration is
proportional to len(word)+1 across each sentence span; sentence spans
come from ffmpeg silencedetect over the voiceover track. Whisper audio
has a high noise floor — use -26dB/d=0.07 for ASMR, -25dB/d=0.12 for
normal voice (thresholds validated on v36, 2026-07-06).
"""
from __future__ import annotations

import re


SILENCE_ASMR = ("-26dB", 0.07)
SILENCE_NORMAL = ("-25dB", 0.12)

_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_END_RE = re.compile(r"silence_end:\s*([\d.]+)")
_TAG_RE = re.compile(r"\[[a-z ]+\]\s*", re.IGNORECASE)  # eleven_v3 audio tags
# GPT leaks «(пауза)»-ремарки despite the prompt ban; if they reach TTS
# the voice reads «пауза» aloud, and captions show it on screen
_REMARK_RE = re.compile(r"\(\s*[^)]*пауза[^)]*\)|\(\s*pause[^)]*\)", re.IGNORECASE)


def strip_stage_directions(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", _REMARK_RE.sub("", text or "")).strip()

ASS_HEADER = """[Script Info]
PlayResX: 720
PlayResY: 1280
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: cap,DejaVu Sans,58,&H00FFFFFF,&H00FFFFFF,&H96000000,&H96000000,-1,0,0,0,100,100,0,0,1,2,1,2,40,40,215,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def parse_silencedetect(stderr: str) -> list[tuple[float, float | None]]:
    """(start, end) pairs from ffmpeg silencedetect stderr.
    A trailing silence with no matching end yields (start, None)."""
    out: list[tuple[float, float | None]] = []
    pending: float | None = None
    for line in stderr.splitlines():
        m = _START_RE.search(line)
        if m:
            pending = float(m.group(1))
            continue
        m = _END_RE.search(line)
        if m and pending is not None:
            out.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:
        out.append((pending, None))
    return out


def speech_spans(
    silences: list[tuple[float, float | None]], total: float,
) -> list[tuple[float, float]]:
    """Invert silence intervals into speech intervals over [0, total]."""
    spans: list[tuple[float, float]] = []
    cur = 0.0
    for start, end in silences:
        if start - cur > 0.01:
            spans.append((cur, start))
        if end is None:
            return spans
        cur = end
    if total - cur > 0.01:
        spans.append((cur, total))
    return spans


def split_sentences(text: str) -> list[str]:
    """Split script into sentences; strips eleven_v3 [tags]. Keeps '?'
    (question intonation reads better on screen), drops '.'/'!'/'…'."""
    text = _TAG_RE.sub("", strip_stage_directions(text))
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out = []
    for p in parts:
        p = p.strip().rstrip(".!…").strip()
        if p:
            out.append(p)
    return out


def align_sentences(
    sentences: list[str], spans: list[tuple[float, float]],
) -> list[tuple[float, float, str]]:
    """Map sentences onto speech spans. Exact count match → zip;
    otherwise distribute proportionally to char length over the full
    voiced window with a fixed 0.25s inter-sentence gap."""
    if not sentences:
        return []
    if spans and len(spans) == len(sentences):
        return [(s, e, txt) for (s, e), txt in zip(spans, sentences)]

    lo = spans[0][0] if spans else 0.0
    hi = spans[-1][1] if spans else 0.0
    gap = 0.25
    window = max(hi - lo - gap * (len(sentences) - 1), 0.1)
    weights = [max(len(s), 1) for s in sentences]
    total_w = sum(weights)
    out: list[tuple[float, float, str]] = []
    t = lo
    for txt, w in zip(sentences, weights):
        d = window * w / total_w
        out.append((t, t + d, txt))
        t += d + gap
    return out


# part 1 of a cutaways script must end with a promise phrase
# («Сейчас открою…» / «Давайте попробуем…» per the script prompt)
_PROMISE_RE = re.compile(r"сейчас\s+откро|попробу", re.IGNORECASE)


def find_promise_end(
    aligned: list[tuple[float, float, str]],
) -> float | None:
    """End time of the promise sentence («Сейчас открою…»), if any."""
    for _, end, txt in aligned:
        if _PROMISE_RE.search(txt):
            return end
    return None


def pick_insert_gap(
    spans: list[tuple[float, float]], total: float,
    anchor: float | None = None,
) -> float | None:
    """Midpoint of a gap (≥0.08s) between speech spans. With an anchor
    (end of the promise sentence) — the gap nearest to it (job#7: the
    longest gap fell mid-script, way before the promise). Without —
    the longest gap whose midpoint is within 20%–85% of total; ASMR
    whisper has no ≥0.5s pause at all (job#6), so any inter-phrase
    micro-gap qualifies rather than silently dropping the cutaways."""
    cands: list[tuple[float, float]] = []  # (length, midpoint)
    for (_, e1), (s2, _) in zip(spans, spans[1:]):
        length = s2 - e1
        if length >= 0.08:
            cands.append((length, (e1 + s2) / 2))
    if anchor is not None:
        near = [c for c in cands if 0.1 * total <= c[1] <= 0.9 * total]
        if near:
            return min(near, key=lambda c: abs(c[1] - anchor))[1]
    windowed = [c for c in cands if 0.2 * total <= c[1] <= 0.85 * total]
    if not windowed:
        return None
    return max(windowed, key=lambda c: c[0])[1]


def shift_captions(
    aligned: list[tuple[float, float, str]],
    split_at: float,
    inserts_seconds: float,
) -> list[tuple[float, float, str]]:
    """Shift sentences that start at/after the split right by the total
    insert duration (inserts are pushed into the timeline at split_at)."""
    return [
        (s + inserts_seconds, e + inserts_seconds, t) if s >= split_at
        else (s, e, t)
        for s, e, t in aligned
    ]


def _ts(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(sent_spans: list[tuple[float, float, str]]) -> str:
    """Word-by-word Dialogue lines, per-word duration ∝ len(word)+1."""
    lines: list[str] = []
    for start, end, text in sent_spans:
        words = text.split()
        weights = [len(w) + 1 for w in words]
        total = sum(weights)
        t = start
        for w, wt in zip(words, weights):
            d = (end - start) * wt / total
            lines.append(
                f"Dialogue: 0,{_ts(t)},{_ts(t + d)},cap,,0,0,0,,{w}"
            )
            t += d
    return ASS_HEADER + "\n".join(lines) + "\n"
