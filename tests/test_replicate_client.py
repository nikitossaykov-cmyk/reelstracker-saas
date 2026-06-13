"""Unit tests for the Replicate wrapper.

Stubs out the `replicate` module via sys.modules so no network is
involved and no API key is required to run these.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

from app.services.replicate_client import (
    ReplicateClient,
    ReplicateError,
    ReplicateSafetyError,
    ReplicateTransientError,
)


def _install_fake_replicate(side_effect_or_return):
    fake = types.ModuleType("replicate")
    if callable(side_effect_or_return) or isinstance(
        side_effect_or_return, Exception
    ):
        fake.run = MagicMock(side_effect=side_effect_or_return)
    else:
        fake.run = MagicMock(return_value=side_effect_or_return)
    sys.modules["replicate"] = fake
    return fake


def test_requires_api_key():
    with pytest.raises(ValueError):
        ReplicateClient(api_key="")


def test_happy_path_returns_replicate_output():
    _install_fake_replicate(["https://r/out.png"])
    c = ReplicateClient(api_key="k")
    assert c.run_model("m", {"prompt": "x"}) == ["https://r/out.png"]


def test_safety_pattern_in_message_raises_safety_error():
    _install_fake_replicate(
        Exception("nsfw content flagged by safety policy")
    )
    c = ReplicateClient(api_key="k")
    with pytest.raises(ReplicateSafetyError):
        c.run_model("m", {"prompt": "x"})


def test_5xx_in_message_raises_transient_error():
    _install_fake_replicate(Exception("upstream 502 bad gateway"))
    c = ReplicateClient(api_key="k")
    with pytest.raises(ReplicateTransientError):
        c.run_model("m", {"prompt": "x"})


def test_unknown_error_is_terminal():
    _install_fake_replicate(Exception("invalid model reference"))
    c = ReplicateClient(api_key="k")
    with pytest.raises(ReplicateError) as ei:
        c.run_model("m", {"prompt": "x"})
    # not safety, not transient — must be the base ReplicateError
    assert not isinstance(ei.value, ReplicateSafetyError)
    assert not isinstance(ei.value, ReplicateTransientError)
