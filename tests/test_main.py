"""Tests for registration and H3 conversion."""
import pytest
from httpx import AsyncClient
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.database import Base, get_db
from app.core.h3_utils import lat_lng_to_h3_cell, validate_h3_cell
from app.crud.zone import geojson_to_wkt

# Test database URL
TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def test_db():
    """Create test database session."""
    engine = create_engine(TEST_DATABASE_URL, echo=False)
    testing_session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Create tables
    Base.metadata.create_all(bind=engine)

    with testing_session_maker() as session:
        yield session

    # Drop tables
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def override_get_db(test_db):
    """Override the get_db dependency."""

    def _override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_registration(test_db, override_get_db):
    """Test owner registration."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/owners/register",
            json={
                "email": "test@example.com",
                "zone_id": "zone-user-1",
                "first_name": "Test",
                "last_name": "User",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Test Address 1",
            },
        )
        
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "test@example.com"
        assert data["zone_id"] == "zone-user-1"
        assert data["first_name"] == "Test"
        assert data["last_name"] == "User"
        assert data["account_type"] == "private"
        assert "api_key" in data
        assert data["active"] is True


@pytest.mark.asyncio
async def test_owner_registration_duplicate_email(test_db, override_get_db):
    """Test owner registration with duplicate email."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        # First registration
        await client.post(
            "/owners/register",
            json={
                "email": "test@example.com",
                "zone_id": "zone-user-1",
                "first_name": "Test",
                "last_name": "User",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Test Address 1",
            },
        )
        
        # Second registration with same email
        response = await client.post(
            "/owners/register",
            json={
                "email": "test@example.com",
                "zone_id": "zone-user-2",
                "first_name": "Test2",
                "last_name": "User2",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Test Address 2",
            },
        )
        
        assert response.status_code == 409
        assert "already registered" in response.json()["detail"]


@pytest.mark.asyncio
async def test_owner_login(test_db, override_get_db):
    """Test owner login."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Register
        await client.post(
            "/owners/register",
            json={
                "email": "test@example.com",
                "zone_id": "zone-user-1",
                "first_name": "Test",
                "last_name": "User",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Test Address 1",
            },
        )
        
        # Login
        response = await client.post(
            "/owners/login",
            json={
                "email": "test@example.com",
                "password": "SecurePassword123",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "owner_id" in data


@pytest.mark.asyncio
async def test_owner_login_invalid_password(test_db, override_get_db):
    """Test owner login with invalid password."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Register
        await client.post(
            "/owners/register",
            json={
                "email": "test@example.com",
                "zone_id": "zone-user-1",
                "first_name": "Test",
                "last_name": "User",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Test Address 1",
            },
        )
        
        # Login with wrong password
        response = await client.post(
            "/owners/login",
            json={
                "email": "test@example.com",
                "password": "WrongPassword",
            },
        )
        
        assert response.status_code == 401


class TestH3Conversion:
    """Test H3 conversion utilities."""
    
    def test_lat_lng_to_h3_cell(self):
        """Test latitude/longitude to H3 cell conversion."""
        lat, lng = 37.7749, -122.4194  # San Francisco
        h3_cell = lat_lng_to_h3_cell(lat, lng)
        
        assert isinstance(h3_cell, str)
        assert validate_h3_cell(h3_cell)
    
    def test_h3_cell_resolution_default(self):
        """Test H3 cell with default resolution."""
        lat, lng = 40.7128, -74.0060  # New York
        h3_cell = lat_lng_to_h3_cell(lat, lng)
        
        assert validate_h3_cell(h3_cell)
    
    def test_h3_cell_custom_resolution(self):
        """Test H3 cell with custom resolution."""
        lat, lng = 51.5074, -0.1278  # London
        h3_cell = lat_lng_to_h3_cell(lat, lng, resolution=8)
        
        assert validate_h3_cell(h3_cell)
    
    def test_invalid_h3_cell(self):
        """Test validation of invalid H3 cell."""
        assert not validate_h3_cell("invalid_cell")
        assert not validate_h3_cell("")

    def test_geojson_to_wkt_multipolygon(self):
        """GeoJSON MultiPolygon should convert to WKT."""
        geojson = {
            "type": "MultiPolygon",
            "coordinates": [[[[
                -73.9809036254883, 40.85409494874863
            ], [
                -74.0687942504883, 40.80943034560593
            ], [
                -73.93249511718751, 40.74757738563813
            ], [
                -73.8710403442383, 40.829429265624036
            ], [
                -73.9809036254883, 40.85409494874863
            ]]]]
        }
        wkt = geojson_to_wkt(geojson)
        assert wkt.startswith("MULTIPOLYGON((")
        assert "-73.9809036254883 40.85409494874863" in wkt
        assert wkt.endswith("))")

    def test_geojson_to_geometry_ewkt(self):
        """GeoJSON polygon should convert to SRID EWKT string."""
        from app.crud.zone import _geojson_to_geometry

        geojson = {
            "type": "Polygon",
            "coordinates": [[
                [-73.9809036254883, 40.85409494874863],
                [-74.0687942504883, 40.80943034560593],
                [-73.93249511718751, 40.74757738563813],
                [-73.9809036254883, 40.85409494874863],
            ]]
        }

        ewkt = _geojson_to_geometry(geojson)
        assert isinstance(ewkt, str)
        assert ewkt.startswith("SRID=4326;")
        assert "MULTIPOLYGON(((" in ewkt
        assert "-73.9809036254883 40.85409494874863" in ewkt

    def test_zone_model_geojson_validator(self):
        """Zone model should convert dict GeoJSON to EWKT on assignment."""
        from app.models.zone import Zone

        geojson = {
            "type": "MultiPolygon",
            "coordinates": [[[[
                -73.964424133, 40.875621535
            ], [
                -74.085273743, 40.79093771
            ], [
                -73.906059265, 40.787558505
            ], [
                -73.922538757, 40.852513065
            ], [
                -73.964767456, 40.87432352
            ], [
                -73.964424133, 40.875621535
            ]]]]
        }

        zone = Zone(
            zone_id="test-zone",
            owner_id=1,
            creator_id=1,
            zone_type="geofence",
            name="Test Zone",
            description="desc",
            h3_cells=[],
            geo_fence_polygon=geojson,
            parameters={},
        )

        assert isinstance(zone.geo_fence_polygon, str)
        assert zone.geo_fence_polygon.startswith("SRID=4326;")
        assert "MULTIPOLYGON(((" in zone.geo_fence_polygon


@pytest.mark.asyncio
async def test_h3_conversion_endpoint(test_db, override_get_db):
    """Test H3 conversion API endpoint."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/utils/h3/convert",
            json={
                "latitude": 37.7749,
                "longitude": -122.4194,
                "resolution": 13,
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["latitude"] == 37.7749
        assert data["longitude"] == -122.4194
        assert "h3_cell_id" in data
        assert "resolution" in data
        assert validate_h3_cell(data["h3_cell_id"])


async def _register_and_login(
    client: AsyncClient,
    *,
    email: str,
    zone_id: str,
    first_name: str,
    last_name: str,
) -> tuple[int, str]:
    register_response = await client.post(
        "/owners/register",
        json={
            "email": email,
            "zone_id": zone_id,
            "first_name": first_name,
            "last_name": last_name,
            "account_type": "private",
            "password": "SecurePassword123",
            "registration_code": "FREE",
            "address": "Test Address 1",
        },
    )
    assert register_response.status_code == 201

    login_response = await client.post(
        "/owners/login",
        json={
            "email": email,
            "password": "SecurePassword123",
        },
    )
    assert login_response.status_code == 200
    return login_response.json()["owner_id"], login_response.json()["access_token"]


@pytest.mark.asyncio
async def test_qr_join_uses_inviter_zone_id(test_db, override_get_db):
    """QR join should always inherit inviter zone_id."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, inviter_token = await _register_and_login(
            client,
            email="inviter@example.com",
            zone_id="inviter-zone-id",
            first_name="Invite",
            last_name="Owner",
        )

        generate_response = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {inviter_token}"},
            json={"expires_in_hours": 24},
        )
        assert generate_response.status_code == 200
        token = generate_response.json()["token"]

        join_response = await client.post(
            "/utils/qr/join",
            json={
                "token": token,
                "email": "joined@example.com",
                "first_name": "Joined",
                "last_name": "User",
                "password": "SecurePassword123",
                "address": "Joined Address",
            },
        )
        assert join_response.status_code == 200
        joined_owner = join_response.json()
        assert joined_owner["zone_id"] == "inviter-zone-id"


@pytest.mark.asyncio
async def test_zone_messages_visibility_and_filtering(test_db, override_get_db):
    """Messages should return public + private related to requester."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        owner_1_id, owner_1_token = await _register_and_login(
            client,
            email="owner1@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="One",
        )
        owner_2_id, _ = await _register_and_login(
            client,
            email="owner2@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="Two",
        )
        _, owner_3_token = await _register_and_login(
            client,
            email="owner3@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="Three",
        )
        _, owner_4_token = await _register_and_login(
            client,
            email="owner4@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="Four",
        )

        headers_owner_1 = {"Authorization": f"Bearer {owner_1_token}"}
        headers_owner_3 = {"Authorization": f"Bearer {owner_3_token}"}
        headers_owner_4 = {"Authorization": f"Bearer {owner_4_token}"}

        response = await client.post(
            "/messages/",
            headers=headers_owner_1,
            json={
                "message": "Public from owner 1",
                "type": "SERVICE",
            },
        )
        assert response.status_code == 201

        response = await client.post(
            "/messages/",
            headers=headers_owner_4,
            json={
                "message": "Public from owner 4",
                "type": "SERVICE",
            },
        )
        assert response.status_code == 201

        response = await client.post(
            "/messages/",
            headers=headers_owner_1,
            json={
                "message": "Private 1 -> 2",
                "type": "PRIVATE",
                "receiver_id": owner_2_id,
            },
        )
        assert response.status_code == 201

        response = await client.post(
            "/messages/",
            headers=headers_owner_3,
            json={
                "message": "Private 3 -> 2 (not visible to owner 1)",
                "type": "PRIVATE",
                "receiver_id": owner_2_id,
            },
        )
        assert response.status_code == 201

        response = await client.get(
            f"/messages/?owner_id={owner_1_id}",
            headers=headers_owner_1,
        )
        assert response.status_code == 200
        messages = response.json()
        message_texts = [entry["message"] for entry in messages]

        assert "Public from owner 1" in message_texts
        assert "Public from owner 4" in message_texts
        assert "Private 1 -> 2" in message_texts
        assert "Private 3 -> 2 (not visible to owner 1)" not in message_texts

        response = await client.get(
            f"/messages/?owner_id={owner_1_id}&other_owner_id={owner_2_id}",
            headers=headers_owner_1,
        )
        assert response.status_code == 200
        filtered_message_texts = [entry["message"] for entry in response.json()]
        assert "Public from owner 1" in filtered_message_texts
        assert "Private 1 -> 2" in filtered_message_texts
        assert "Public from owner 4" not in filtered_message_texts


@pytest.mark.asyncio
async def test_get_messages_without_trailing_slash_returns_200(test_db, override_get_db):
    """GET /messages (no slash) must list messages; avoids 405 clash with POST /messages contract."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        owner_id, access_token = await _register_and_login(
            client,
            email="noslash-msg@example.com",
            zone_id="zone-noslash",
            first_name="No",
            last_name="Slash",
        )
        headers = {"Authorization": f"Bearer {access_token}"}
        create_resp = await client.post(
            "/messages/",
            headers=headers,
            json={"message": "Listed without slash", "type": "SERVICE"},
        )
        assert create_resp.status_code == 201

        response = await client.get(
            f"/messages?owner_id={owner_id}&skip=0&limit=100",
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) >= 1
        entry = body[0]
        for key in ("id", "zone_id", "sender_id", "type", "scope", "category", "visibility", "message", "created_at"):
            assert key in entry
        assert any(item["message"] == "Listed without slash" for item in body)


@pytest.mark.asyncio
async def test_post_contract_messages_still_returns_201(test_db, override_get_db):
    """Contract POST /messages must remain available for mobile integrations."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        register_resp = await client.post(
            "/owners/register",
            json={
                "email": "contract-msg@example.com",
                "zone_id": "contract-zone-1",
                "first_name": "Contract",
                "last_name": "User",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Addr",
            },
        )
        assert register_resp.status_code == 201
        zone_id = register_resp.json()["zone_id"]

        login_resp = await client.post(
            "/owners/login",
            json={"email": "contract-msg@example.com", "password": "SecurePassword123"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]

        response = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "zoneId": zone_id,
                "type": "SERVICE",
                "text": "Contract path message",
                "metadata": {},
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["status"] == "success"
        assert payload["data"]["text"] == "Contract path message"


@pytest.mark.asyncio
async def test_post_messages_accepts_public_chat_payload_without_trailing_slash(test_db, override_get_db):
    """POST /messages should accept chat payload and return created row."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, token = await _register_and_login(
            client,
            email="chat-public-noslash@example.com",
            zone_id="chat-zone-public",
            first_name="Chat",
            last_name="Public",
        )

        response = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "message": "Hello world",
                "type": "SERVICE",
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["message"] == "Hello world"
        assert payload["type"] == "SERVICE"
        assert payload["scope"] == "public"
        assert payload["visibility"] == "public"
        assert payload["receiver_id"] is None
        for key in ("id", "zone_id", "sender_id", "created_at"):
            assert key in payload


@pytest.mark.asyncio
async def test_post_messages_accepts_private_chat_payload_without_trailing_slash(test_db, override_get_db):
    """POST /messages should accept private chat payload and return created row."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        receiver_id, _ = await _register_and_login(
            client,
            email="chat-private-receiver@example.com",
            zone_id="chat-zone-private",
            first_name="Chat",
            last_name="Receiver",
        )
        _, sender_token = await _register_and_login(
            client,
            email="chat-private-sender@example.com",
            zone_id="chat-zone-private",
            first_name="Chat",
            last_name="Sender",
        )

        response = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {sender_token}"},
            json={
                "message": "123123123",
                "type": "PRIVATE",
                "receiver_id": receiver_id,
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["message"] == "123123123"
        assert payload["type"] == "PRIVATE"
        assert payload["scope"] == "private"
        assert payload["visibility"] == "private"
        assert payload["receiver_id"] == receiver_id
        for key in ("id", "zone_id", "sender_id", "created_at"):
            assert key in payload


@pytest.mark.asyncio
async def test_post_messages_private_type_without_receiver_rejected(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, token = await _register_and_login(
            client,
            email="missing-recipient@example.com",
            zone_id="chat-zone-private",
            first_name="Chat",
            last_name="Sender",
        )
        response = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "missing receiver", "type": "PRIVATE"},
        )
        assert response.status_code == 422
        payload = response.json()
        assert payload["error_code"] == "MISSING_RECIPIENT_FOR_PRIVATE_TYPE"


@pytest.mark.asyncio
async def test_post_messages_legacy_visibility_is_mapped_with_deprecation_header(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, token = await _register_and_login(
            client,
            email="legacy-visibility@example.com",
            zone_id="chat-zone-legacy",
            first_name="Legacy",
            last_name="Client",
        )
        response = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "legacy payload", "visibility": "public"},
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["type"] == "SERVICE"
        assert payload["scope"] == "public"
        assert response.headers.get("X-API-Deprecated")


@pytest.mark.asyncio
async def test_post_messages_type_alias_is_normalized(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, token = await _register_and_login(
            client,
            email="alias-normalization@example.com",
            zone_id="chat-zone-alias",
            first_name="Alias",
            last_name="Client",
        )
        response = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "alias payload", "type": "NS PANIC"},
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["type"] == "NS_PANIC"
        assert payload["category"] == "Alarm"


def test_websocket_ws_messages_alias_accepts_valid_token(test_db, override_get_db):
    """WebSocket /ws/messages must mirror /ws for older clients (token query, SUBSCRIBE)."""
    with TestClient(app) as client:
        reg = client.post(
            "/owners/register",
            json={
                "email": "ws-alias@example.com",
                "zone_id": "ws-alias-zone",
                "first_name": "WS",
                "last_name": "Alias",
                "account_type": "private",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Addr",
            },
        )
        assert reg.status_code == 201
        login = client.post(
            "/owners/login",
            json={"email": "ws-alias@example.com", "password": "SecurePassword123"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        with client.websocket_connect(f"/ws/messages?token={token}") as ws:
            ws.send_json({"type": "SUBSCRIBE", "zoneIds": ["ws-alias-zone"]})


@pytest.mark.asyncio
async def test_get_zone_returns_all_matching_zone_id_entries(test_db, override_get_db):
    """Fetching /zones/{zone_id} should return all matching zones across owners."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, owner_1_token = await _register_and_login(
            client,
            email="zones-owner1@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="One",
        )
        _, owner_2_token = await _register_and_login(
            client,
            email="zones-owner2@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="Two",
        )
        _, owner_3_token = await _register_and_login(
            client,
            email="zones-owner3@example.com",
            zone_id="other-zone",
            first_name="Owner",
            last_name="Three",
        )

        create_payload = {
            "zone_id": "shared-zone-id-value",
            "zone_type": "warn",
            "name": "Shared Zone",
            "description": "Shared",
            "h3_cells": [],
        }

        response = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {owner_1_token}"},
            json=create_payload,
        )
        assert response.status_code == 201

        response = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {owner_2_token}"},
            json=create_payload,
        )
        assert response.status_code == 201

        response = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {owner_3_token}"},
            json={
                **create_payload,
                "zone_id": "other-zone-id-value",
                "name": "Other Zone",
            },
        )
        assert response.status_code == 201

        response = await client.get(
            "/zones/shared-zone-id-value",
            headers={"Authorization": f"Bearer {owner_1_token}"},
        )
        assert response.status_code == 200
        zones = response.json()
        assert isinstance(zones, list)
        assert len(zones) == 2
        assert all(zone["zone_id"] == "shared-zone-id-value" for zone in zones)


@pytest.mark.asyncio
async def test_list_zones_with_zone_id_query_returns_all_matching_entries(test_db, override_get_db):
    """GET /zones/?zone_id=... should return all matching zones across owners."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, owner_1_token = await _register_and_login(
            client,
            email="query-owner1@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="One",
        )
        _, owner_2_token = await _register_and_login(
            client,
            email="query-owner2@example.com",
            zone_id="shared-zone",
            first_name="Owner",
            last_name="Two",
        )

        payload = {
            "zone_id": "ZN-80BJC1",
            "zone_type": "warn",
            "name": "Operations Zone",
            "description": "Zone from dashboard console.",
            "h3_cells": ["862a1008fffffff"],
        }

        response = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {owner_1_token}"},
            json=payload,
        )
        assert response.status_code == 201

        response = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {owner_2_token}"},
            json=payload,
        )
        assert response.status_code == 201

        response = await client.get(
            "/zones/?zone_id=ZN-80BJC1",
            headers={"Authorization": f"Bearer {owner_1_token}"},
        )
        assert response.status_code == 200
        zones = response.json()
        assert len(zones) == 2
        assert all(zone["zone_id"] == "ZN-80BJC1" for zone in zones)


@pytest.mark.asyncio
async def test_private_admin_and_user_can_view_each_others_zones(test_db, override_get_db):
    """Private admin and linked private user should both see each other's zones."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_register = await client.post(
            "/owners/register",
            json={
                "email": "private-admin@example.com",
                "zone_id": "private-shared-zone",
                "first_name": "Private",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin_register.status_code == 201
        admin_id = admin_register.json()["id"]

        user_register = await client.post(
            "/owners/register",
            json={
                "email": "private-user@example.com",
                "zone_id": "private-shared-zone",
                "first_name": "Private",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user_register.status_code == 201
        user_id = user_register.json()["id"]

        admin_login = await client.post(
            "/owners/login",
            json={"email": "private-admin@example.com", "password": "SecurePassword123"},
        )
        assert admin_login.status_code == 200
        admin_token = admin_login.json()["access_token"]

        user_login = await client.post(
            "/owners/login",
            json={"email": "private-user@example.com", "password": "SecurePassword123"},
        )
        assert user_login.status_code == 200
        user_token = user_login.json()["access_token"]

        admin_zone = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "zone_id": "PRIVATE-ZONE-A",
                "zone_type": "warn",
                "name": "Admin Zone",
                "description": "Admin created",
                "h3_cells": [],
            },
        )
        assert admin_zone.status_code == 201

        user_zone = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {user_token}"},
            json={
                "zone_id": "PRIVATE-ZONE-U",
                "zone_type": "warn",
                "name": "User Zone",
                "description": "User created",
                "h3_cells": [],
            },
        )
        assert user_zone.status_code == 201

        user_reads_admin = await client.get(
            f"/zones/?owner_id={admin_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert user_reads_admin.status_code == 200
        assert any(zone["owner_id"] == admin_id for zone in user_reads_admin.json())

        admin_reads_user = await client.get(
            f"/zones/?owner_id={user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert admin_reads_user.status_code == 200
        assert any(zone["owner_id"] == user_id for zone in admin_reads_user.json())


@pytest.mark.asyncio
async def test_contract_zones_lists_private_account_admin_and_user_zones(test_db, override_get_db):
    """Contract /zones should include linked private admin+user zones for both callers."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_register = await client.post(
            "/owners/register",
            json={
                "email": "contract-private-admin@example.com",
                "zone_id": "contract-private-shared-zone",
                "first_name": "Contract",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin_register.status_code == 201
        admin_id = admin_register.json()["id"]

        user_register = await client.post(
            "/owners/register",
            json={
                "email": "contract-private-user@example.com",
                "zone_id": "contract-private-shared-zone",
                "first_name": "Contract",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user_register.status_code == 201
        user_id = user_register.json()["id"]

        admin_login = await client.post(
            "/owners/login",
            json={"email": "contract-private-admin@example.com", "password": "SecurePassword123"},
        )
        assert admin_login.status_code == 200
        admin_token = admin_login.json()["access_token"]

        user_login = await client.post(
            "/owners/login",
            json={"email": "contract-private-user@example.com", "password": "SecurePassword123"},
        )
        assert user_login.status_code == 200
        user_token = user_login.json()["access_token"]

        admin_create_zone = await client.post(
            "/zones",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "id": "C-PRIVATE-ADMIN",
                "name": "Contract Admin Zone",
                "type": "warn",
                "geometry": {},
                "config": {},
            },
        )
        assert admin_create_zone.status_code == 201

        user_create_zone = await client.post(
            "/zones",
            headers={"Authorization": f"Bearer {user_token}"},
            json={
                "id": "C-PRIVATE-USER",
                "name": "Contract User Zone",
                "type": "warn",
                "geometry": {},
                "config": {},
            },
        )
        assert user_create_zone.status_code == 201

        admin_list = await client.get(
            "/zones",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert admin_list.status_code == 200
        admin_zone_ids = {zone["id"] for zone in admin_list.json()["data"]}
        admin_owner_ids = {zone["owner_id"] for zone in admin_list.json()["data"]}
        assert {"C-PRIVATE-ADMIN", "C-PRIVATE-USER"}.issubset(admin_zone_ids)
        assert {admin_id, user_id}.issubset(admin_owner_ids)

        user_list = await client.get(
            "/zones",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert user_list.status_code == 200
        user_zone_ids = {zone["id"] for zone in user_list.json()["data"]}
        user_owner_ids = {zone["owner_id"] for zone in user_list.json()["data"]}
        assert {"C-PRIVATE-ADMIN", "C-PRIVATE-USER"}.issubset(user_zone_ids)
        assert {admin_id, user_id}.issubset(user_owner_ids)


@pytest.mark.asyncio
async def test_contract_create_zone_accepts_internal_zone_payload_shape(test_db, override_get_db):
    """Contract /zones should accept dashboard/internal style zone payload keys."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, owner_token = await _register_and_login(
            client,
            email="contract-zone-owner@example.com",
            zone_id="shared-zone",
            first_name="Contract",
            last_name="Owner",
        )

        payload = {
            "zone_id": "ZN-76F7LJ",
            "name": "Operations Zone",
            "description": "Zone from dashboard console.",
            "zone_type": "geofence",
            "h3_cells": ["862a10777ffffff"],
            "geo_fence_polygon": {
                "type": "MultiPolygon",
                "coordinates": [[[[
                    -73.91017913818361, 40.836934333793835
                ], [
                    -73.84323120117189, 40.74570425662038
                ], [
                    -73.99635314941408, 40.76884853115124
                ], [
                    -73.91017913818361, 40.836934333793835
                ]]]],
            },
        }

        response = await client.post(
            "/zones",
            headers={"Authorization": f"Bearer {owner_token}"},
            json=payload,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "success"
        assert body["data"]["id"] == "ZN-76F7LJ"
        assert body["data"]["name"] == "Operations Zone"
        assert body["data"]["type"] in {"polygon", "geofence"}

        from app.models import Zone

        created = (
            test_db.query(Zone)
            .filter(Zone.owner_id.isnot(None), Zone.zone_id == "ZN-76F7LJ")
            .first()
        )
        assert created is not None
        assert created.geo_fence_polygon is not None


@pytest.mark.asyncio
async def test_create_device_duplicate_hid_updates_existing(test_db, override_get_db):
    """Creating a device with duplicate hid should update existing record."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, owner_token = await _register_and_login(
            client,
            email="device-owner@example.com",
            zone_id="device-zone",
            first_name="Device",
            last_name="Owner",
        )
        headers = {"Authorization": f"Bearer {owner_token}"}
        payload = {
            "hid": "WEB-RFEQHBAH",
            "name": "John Smith (Web)",
            "latitude": 0.0,
            "longitude": 0.0,
            "address": "Unknown",
        }

        first = await client.post("/devices/", headers=headers, json=payload)
        assert first.status_code == 201

        duplicate_payload = {
            **payload,
            "name": "Updated Name",
            "address": "Updated Address",
            "propagate_enabled": False,
            "update_interval_seconds": 120,
        }
        duplicate = await client.post("/devices/", headers=headers, json=duplicate_payload)
        assert duplicate.status_code == 200
        body = duplicate.json()
        assert body["hid"] == payload["hid"]
        assert body["name"] == "Updated Name"
        assert body["address"] == "Updated Address"
        assert body["propagate_enabled"] is False
        assert body["update_interval_seconds"] == 120


def _http_error_message(body: dict) -> str:
    if isinstance(body.get("detail"), str):
        return body["detail"]
    return str((body.get("error") or {}).get("message", ""))


@pytest.mark.asyncio
async def test_get_utils_registration_code_returns_string(test_db, override_get_db):
    """GET /utils/registration-code should mint a non-empty code (no auth)."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.get("/utils/registration-code")
        assert r.status_code == 200
        data = r.json()
        code = data.get("registration_code") or data.get("registrationCode") or data.get("code")
        assert isinstance(code, str)
        assert len(code) > 0


@pytest.mark.asyncio
async def test_get_owners_registration_code_returns_string(test_db, override_get_db):
    """GET /owners/registration-code should mirror utils behavior."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.get("/owners/registration-code")
        assert r.status_code == 200
        data = r.json()
        code = data.get("registration_code") or data.get("registrationCode") or data.get("code")
        assert isinstance(code, str)
        assert len(code) > 0


@pytest.mark.asyncio
async def test_post_registration_code_issue_exclusive_succeeds(test_db, override_get_db):
    """Exclusive tier is free and may request an HMAC registration code."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/utils/registration-code/issue",
            json={
                "email": "exclusive-free@example.com",
                "pricingTier": "exclusive",
            },
        )
        assert r.status_code == 200
        data = r.json()
        code = data.get("registration_code")
        assert isinstance(code, str)
        assert len(code) > 0
        assert data.get("pricing_tier") == "exclusive"


@pytest.mark.asyncio
async def test_post_registration_code_issue_paid_tier_requires_upgrade(
    test_db, override_get_db
):
    """Paid tiers are blocked until subscription checkout is implemented."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/utils/registration-code/issue",
            json={
                "email": "paid-tier@example.com",
                "pricingTier": "private_plus",
            },
        )
        assert r.status_code == 403
        assert _http_error_message(r.json()) == "You must upgrade your plan"


@pytest.mark.asyncio
async def test_admin_register_without_registration_code_rejected(test_db, override_get_db):
    """Administrator self-registration must include a registration code."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/owners/register",
            json={
                "email": "nocode-admin@example.com",
                "zone_id": "zone-nocode",
                "first_name": "No",
                "last_name": "Code",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "address": "Addr",
            },
        )
        assert r.status_code == 400
        assert "registration code" in _http_error_message(r.json()).lower()


@pytest.mark.asyncio
async def test_admin_register_with_minted_code_succeeds_once(test_db, override_get_db):
    """Echo minted GET code on register; second attempt with same code fails."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        code = (await client.get("/utils/registration-code")).json()["registration_code"]
        first = await client.post(
            "/owners/register",
            json={
                "email": "minted-1@example.com",
                "zone_id": "zone-minted-1",
                "first_name": "Minted",
                "last_name": "One",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": code,
                "address": "Addr",
            },
        )
        assert first.status_code == 201

        second = await client.post(
            "/owners/register",
            json={
                "email": "minted-2@example.com",
                "zone_id": "zone-minted-2",
                "first_name": "Minted",
                "last_name": "Two",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": code,
                "address": "Addr",
            },
        )
        assert second.status_code == 400
        assert "invalid" in _http_error_message(second.json()).lower() or "used" in _http_error_message(
            second.json()
        ).lower()


@pytest.mark.asyncio
async def test_contract_register_admin_requires_registration_code(test_db, override_get_db):
    """POST /register (contract) requires registrationCode for administrator."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/register",
            json={
                "name": "Contract Admin",
                "email": "contract-nocode@example.com",
                "password": "SecurePassword123",
                "accountType": "PRIVATE",
                "registrationType": "ADMINISTRATOR",
                "zoneId": "ZONE-C1",
                "address": "Addr",
            },
        )
        assert r.status_code == 400
        assert "registration code" in _http_error_message(r.json()).lower()


@pytest.mark.asyncio
async def test_contract_register_admin_with_free_succeeds(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/register",
            json={
                "name": "Contract Admin",
                "email": "contract-free@example.com",
                "password": "SecurePassword123",
                "accountType": "PRIVATE",
                "registrationType": "ADMINISTRATOR",
                "registrationCode": "FREE",
                "zoneId": "ZONE-C2",
                "address": "Addr",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body.get("status") == "success"
        assert body["data"]["email"] == "contract-free@example.com"


@pytest.mark.asyncio
async def test_exclusive_account_allows_one_user_rejects_second(test_db, override_get_db):
    """Exclusive accounts: admin can invite exactly 1 user (the 2nd is rejected)."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "exclusive-admin@example.com",
                "zone_id": "exclusive-zone",
                "first_name": "Exclusive",
                "last_name": "Admin",
                "account_type": "exclusive",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        first_user = await client.post(
            "/owners/register",
            json={
                "email": "exclusive-user-1@example.com",
                "zone_id": "exclusive-zone",
                "first_name": "Exclusive",
                "last_name": "UserOne",
                "account_type": "exclusive",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert first_user.status_code == 201, first_user.text

        second_user = await client.post(
            "/owners/register",
            json={
                "email": "exclusive-user-2@example.com",
                "zone_id": "exclusive-zone",
                "first_name": "Exclusive",
                "last_name": "UserTwo",
                "account_type": "exclusive",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert second_user.status_code == 403
        message = _http_error_message(second_user.json()).lower()
        assert "exclusive" in message
        assert "1" in message


@pytest.mark.asyncio
async def test_qr_generate_rejected_for_private_user_role(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "qr-admin@example.com",
                "zone_id": "qr-zone",
                "first_name": "QR",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        user = await client.post(
            "/owners/register",
            json={
                "email": "qr-user@example.com",
                "zone_id": "qr-zone",
                "first_name": "QR",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user.status_code == 201

        user_login = await client.post(
            "/owners/login",
            json={"email": "qr-user@example.com", "password": "SecurePassword123"},
        )
        assert user_login.status_code == 200
        token = user_login.json()["access_token"]

        qr_generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 24},
        )
        assert qr_generate.status_code == 403
        assert "administrator" in _http_error_message(qr_generate.json()).lower()


@pytest.mark.asyncio
async def test_private_account_device_limit_is_one_per_owner(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, token = await _register_and_login(
            client,
            email="device-cap@example.com",
            zone_id="device-cap-zone",
            first_name="Device",
            last_name="Cap",
        )
        headers = {"Authorization": f"Bearer {token}"}
        first = await client.post(
            "/devices/",
            headers=headers,
            json={"hid": "PRIVATE-DEVICE-1", "name": "Phone 1", "address": "Addr"},
        )
        assert first.status_code == 201

        second = await client.post(
            "/devices/",
            headers=headers,
            json={"hid": "PRIVATE-DEVICE-2", "name": "Phone 2", "address": "Addr"},
        )
        assert second.status_code == 403
        assert "at most 1 device" in _http_error_message(second.json()).lower()


@pytest.mark.asyncio
async def test_admin_can_manage_linked_user_device_and_device_shows_owner(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "manage-admin@example.com",
                "zone_id": "manage-zone",
                "first_name": "Manage",
                "last_name": "Admin",
                "account_type": "private_plus",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        user = await client.post(
            "/owners/register",
            json={
                "email": "manage-user@example.com",
                "zone_id": "manage-zone",
                "first_name": "Manage",
                "last_name": "User",
                "account_type": "private_plus",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user.status_code == 201

        admin_login = await client.post(
            "/owners/login",
            json={"email": "manage-admin@example.com", "password": "SecurePassword123"},
        )
        user_login = await client.post(
            "/owners/login",
            json={"email": "manage-user@example.com", "password": "SecurePassword123"},
        )
        assert admin_login.status_code == 200
        assert user_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        user_headers = {"Authorization": f"Bearer {user_login.json()['access_token']}"}

        created = await client.post(
            "/devices/",
            headers=user_headers,
            json={"hid": "MANAGE-USER-DEVICE", "name": "User Phone", "address": "User Address"},
        )
        assert created.status_code == 201
        device_id = created.json()["id"]

        patched = await client.patch(
            f"/devices/{device_id}",
            headers=admin_headers,
            json={"active": False},
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["active"] is False
        assert body["owner"]["email"] == "manage-user@example.com"
        assert body["owner"]["id"] == user.json()["id"]


@pytest.mark.asyncio
async def test_admin_can_deactivate_user_and_block_login(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "toggle-admin@example.com",
                "zone_id": "toggle-zone",
                "first_name": "Toggle",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        user = await client.post(
            "/owners/register",
            json={
                "email": "toggle-user@example.com",
                "zone_id": "toggle-zone",
                "first_name": "Toggle",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user.status_code == 201
        user_id = user.json()["id"]

        admin_login = await client.post(
            "/owners/login",
            json={"email": "toggle-admin@example.com", "password": "SecurePassword123"},
        )
        assert admin_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

        deactivate = await client.patch(
            f"/owners/{user_id}",
            headers=admin_headers,
            json={"active": False},
        )
        assert deactivate.status_code == 200
        assert deactivate.json()["active"] is False

        blocked_login = await client.post(
            "/owners/login",
            json={"email": "toggle-user@example.com", "password": "SecurePassword123"},
        )
        assert blocked_login.status_code == 403


@pytest.mark.asyncio
async def test_user_list_owners_returns_scoped_account_receivers(test_db, override_get_db):
    """User role should see scoped same-account receivers in /owners list."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_register = await client.post(
            "/owners/register",
            json={
                "email": "owners-admin@example.com",
                "zone_id": "owners-zone",
                "first_name": "Owners",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin_register.status_code == 201
        admin_id = admin_register.json()["id"]

        user_a_register = await client.post(
            "/owners/register",
            json={
                "email": "owners-user-a@example.com",
                "zone_id": "owners-zone",
                "first_name": "Owners",
                "last_name": "UserA",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User A Address",
            },
        )
        assert user_a_register.status_code == 201
        user_a_id = user_a_register.json()["id"]

        user_b_register = await client.post(
            "/owners/register",
            json={
                "email": "owners-user-b@example.com",
                "zone_id": "owners-zone",
                "first_name": "Owners",
                "last_name": "UserB",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User B Address",
            },
        )
        assert user_b_register.status_code == 201
        user_b_id = user_b_register.json()["id"]

        user_a_login = await client.post(
            "/owners/login",
            json={"email": "owners-user-a@example.com", "password": "SecurePassword123"},
        )
        assert user_a_login.status_code == 200
        user_a_headers = {"Authorization": f"Bearer {user_a_login.json()['access_token']}"}

        list_response = await client.get("/owners/?skip=0&limit=500", headers=user_a_headers)
        assert list_response.status_code == 200
        listed_ids = {owner["id"] for owner in list_response.json()}
        assert {admin_id, user_a_id, user_b_id}.issubset(listed_ids)
        sample_row = list_response.json()[0]
        assert "first_name" in sample_row
        assert "last_name" in sample_row
        assert "email" in sample_row
        assert "zone_id" in sample_row
        assert "active" in sample_row


@pytest.mark.asyncio
async def test_user_members_contains_account_owner_mapping_for_receivers(test_db, override_get_db):
    """Contract /members should include account_owner_id mapping fields for USER role."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_register = await client.post(
            "/owners/register",
            json={
                "email": "member-map-admin@example.com",
                "zone_id": "member-map-zone",
                "first_name": "Member",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin_register.status_code == 201
        admin_id = admin_register.json()["id"]
        user_register = await client.post(
            "/owners/register",
            json={
                "email": "member-map-user@example.com",
                "zone_id": "member-map-zone",
                "first_name": "Member",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user_register.status_code == 201

        user_login = await client.post(
            "/owners/login",
            json={"email": "member-map-user@example.com", "password": "SecurePassword123"},
        )
        assert user_login.status_code == 200
        headers = {"Authorization": f"Bearer {user_login.json()['access_token']}"}

        members_response = await client.get("/members", headers=headers)
        assert members_response.status_code == 200
        members = members_response.json()["data"]
        assert len(members) >= 2
        row = members[0]
        assert "account_owner_id" in row
        assert "email" in row
        assert "zone_id" in row
        assert "first_name" in row
        assert "last_name" in row


@pytest.mark.asyncio
async def test_user_private_message_scope_and_receiver_validation(test_db, override_get_db):
    """USER private messaging should allow in-scope targets and reject invalid receiver cases."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "pm-admin@example.com",
                "zone_id": "pm-zone",
                "first_name": "PM",
                "last_name": "Admin",
                "account_type": "private_plus",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        sender_user = await client.post(
            "/owners/register",
            json={
                "email": "pm-sender@example.com",
                "zone_id": "pm-zone",
                "first_name": "PM",
                "last_name": "Sender",
                "account_type": "private_plus",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "Sender Address",
            },
        )
        assert sender_user.status_code == 201
        sender_id = sender_user.json()["id"]

        receiver_user = await client.post(
            "/owners/register",
            json={
                "email": "pm-receiver@example.com",
                "zone_id": "pm-zone",
                "first_name": "PM",
                "last_name": "Receiver",
                "account_type": "private_plus",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "Receiver Address",
            },
        )
        assert receiver_user.status_code == 201
        receiver_id = receiver_user.json()["id"]

        other_admin = await client.post(
            "/owners/register",
            json={
                "email": "pm-other-admin@example.com",
                "zone_id": "pm-zone",
                "first_name": "Other",
                "last_name": "Admin",
                "account_type": "private_plus",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Other Admin Address",
            },
        )
        assert other_admin.status_code == 201
        outsider_id = other_admin.json()["id"]

        sender_login = await client.post(
            "/owners/login",
            json={"email": "pm-sender@example.com", "password": "SecurePassword123"},
        )
        assert sender_login.status_code == 200
        sender_headers = {"Authorization": f"Bearer {sender_login.json()['access_token']}"}

        allowed = await client.post(
            "/messages",
            headers=sender_headers,
            json={"message": "allowed", "type": "PRIVATE", "receiver_id": receiver_id},
        )
        assert allowed.status_code == 201

        blocked_scope = await client.post(
            "/messages",
            headers=sender_headers,
            json={"message": "blocked-scope", "type": "PRIVATE", "receiver_id": outsider_id},
        )
        assert blocked_scope.status_code == 403
        assert blocked_scope.json()["error_code"] == "RECEIVER_OUTSIDE_ALLOWED_SCOPE"

        to_self = await client.post(
            "/messages",
            headers=sender_headers,
            json={"message": "self", "type": "PRIVATE", "receiver_id": sender_id},
        )
        assert to_self.status_code == 422
        assert to_self.json()["error_code"] == "INVALID_RECEIVER_SELF"

        admin_login = await client.post(
            "/owners/login",
            json={"email": "pm-admin@example.com", "password": "SecurePassword123"},
        )
        assert admin_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        deactivate = await client.patch(
            f"/owners/{receiver_id}",
            headers=admin_headers,
            json={"active": False},
        )
        assert deactivate.status_code == 200

        inactive = await client.post(
            "/messages",
            headers=sender_headers,
            json={"message": "inactive", "type": "PRIVATE", "receiver_id": receiver_id},
        )
        assert inactive.status_code == 422
        assert inactive.json()["error_code"] == "RECEIVER_INACTIVE"


@pytest.mark.asyncio
async def test_zone_create_returns_consistent_identity_fields(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        owner_id, token = await _register_and_login(
            client,
            email="zone-create-fields@example.com",
            zone_id="zone-create-fields",
            first_name="Zone",
            last_name="Creator",
        )
        response = await client.post(
            "/zones/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "zone_id": "ZONE-CREATE-IDENTITY",
                "zone_type": "warn",
                "name": "Main Zone",
                "description": "Main",
                "h3_cells": [],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert isinstance(body["id"], int)
        assert body["zone_id"] == "ZONE-CREATE-IDENTITY"
        assert body["owner_id"] == owner_id
        assert body["creator_id"] == owner_id


@pytest.mark.asyncio
async def test_zone_patch_by_record_id_updates_user_own_zone(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "zone-admin@example.com",
                "zone_id": "zone-shared",
                "first_name": "Zone",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        user = await client.post(
            "/owners/register",
            json={
                "email": "zone-user@example.com",
                "zone_id": "zone-shared",
                "first_name": "Zone",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user.status_code == 201

        user_login = await client.post(
            "/owners/login",
            json={"email": "zone-user@example.com", "password": "SecurePassword123"},
        )
        assert user_login.status_code == 200
        user_headers = {"Authorization": f"Bearer {user_login.json()['access_token']}"}

        created = await client.post(
            "/zones/",
            headers=user_headers,
            json={
                "zone_id": "USER-ZONE-1",
                "zone_type": "warn",
                "name": "User Zone",
                "description": "Before update",
                "h3_cells": [],
            },
        )
        assert created.status_code == 201
        zone_record_id = created.json()["id"]

        patched = await client.patch(
            f"/zones/{zone_record_id}",
            headers=user_headers,
            json={"name": "User Zone Updated"},
        )
        assert patched.status_code == 200
        assert patched.json()["id"] == zone_record_id
        assert patched.json()["name"] == "User Zone Updated"


@pytest.mark.asyncio
async def test_zone_patch_non_owned_zone_returns_403(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "zone-admin-403@example.com",
                "zone_id": "zone-shared-403",
                "first_name": "Zone",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        user = await client.post(
            "/owners/register",
            json={
                "email": "zone-user-403@example.com",
                "zone_id": "zone-shared-403",
                "first_name": "Zone",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user.status_code == 201

        admin_login = await client.post(
            "/owners/login",
            json={"email": "zone-admin-403@example.com", "password": "SecurePassword123"},
        )
        user_login = await client.post(
            "/owners/login",
            json={"email": "zone-user-403@example.com", "password": "SecurePassword123"},
        )
        assert admin_login.status_code == 200
        assert user_login.status_code == 200

        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        user_headers = {"Authorization": f"Bearer {user_login.json()['access_token']}"}

        admin_zone = await client.post(
            "/zones/",
            headers=admin_headers,
            json={
                "zone_id": "ADMIN-MAIN-ZONE",
                "zone_type": "warn",
                "name": "Admin Main Zone",
                "description": "Admin zone",
                "h3_cells": [],
            },
        )
        assert admin_zone.status_code == 201
        admin_zone_record_id = admin_zone.json()["id"]

        forbidden = await client.patch(
            f"/zones/{admin_zone_record_id}",
            headers=user_headers,
            json={"name": "Not Allowed"},
        )
        assert forbidden.status_code == 403
        assert "forbidden" in _http_error_message(forbidden.json()).lower()


@pytest.mark.asyncio
async def test_zone_limit_exceeded_returns_clear_message(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": "zone-limit-admin@example.com",
                "zone_id": "zone-limit-shared",
                "first_name": "Limit",
                "last_name": "Admin",
                "account_type": "private",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Admin Address",
            },
        )
        assert admin.status_code == 201
        admin_id = admin.json()["id"]

        user = await client.post(
            "/owners/register",
            json={
                "email": "zone-limit-user@example.com",
                "zone_id": "zone-limit-shared",
                "first_name": "Limit",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert user.status_code == 201

        user_login = await client.post(
            "/owners/login",
            json={"email": "zone-limit-user@example.com", "password": "SecurePassword123"},
        )
        assert user_login.status_code == 200
        user_headers = {"Authorization": f"Bearer {user_login.json()['access_token']}"}

        first = await client.post(
            "/zones/",
            headers=user_headers,
            json={
                "zone_id": "USER-LIMIT-ZONE-1",
                "zone_type": "warn",
                "name": "Zone 1",
                "description": "z1",
                "h3_cells": [],
            },
        )
        second = await client.post(
            "/zones/",
            headers=user_headers,
            json={
                "zone_id": "USER-LIMIT-ZONE-2",
                "zone_type": "warn",
                "name": "Zone 2",
                "description": "z2",
                "h3_cells": [],
            },
        )
        third = await client.post(
            "/zones/",
            headers=user_headers,
            json={
                "zone_id": "USER-LIMIT-ZONE-3",
                "zone_type": "warn",
                "name": "Zone 3",
                "description": "z3",
                "h3_cells": [],
            },
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert third.status_code == 403
        assert "zone #2 and zone #3" in _http_error_message(third.json()).lower()
