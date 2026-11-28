"""Principal, OperationError, and OperationContext contracts (M6a)."""

from __future__ import annotations

import pytest
from mansio_core.authz import ChannelAuthorizer
from mansio_core.bus import Bus
from mansio_core.operations import (
    OPERATION_ERROR_KINDS,
    OperationContext,
    OperationError,
)
from mansio_core.principal import Principal, PrincipalKind


class TestPrincipalConstructors:
    def test_user_sets_the_id(self) -> None:
        principal = Principal.user("agent-1")
        assert principal.kind is PrincipalKind.USER
        assert principal.user_id == "agent-1"
        assert not principal.is_privileged

    @pytest.mark.parametrize(
        ("factory", "kind"),
        [
            (Principal.supertoken, PrincipalKind.SUPERTOKEN),
            (Principal.admin, PrincipalKind.ADMIN),
            (Principal.service, PrincipalKind.SERVICE),
            (Principal.no_auth, PrincipalKind.NO_AUTH),
        ],
    )
    def test_privileged_kinds_carry_no_id(self, factory, kind: PrincipalKind) -> None:
        principal = factory()
        assert principal.kind is kind
        assert principal.user_id is None
        assert principal.is_privileged

    def test_no_auth_is_distinct_from_supertoken(self) -> None:
        assert Principal.no_auth() != Principal.supertoken()
        assert Principal.no_auth().kind.value == "no_auth"

    def test_kind_set_is_closed(self) -> None:
        assert {kind.value for kind in PrincipalKind} == {
            "user",
            "supertoken",
            "admin",
            "service",
            "no_auth",
        }


class TestPrincipalInvariants:
    def test_user_requires_an_id(self) -> None:
        with pytest.raises(ValueError, match="non-empty user_id"):
            Principal(kind=PrincipalKind.USER, user_id=None)

    def test_user_requires_a_non_empty_id(self) -> None:
        with pytest.raises(ValueError, match="non-empty user_id"):
            Principal(kind=PrincipalKind.USER, user_id="")

    def test_user_id_must_match_the_pattern(self) -> None:
        with pytest.raises(ValueError, match="invalid user id"):
            Principal.user("Bad ID")
        with pytest.raises(ValueError, match="invalid user id"):
            Principal.user("ab")

    @pytest.mark.parametrize(
        "kind",
        [
            PrincipalKind.SUPERTOKEN,
            PrincipalKind.ADMIN,
            PrincipalKind.SERVICE,
            PrincipalKind.NO_AUTH,
        ],
    )
    def test_privileged_kinds_reject_an_id(self, kind: PrincipalKind) -> None:
        with pytest.raises(ValueError, match="carries no user_id"):
            Principal(kind=kind, user_id="agent-1")

    def test_principal_is_frozen_and_hashable(self) -> None:
        principal = Principal.user("agent-1")
        with pytest.raises(AttributeError):
            principal.user_id = "agent-2"  # type: ignore[misc]
        assert {principal, Principal.user("agent-1")} == {principal}


class TestOperationError:
    def test_carries_kind_and_message(self) -> None:
        error = OperationError("forbidden", "not yours")
        assert error.kind == "forbidden"
        assert error.message == "not yours"
        assert str(error) == "not yours"

    def test_rejects_an_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="unknown operation error kind"):
            OperationError("teapot", "no")  # type: ignore[arg-type]

    def test_kind_set_is_closed(self) -> None:
        assert OPERATION_ERROR_KINDS == (
            "bad_request",
            "unauthorized",
            "forbidden",
            "not_found",
            "conflict",
        )


class TestOperationContext:
    def test_holds_dependencies_and_the_no_auth_flag(self) -> None:
        bus = Bus()
        try:
            context = OperationContext(
                bus=bus,
                authz=ChannelAuthorizer(bus),
                token_store=None,
                allow_unauthenticated=True,
            )
        finally:
            bus.close()
        assert context.allow_unauthenticated is True
        assert context.token_store is None
