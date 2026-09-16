"""Communal picker must stay on the caller's network primary zones."""
from types import SimpleNamespace

from app.models.owner import AccountType, OwnerRole
from app.services.communal_zone_service import (
    is_zone_public,
    list_public_defining_zones,
    zone_eligible_for_communal_assignment,
)


def _owner(
    *,
    owner_id: int,
    zone_id: str,
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
    )


def _zone(
    *,
    zone_id: str,
    creator_id: int,
    is_primary: bool,
    contract_type: str = "geofence",
    active: bool = True,
    is_public: bool = True,
):
    return SimpleNamespace(
        id=1,
        zone_id=zone_id,
        creator_id=creator_id,
        owner_id=creator_id,
        active=active,
        is_primary=is_primary,
        parameters={
            "contractType": contract_type,
            "config": {"is_public": is_public},
        },
    )


def test_is_zone_public_requires_defining_type():
    defining = _zone(zone_id="N1", creator_id=1, is_primary=True)
    communal = _zone(
        zone_id="N1", creator_id=1, is_primary=True, contract_type="communal_id"
    )
    assert is_zone_public(defining) is True
    assert is_zone_public(communal) is False


def test_eligible_primary_same_network_only():
    admin = _owner(owner_id=1, zone_id="NET-A")
    member = _owner(
        owner_id=2,
        zone_id="NET-A",
        role=OwnerRole.USER,
        account_type=AccountType.EXCLUSIVE,
        account_owner_id=1,
    )
    same_primary = _zone(zone_id="NET-A", creator_id=1, is_primary=True)
    other_primary = _zone(zone_id="NET-B", creator_id=9, is_primary=True)
    same_secondary = _zone(zone_id="NET-A", creator_id=2, is_primary=False)

    assert zone_eligible_for_communal_assignment(admin, same_primary) is True
    assert zone_eligible_for_communal_assignment(member, same_primary) is True
    assert zone_eligible_for_communal_assignment(admin, other_primary) is False
    assert zone_eligible_for_communal_assignment(member, other_primary) is False
    assert zone_eligible_for_communal_assignment(admin, same_secondary) is False
    assert zone_eligible_for_communal_assignment(member, same_secondary) is False


def test_individual_may_use_own_defining_secondary():
    solo = _owner(
        owner_id=5,
        zone_id="SOLO-5",
        role=OwnerRole.USER,
        account_type=AccountType.EXCLUSIVE,
        account_owner_id=None,
    )
    own = _zone(zone_id="SOLO-5", creator_id=5, is_primary=False)
    other = _zone(zone_id="SOLO-5", creator_id=99, is_primary=False)
    assert zone_eligible_for_communal_assignment(solo, own) is True
    assert zone_eligible_for_communal_assignment(solo, other) is False


def test_list_public_defining_zones_filters_by_owner_network(monkeypatch):
    admin = _owner(owner_id=1, zone_id="NET-A")
    rows = [
        _zone(zone_id="NET-A", creator_id=1, is_primary=True),
        _zone(zone_id="NET-B", creator_id=2, is_primary=True),
        _zone(zone_id="NET-A", creator_id=3, is_primary=False),
    ]
    # Stamp distinct ids for assertions.
    for index, row in enumerate(rows, start=10):
        row.id = index

    class _Query:
        def filter(self, *args, **kwargs):
            del args, kwargs
            return self

        def order_by(self, *args, **kwargs):
            del args, kwargs
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            del model
            return _Query()

    listed = list_public_defining_zones(_Db(), owner=admin, skip=0, limit=50)
    assert [z.id for z in listed] == [10]
