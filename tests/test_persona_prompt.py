"""Unit tests for the persona prompt builder.

No DB, no network. Verifies the moderation-safe frame is present and
positioned before the user bio (the property that protects PuLID-Flux
moderation from misreading risky-sounding bios as malicious intent).
"""
from app.services.persona_prompt import (
    SAFE_DEFAULTS,
    build_persona_prompt,
    defensive_fallback_prompt,
)


def test_includes_defensive_safe_phrases():
    out = build_persona_prompt(
        "блондинка 23 года, голубые глаза", style="studio"
    )
    low = out.lower()
    for phrase in (
        "fictional",
        "generic",
        "no real-person likeness",
        "no logos of existing brands",
    ):
        assert phrase in low, f"missing safety phrase: {phrase}"


def test_includes_user_bio_verbatim_after_safety_frame():
    bio = "блондинка 23 года"
    out = build_persona_prompt(bio, style=None)
    assert bio in out
    assert out.index("fictional") < out.index(bio)


def test_style_hint_appended_when_present():
    out = build_persona_prompt("девушка", style="editorial")
    assert "editorial" in out.lower()
    out2 = build_persona_prompt("девушка", style=None)
    assert "editorial" not in out2.lower()


def test_truncates_to_safe_length():
    huge = "x" * 5000
    out = build_persona_prompt(huge, style=None)
    assert len(out) <= 1000


def test_unknown_style_silently_dropped():
    """An unknown style_hint should not crash — it just isn't appended.

    Validation against the allowed set happens in the service layer, not
    the prompt builder, so this layer must be permissive.
    """
    out = build_persona_prompt("девушка", style="not-a-real-style")
    assert "not-a-real-style" not in out


def test_defensive_fallback_is_user_free():
    fb = defensive_fallback_prompt()
    assert fb == SAFE_DEFAULTS
    for risky in ("teen", "child", "young teenager"):
        assert risky not in fb.lower()
