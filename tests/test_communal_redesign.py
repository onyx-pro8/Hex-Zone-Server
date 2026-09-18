"""Unit tests for the redesigned Communal ID registry + multi-ID tagging."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.owner import AccountType, OwnerRole
from app.services.communal_zone_service import (
    apply_communal_ids_to_config,
    assert_communal_ids_attachable,
    assert_may_generate_communal_id,
    assign_owner_communal_id,
    extract_communal_ids_from_config,
    get_communal_ids,
    zone_eligible_for_communal_assignment,
)


def _owner(
    *,
    owner_id: int = 1,
    zone_id: str = "NET-A",
    role: OwnerRole = OwnerRole.ADMINISTRATOR,
    account_type: AccountType = AccountType.PRIVATE_PLUS,
    account_owner_id: int | None = None,
):
    return SimpleNamespace(
        id=owner_id,
        zone_id=zone_id,
        role=role,
        account_type=account_type,
        account_owner_id=account_owner_id,
        communal_id=None,
    )


def _zone(
    *,
    zone_id: str = "NET-A",
    creator_id: int = 1,
    owner_id: int | None = None,
    is_primary: bool = True,
    communal_ids: list[str] | None = None,
    communal_id: str | None = None,
):
    config: dict = {"is_public": True}
    if communal_ids:
        config["communal_ids"] = communal_ids
        config["communal_id"] = communal_ids[0]
    elif communal_id:
        config["communal_id"] = communal_id
    return SimpleNamespace(
        id=1,
        zone_id=zone_id,
        creator_id=creator_id,
        owner_id=creator_id if owner_id is None else owner_id,
        active=True,
        is_primary=is_primary,
        parameters={"contractType": "geofence", "config": config},
    )


def test_extract_and_apply_multi_communal_ids():
    config = {"communal_ids": ["comm-aaa", "COMM-BBB"], "communal_id": "COMM-CCC"}
    ids = extract_communal_ids_from_config(config)
    assert ids == ["COMM-AAA", "COMM-BBB", "COMM-CCC"]
    written = apply_communal_ids_to_config({}, ["COMM-AAA", "COMM-BBB"])
    assert written["communal_ids"] == ["COMM-AAA", "COMM-BBB"]
    assert written["communal_id"] == "COMM-AAA"


def test_get_communal_ids_reads_legacy_and_multi():
    zone = _zone(communal_id="COMM-LEGACY")
    assert get_communal_ids(zone) == ["COMM-LEGACY"]
    zone2 = _zone(communal_ids=["COMM-A", "COMM-B"])
    assert get_communal_ids(zone2) == ["COMM-A", "COMM-B"]


def test_assign_owner_communal_id_no_longer_mints(db=None):
    owner = _owner(account_type=AccountType.EXCLUSIVE, role=OwnerRole.USER)
    # db unused — function is a no-op without minting
    assert assign_owner_communal_id(SimpleNamespace(), owner) == ""


def test_assert_may_generate_admin_only():
    admin = _owner()
    assert_may_generate_communal_id(admin)

    member = _owner(
        owner_id=2,
        role=OwnerRole.USER,
        account_type=AccountType.EXCLUSIVE,
        account_owner_id=1,
    )
    with pytest.raises(HTTPException) as exc:
        assert_may_generate_communal_id(member)
    assert exc.value.status_code == 403


def test_zone_eligible_primary_admin_only():
    admin = _owner()
    member = _owner(
        owner_id=2,
        role=OwnerRole.USER,
        account_type=AccountType.EXCLUSIVE,
        account_owner_id=1,
    )
    primary = _zone(is_primary=True, owner_id=1)
    secondary = _zone(is_primary=False, owner_id=1, creator_id=1)
    assert zone_eligible_for_communal_assignment(admin, primary, account_owner_ids=[1, 2]) is True
    assert zone_eligible_for_communal_assignment(admin, secondary, account_owner_ids=[1, 2]) is False
    assert zone_eligible_for_communal_assignment(member, primary, account_owner_ids=[1, 2]) is False


def test_assert_communal_ids_require_primary(monkeypatch):
    admin = _owner()

    class _Db:
        pass

    monkeypatch.setattr(
        "app.services.communal_zone_service.registry_communal_id_taken",
        lambda db, cid: cid == "COMM-OK",
    )
    monkeypatch.setattr(
        "app.services.communal_zone_service.zone_communal_id_taken",
        lambda db, cid: False,
    )

    assert assert_communal_ids_attachable(_Db(), admin, [], is_primary=False) == []
    with pytest.raises(HTTPException):
        assert_communal_ids_attachable(_Db(), admin, ["COMM-OK"], is_primary=False)
    assert assert_communal_ids_attachable(
        _Db(), admin, ["COMM-OK"], is_primary=True
    ) == ["COMM-OK"]
    with pytest.raises(HTTPException):
        assert_communal_ids_attachable(
            _Db(), admin, ["COMM-MISSING"], is_primary=True
        )
