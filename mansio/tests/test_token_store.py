"""Tests for TokenStore — SQLite-backed agent token management."""

from __future__ import annotations

from pathlib import Path

import pytest

from mansio.token_store import TokenStore


@pytest.fixture()
def store(tmp_path: Path) -> TokenStore:
    """Create a TokenStore with a temp database."""
    return TokenStore(str(tmp_path / "test.db"))


class TestCreateAndValidate:
    """Token creation and validation."""

    def test_create_returns_plaintext(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice", "Alice's bot")
        assert entry["token"].startswith("mst-")
        assert entry["user_id"] == "agent-alice"
        assert entry["label"] == "Alice's bot"
        assert entry["id"]
        assert entry["created_at"]
        assert entry["last_used_at"] is None

    def test_validate_correct_token(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice")
        result = store.validate(entry["token"])
        assert result == "agent-alice"

    def test_validate_wrong_token(self, store: TokenStore) -> None:
        store.create_token("agent-alice")
        assert store.validate("mst-wrong") is False

    def test_validate_empty_string(self, store: TokenStore) -> None:
        assert store.validate("") is False

    def test_validate_no_prefix(self, store: TokenStore) -> None:
        assert store.validate("not-a-valid-token") is False

    def test_validate_legacy_prefix(self, store: TokenStore) -> None:
        """Legacy pzt- tokens created before the rename are still accepted."""
        import hashlib
        import secrets

        token = f"pzt-{secrets.token_hex(24)}"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn = store._conn()
        conn.execute(
            "INSERT INTO tokens (id, token_hash, token_prefix, user_id, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy01", token_hash, token[:8], "agent-legacy", "old token", "2024-01-01"),
        )
        conn.commit()
        assert store.validate(token) == "agent-legacy"

    def test_validate_updates_last_used_at(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice")
        assert entry["last_used_at"] is None

        store.validate(entry["token"])

        tokens = store.list_tokens()
        assert tokens[0]["last_used_at"] is not None

    def test_plaintext_not_stored(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice")
        tokens = store.list_tokens()
        # list_tokens should not expose the hash or plaintext
        assert "token" not in tokens[0]
        assert "token_hash" not in tokens[0]
        # but should have the prefix for display
        assert tokens[0]["token_prefix"] == entry["token"][:8]


class TestSupertoken:
    """Supertoken (user_id=None) behavior."""

    def test_create_supertoken(self, store: TokenStore) -> None:
        entry = store.create_token(user_id=None, label="admin token")
        assert entry["user_id"] is None

    def test_validate_supertoken_returns_none(self, store: TokenStore) -> None:
        entry = store.create_token(user_id=None)
        result = store.validate(entry["token"])
        # None means supertoken (wildcard), not "invalid"
        assert result is None


class TestRejectEmptyAgentId:
    """Empty-string user_id must be rejected (fixes #106)."""

    def test_empty_string_raises(self, store: TokenStore) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            store.create_token(user_id="")

    def test_whitespace_only_raises(self, store: TokenStore) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            store.create_token(user_id="   ")

    def test_none_still_allowed(self, store: TokenStore) -> None:
        entry = store.create_token(user_id=None)
        assert entry["user_id"] is None


class TestDelete:
    """Token deletion."""

    def test_delete_existing(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice")
        assert store.delete_token(entry["id"]) is True
        assert store.validate(entry["token"]) is False

    def test_delete_nonexistent(self, store: TokenStore) -> None:
        assert store.delete_token("nonexistent") is False

    def test_delete_removes_from_list(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice")
        store.delete_token(entry["id"])
        assert len(store.list_tokens()) == 0


class TestRotate:
    """Token rotation."""

    def test_rotate_generates_new_token(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice", "bot")
        old_token = entry["token"]

        result = store.rotate_token(entry["id"])
        assert result is not None
        assert result["token"] != old_token
        assert result["token"].startswith("mst-")
        # Metadata preserved
        assert result["user_id"] == "agent-alice"
        assert result["label"] == "bot"
        assert result["id"] == entry["id"]

    def test_rotate_invalidates_old(self, store: TokenStore) -> None:
        entry = store.create_token("agent-alice")
        old_token = entry["token"]

        result = store.rotate_token(entry["id"])
        assert result is not None
        assert store.validate(old_token) is False
        assert store.validate(result["token"]) == "agent-alice"

    def test_rotate_nonexistent(self, store: TokenStore) -> None:
        assert store.rotate_token("nonexistent") is None


class TestListTokens:
    """Token listing."""

    def test_empty(self, store: TokenStore) -> None:
        assert store.list_tokens() == []

    def test_multiple_tokens(self, store: TokenStore) -> None:
        store.create_token("agent-alice", "alice")
        store.create_token("agent-bob", "bob")
        tokens = store.list_tokens()
        assert len(tokens) == 2

    def test_has_tokens(self, store: TokenStore) -> None:
        assert store.has_tokens() is False
        store.create_token("agent-alice")
        assert store.has_tokens() is True


class TestMultipleTokensPerAgent:
    """An agent can have multiple tokens."""

    def test_two_tokens_same_agent(self, store: TokenStore) -> None:
        e1 = store.create_token("agent-alice", "token 1")
        e2 = store.create_token("agent-alice", "token 2")
        assert e1["token"] != e2["token"]
        assert store.validate(e1["token"]) == "agent-alice"
        assert store.validate(e2["token"]) == "agent-alice"

    def test_delete_one_keeps_other(self, store: TokenStore) -> None:
        e1 = store.create_token("agent-alice", "token 1")
        e2 = store.create_token("agent-alice", "token 2")
        store.delete_token(e1["id"])
        assert store.validate(e1["token"]) is False
        assert store.validate(e2["token"]) == "agent-alice"
