"""Regression tests for NATSBackend.info()/stats() and the num_subjects guard.

Unlike test_nats_backend.py, these tests do not require a running NATS
server: they stub out the JetStreamContext.stream_info() call directly,
so they exercise NATSBackend.info()/stats() and the shared
_subject_count() helper in isolation.

Context (see issue #293): the installed nats-py version determines
whether JetStream's StreamState carries a num_subjects field at all.
stats() already guarded the access with hasattr(); info() read it
unguarded and raised AttributeError whenever num_subjects was absent.
"""

from __future__ import annotations

import pytest

nats_py = pytest.importorskip("nats")  # noqa: E402

import nats.errors  # noqa: E402
from nats.js.api import StreamState  # noqa: E402

from mansio.backends.nats import NATSBackend, _subject_count  # noqa: E402


class _FakeStreamInfo:
    """Minimal stand-in for nats.js.api.StreamInfo, carrying only .state."""

    def __init__(self, state: object) -> None:
        self.state = state


class _FakeEmptySubscription:
    """Stand-in for a pull subscription with no messages available.

    fetch() times out immediately, matching how _fetch_batch()
    interprets an empty/exhausted stream.
    """

    async def fetch(self, batch: int, timeout: float) -> list:
        raise nats.errors.TimeoutError()

    async def unsubscribe(self) -> None:
        pass


class _FakeJetStream:
    """Stand-in for JetStreamContext, covering only what info()/stats() use."""

    def __init__(self, state: object) -> None:
        self._state = state

    async def stream_info(self, stream_name: str) -> _FakeStreamInfo:
        return _FakeStreamInfo(self._state)

    async def pull_subscribe(
        self, subject: str, stream: str | None = None
    ) -> _FakeEmptySubscription:
        return _FakeEmptySubscription()


def _wire_fake_js(backend: NATSBackend, state: object) -> None:
    """Mark a backend as connected with a fake JetStream context.

    Bypasses backend.connect() entirely, so no real NATS server is
    needed to exercise info()/stats() logic.
    """
    backend._connected = True
    backend._js = _FakeJetStream(state)


def _make_state_without_num_subjects() -> StreamState:
    """Build a real StreamState as returned by the installed nats-py.

    StreamState's constructor here does not accept num_subjects at
    all (it is absent from this nats-py version's schema), which is
    exactly the trigger described in issue #293: info() read
    state.num_subjects unconditionally and raised AttributeError.
    """
    assert not hasattr(
        StreamState(messages=0, bytes=0, first_seq=0, last_seq=0, consumer_count=0), "num_subjects"
    )
    return StreamState(messages=5, bytes=1234, first_seq=1, last_seq=5, consumer_count=0)


class _StateWithNumSubjects:
    """Simulates a newer nats-py/nats-server StreamState that does carry num_subjects."""

    def __init__(self, num_subjects: int) -> None:
        self.messages = 5
        self.bytes = 1234
        self.first_seq = 1
        self.last_seq = 5
        self.num_subjects = num_subjects


class TestSubjectCountHelper:
    """Unit tests for the shared _subject_count() helper."""

    def test_missing_attribute_returns_zero(self):
        state = _make_state_without_num_subjects()
        assert _subject_count(state) == 0

    def test_present_attribute_is_returned(self):
        state = _StateWithNumSubjects(num_subjects=3)
        assert _subject_count(state) == 3


class TestNATSBackendInfoStreamStateGuard:
    """Regression tests for info() with a StreamState lacking num_subjects."""

    def test_info_does_not_raise_without_num_subjects(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_INFO_GUARD")
        _wire_fake_js(backend, _make_state_without_num_subjects())

        info = backend.info()

        assert info["connected"] is True
        assert info["total_messages"] == 5
        assert info["total_bytes"] == 1234
        assert info["total_channels"] == 0
        assert info["first_seq"] == 1
        assert info["last_seq"] == 5

    def test_info_reports_num_subjects_when_present(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_INFO_GUARD")
        _wire_fake_js(backend, _StateWithNumSubjects(num_subjects=7))

        info = backend.info()

        assert info["total_channels"] == 7


class TestNATSBackendStatsStreamStateGuard:
    """Same StreamState shapes exercised through stats(), for parity with info()."""

    def test_stats_total_channels_without_num_subjects_falls_back_to_breakdown(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_STATS_GUARD")
        _wire_fake_js(backend, _make_state_without_num_subjects())

        stats = backend.stats()

        # total_channels falls back to len(breakdown) (0, no messages fetched)
        # via `total_channels or len(breakdown)` in stats().
        assert stats["total_channels"] == 0

    def test_stats_total_channels_with_num_subjects(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_STATS_GUARD")
        _wire_fake_js(backend, _StateWithNumSubjects(num_subjects=4))

        stats = backend.stats()

        assert stats["total_channels"] == 4
