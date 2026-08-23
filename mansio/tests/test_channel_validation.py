"""Tests for channel name validation in HttpFrontend.

Verifies:
- User channel naming rules (length, start/end chars, specials)
- Trailing underscore rejection (consistent with other trailing specials)
- 64-char boundary (stated max is 64, not 63)
- Reserved prefix enforcement
- Unicode channel names
"""

from __future__ import annotations

import pytest

from mansio.frontends.http import _validate_channel_name


class TestValidateChannelName:
    """Unit tests for _validate_channel_name."""

    # --- Valid user channels ---

    @pytest.mark.parametrize(
        "name",
        [
            "abc",  # minimum length
            "general",  # typical name
            "dev-log",  # hyphens
            "test.channel",  # dots
            "test_channel",  # underscores in middle
            "a" * 64,  # maximum length
            "ab1",  # ends with digit
            "café",  # Unicode (French)
            "проект",  # Unicode (Russian)
            "中文频道",  # Unicode (Chinese)
            "テスト",  # Unicode (Japanese)
            "über-cool",  # Unicode (German)
            "my_test.chan-1",  # mixed separators
        ],
    )
    def test_valid_user_channel(self, name: str) -> None:
        assert _validate_channel_name(name) is None

    # --- Invalid: length ---

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "a",  # 1 char
            "ab",  # 2 chars
            "a" * 65,  # over max
            "a" * 128,  # way over max
        ],
    )
    def test_reject_bad_length(self, name: str) -> None:
        result = _validate_channel_name(name)
        assert result is not None
        assert result[1] == 400

    # --- Invalid: start character ---

    @pytest.mark.parametrize(
        "name",
        [
            "1starts-digit",
            "-starts-hyphen",
            ".starts-dot",
            "_starts-under",
        ],
    )
    def test_reject_bad_start(self, name: str) -> None:
        result = _validate_channel_name(name)
        assert result is not None
        assert result[1] == 400

    # --- Invalid: trailing special characters ---

    @pytest.mark.parametrize(
        "name",
        [
            "abc-",  # trailing hyphen
            "abc.",  # trailing dot
            "abc_",  # trailing underscore
        ],
    )
    def test_reject_trailing_special(self, name: str) -> None:
        result = _validate_channel_name(name)
        assert result is not None
        assert result[1] == 400

    # --- Invalid: format ---

    @pytest.mark.parametrize(
        "name",
        [
            "UPPERCASE",
            "has spaces",
            "has\ttab",
            "test::double",  # consecutive colons (won't match anyway)
            "test--double",  # consecutive hyphens
            "test..double",  # consecutive dots
            "test__double",  # consecutive underscores
            "test.-mixed",  # consecutive mixed specials
        ],
    )
    def test_reject_bad_format(self, name: str) -> None:
        result = _validate_channel_name(name)
        assert result is not None
        assert result[1] == 400

    # --- Reserved prefixes pass name validation (auth enforced elsewhere) ---

    def test_reserved_prefix_passes_name_validation(self) -> None:
        """broadcast: is admin-only, but _validate_channel_name only checks format.
        Auth enforcement happens in _validate_and_auth_publish."""
        assert _validate_channel_name("broadcast:test") is None

    # --- Valid system channels ---

    @pytest.mark.parametrize(
        "name",
        [
            "_system:agents",
            "dm:alice:bob",
            "notebook:elena",
            "memory:elena",
            "broadcast:releases",
        ],
    )
    def test_valid_system_channel(self, name: str) -> None:
        assert _validate_channel_name(name) is None

    # --- Boundary: exactly 64 chars ---

    def test_64_char_boundary(self) -> None:
        """64 chars should be accepted (stated max), 65 rejected."""
        assert _validate_channel_name("a" * 64) is None
        result = _validate_channel_name("a" * 65)
        assert result is not None
        assert result[1] == 400

    # --- Trailing underscore consistency ---

    def test_trailing_special_consistency(self) -> None:
        """All trailing specials (hyphen, dot, underscore) should be rejected."""
        for suffix in ["-", ".", "_"]:
            name = "test" + suffix
            result = _validate_channel_name(name)
            assert result is not None, f"trailing '{suffix}' should be rejected"
            assert result[1] == 400
