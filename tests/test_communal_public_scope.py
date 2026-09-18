"""Communal assignment eligibility — admin + primary only."""
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
    owner_id: int | None = None,
    contract_type: str = "geofence",
    active: bool = True,
    is_public: bool = True,
):
    resolved_owner = creator_id if owner_id is None else owner_id
    return SimpleNamespace(
        id=1,
        zone_id=zone_id,
        creator_id=creator_id,
        owner_id=resolved_owner,
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


def test_eligible_primary_admin_only():
    admin = _owner(owner_id=1, zone_id="NET-A")
    member = _owner(
        owner_id=2,
        zone_id="NET-A",
        role=OwnerRole.USER,
        account_type=AccountType.EXCLUSIVE,
        account_owner_id=1,
    )
    same_primary = _zone(zone_id="NET-A", creator_id=1, owner_id=1, is_primary=True)
    other_primary = _zone(zone_id="NET-B", creator_id=9, owner_id=9, is_primary=True)
    same_secondary = _zone(zone_id="NET-A", creator_id=2, owner_id=2, is_primary=False)
    mismatched_network_id = _zone(
        zone_id="OTHER-STRING", creator_id=1, owner_id=1, is_primary=True
    )

    account_ids = [1, 2]
    assert (
        zone_eligible_for_communal_assignment(
            admin, same_primary, account_owner_ids=account_ids
        )
        is True
    )
    assert (
        zone_eligible_for_communal_assignment(
            member, same_primary, account_owner_ids=account_ids
        )
        is False
    )
    assert (
        zone_eligible_for_communal_assignment(
            admin, other_primary, account_owner_ids=account_ids
        )
        is False
    )
    assert (
        zone_eligible_for_communal_assignment(
            admin, same_secondary, account_owner_ids=account_ids
        )
        is False
    )
    assert (
        zone_eligible_for_communal_assignment(
            admin, mismatched_network_id, account_owner_ids=account_ids
        )
        is True
    )


def test_individual_cannot_attach_communal_ids():
    solo = _owner(
        owner_id=5,
        zone_id="SOLO-5",
        role=OwnerRole.USER,
        account_type=AccountType.EXCLUSIVE,
        account_owner_id=None,
    )
    own = _zone(zone_id="SOLO-5", creator_id=5, owner_id=5, is_primary=False)
    admin_primary = _zone(
        zone_id="ADMIN-NET", creator_id=1, owner_id=5, is_primary=True
    )
    assert (
        zone_eligible_for_communal_assignment(solo, own, account_owner_ids=[5])
        is False
    )
    assert (
        zone_eligible_for_communal_assignment(
            solo, admin_primary, account_owner_ids=[5]
        )
        is False
    )


def test_list_public_defining_zones_filters_by_account_owners(monkeypatch):
    admin = _owner(owner_id=1, zone_id="NET-A")
    rows = [
        _zone(zone_id="NET-A", creator_id=1, owner_id=1, is_primary=True),
        _zone(zone_id="NET-B", creator_id=2, owner_id=2, is_primary=True),
        _zone(zone_id="NET-A", creator_id=3, owner_id=3, is_primary=False),
    ]
    for index, row in enumerate(rows, start=10):
        row.id = index

    class _Query:
        def __init__(self):
            self._owner_ids = None

        def filter(self, *args, **kwargs):
            del kwargs
            for expr in args:
                in_values = getattr(expr, "right", None)
                if in_values is not None and hasattr(in_values, "value"):
                    self._owner_ids = set(in_values.value)
            return self

        def order_by(self, *args, **kwargs):
            del args, kwargs
            return self

        def all(self):
            if self._owner_ids is None:
                return rows
            return [z for z in rows if int(z.owner_id) in self._owner_ids]

    class _Db:
        def query(self, model):
            del model
            return _Query()

    monkeypatch.setattr(
        "app.services.access_policy.zone_listing_owner_ids",
        lambda db, owner: [1],
    )

    listed = list_public_defining_zones(_Db(), owner=admin, skip=0, limit=50)
    assert [z.id for z in listed] == [10]
