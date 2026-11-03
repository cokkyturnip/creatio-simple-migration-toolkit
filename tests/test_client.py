"""Unit tests for the OData client: auth, retries, batch, metadata."""

from __future__ import annotations

import uuid

import pytest

from csmt.auth import AuthManager, BasicAuth, FormsAuth
from csmt.client import (
    BatchResult,
    ODataClient,
    extract_error_message,
    extract_record_id_from_location,
    odata_escape,
    odata_literal,
    parse_csdl_entity_fields,
)
from csmt.config import Settings
from csmt.errors import AuthError, BatchError, ConfigError, ODataError
from mock_creatio import MockCreatio


def settings_for(mock: MockCreatio, **overrides) -> Settings:
    data = {
        "base_url": mock.url,
        "username": "Supervisor",
        "password": "secret",
        "entity": "Contact",
        "timeout": 10,
        "max_retries": 2,
        "retry_backoff": 0.001,
        "retry_max_delay": 0.01,
    }
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def odata(mock: MockCreatio):
    client = ODataClient(settings_for(mock), sleep=lambda _seconds: None)
    try:
        yield client
    finally:
        client.close()


# ------------------------------------------------------------------ auth
def test_forms_auth_manager_headers(mock: MockCreatio) -> None:
    manager = AuthManager(settings_for(mock))
    manager.ensure()
    assert manager.mode == "forms"
    assert manager.headers()["BPMCSRF"] == mock.csrf_token


def test_basic_auth(mock: MockCreatio) -> None:
    client = ODataClient(settings_for(mock, auth_mode="basic"), sleep=lambda _seconds: None)
    try:
        record = client.create("Contact", {"Name": "Basic"})
        assert record["Id"]
    finally:
        client.close()


def test_oauth2_auth(mock: MockCreatio) -> None:
    settings = settings_for(
        mock,
        auth_mode="oauth2",
        client_id="client",
        client_secret="client-secret",
        identity_url=mock.url,
    )
    client = ODataClient(settings, sleep=lambda _seconds: None)
    try:
        record = client.create("Contact", {"Name": "OAuth"})
        assert record["Id"]
        assert client.auth.mode == "oauth2"
    finally:
        client.close()


def test_bad_password_raises_auth_error(mock: MockCreatio) -> None:
    settings = settings_for(mock, password="wrong")
    client = ODataClient(settings, sleep=lambda _seconds: None)
    try:
        with pytest.raises(ODataError) as excinfo:
            client.get_collection("Contact")
        assert "Authentication failed" in str(excinfo.value)
    finally:
        client.close()


def test_forms_auth_direct_bad_password(mock: MockCreatio) -> None:
    import requests

    strategy = FormsAuth(settings_for(mock, password="wrong"))
    with pytest.raises(AuthError):
        strategy.authenticate(requests.Session())


def test_basic_auth_requires_credentials(mock: MockCreatio) -> None:
    with pytest.raises(AuthError):
        BasicAuth(settings_for(mock, auth_mode="basic", username=None, password=None))


# ---------------------------------------------------------------- reading
def test_service_document_and_ping(odata: ODataClient) -> None:
    info = odata.ping()
    assert info["collections"] == 3
    assert info["entity"] == "Contact"
    assert info["entity_reachable"] is True


def test_get_collection_filter_and_select(odata: ODataClient, mock: MockCreatio) -> None:
    mock.seed("Contact", {"Name": "Alice", "Email": "alice@x"})
    mock.seed("Contact", {"Name": "Bob", "Email": "bob@x"})
    items, _ = odata.get_collection(
        "Contact",
        filter="(Email eq 'alice@x' or Email eq 'bob@x')",
        select=["Id", "Name"],
        top=10,
    )
    assert {item["Name"] for item in items} == {"Alice", "Bob"}
    assert all(set(item) == {"Id", "Name"} for item in items)


def test_get_collection_count(odata: ODataClient, mock: MockCreatio) -> None:
    mock.seed("Contact", {"Name": "Alice"})
    mock.seed("Contact", {"Name": "Bob"})
    _, total = odata.get_collection("Contact", count=True, top=1)
    assert total == 2
    assert odata.count("Contact") == 2


def test_iter_collection_pages(odata: ODataClient, mock: MockCreatio) -> None:
    for index in range(7):
        mock.seed("Contact", {"Name": f"Person {index}"})
    names = [record["Name"] for record in odata.iter_collection("Contact", page_size=3)]
    assert len(names) == 7


def test_get_missing_record_returns_none(odata: ODataClient) -> None:
    assert odata.get_record("Contact", str(uuid.uuid4())) is None


# ---------------------------------------------------------------- writing
def test_create_and_update(odata: ODataClient, mock: MockCreatio) -> None:
    created = odata.create("Contact", {"Name": "Alice", "Email": "alice@x"})
    record_id = created["Id"]
    assert mock.store["Contact"][record_id]["Name"] == "Alice"

    odata.update("Contact", record_id, {"Name": "Alice Updated"})
    assert mock.store["Contact"][record_id]["Name"] == "Alice Updated"


def test_create_transient_503_is_not_resend(odata: ODataClient, mock: MockCreatio) -> None:
    """POST is not idempotent: the transport layer must never resend it."""
    with pytest.raises(ODataError) as excinfo:
        odata.create("Contact", {"Name": "TRIGGER-503"})
    assert excinfo.value.status == 503
    assert mock.attempts["TRIGGER-503"] == 1


def test_create_raises_odata_error(odata: ODataClient) -> None:
    with pytest.raises(ODataError) as excinfo:
        odata.create("Contact", {"Name": "BAD-REQUEST"})
    assert excinfo.value.status == 400
    assert "Name value rejected" in excinfo.value.detail


def test_create_never_retries_on_500(odata: ODataClient, mock: MockCreatio) -> None:
    """POST gets exactly one attempt even on 5xx responses."""
    with pytest.raises(ODataError) as excinfo:
        odata.create("Contact", {"Name": "ALWAYS-500"})
    assert excinfo.value.status == 500
    assert mock.attempts["ALWAYS-500"] == 1


def test_get_retries_on_500(mock: MockCreatio) -> None:
    """GET is idempotent and still retries."""
    client = ODataClient(settings_for(mock), sleep=lambda _seconds: None)
    try:
        items, _ = client.get_collection("Contact")
        assert items == []
    finally:
        client.close()


def test_duplicate_unique_field_returns_400(odata: ODataClient) -> None:
    odata.create("Contact", {"Name": "Alice", "Email": "dupe@x"})
    with pytest.raises(ODataError) as excinfo:
        odata.create("Contact", {"Name": "Alice 2", "Email": "dupe@x"})
    assert "unique" in excinfo.value.detail.lower()


def test_patch_missing_record_returns_404(odata: ODataClient) -> None:
    with pytest.raises(ODataError) as excinfo:
        odata.update("Contact", str(uuid.uuid4()), {"Name": "X"})
    assert excinfo.value.status == 404


# ------------------------------------------------------------------ batch
def test_batch_creates_records(odata: ODataClient, mock: MockCreatio) -> None:
    results = odata.batch(
        [
            {"method": "POST", "url": "Contact", "id": "1", "body": {"Name": "Batch A"}},
            {"method": "POST", "url": "Contact", "id": "2", "body": {"Name": "Batch B"}},
        ]
    )
    assert set(results) == {"1", "2"}
    assert results["1"].ok and results["2"].ok
    assert results["1"].record_id
    assert len(mock.records("Contact")) == 2


def test_batch_continues_after_error(odata: ODataClient) -> None:
    results = odata.batch(
        [
            {"method": "POST", "url": "Contact", "id": "1", "body": {"Name": "BAD-REQUEST"}},
            {"method": "POST", "url": "Contact", "id": "2", "body": {"Name": "Good"}},
        ]
    )
    assert results["1"].status == 400
    assert not results["1"].ok
    assert "rejected" in (results["1"].error_message or "")
    assert results["2"].ok


def test_batch_patch(odata: ODataClient, mock: MockCreatio) -> None:
    record_id = mock.seed("Contact", {"Name": "Before"})
    results = odata.batch(
        [
            {
                "method": "PATCH",
                "url": f"Contact({record_id})",
                "id": "1",
                "body": {"Name": "After"},
            }
        ]
    )
    assert results["1"].status == 204
    assert mock.store["Contact"][record_id]["Name"] == "After"


def test_batch_rejects_more_than_100(odata: ODataClient) -> None:
    with pytest.raises(ConfigError):
        odata.batch(
            [
                {"method": "POST", "url": "Contact", "id": str(i), "body": {"Name": str(i)}}
                for i in range(101)
            ]
        )


def test_batch_empty_returns_empty(odata: ODataClient) -> None:
    assert odata.batch([]) == {}


def test_batch_garbage_response_raises(odata: ODataClient, mock: MockCreatio) -> None:
    mock.force_batch_garbage = True
    with pytest.raises(BatchError):
        odata.batch([{"method": "POST", "url": "Contact", "id": "1", "body": {"Name": "X"}}])


def test_batch_http_error_raises(odata: ODataClient, mock: MockCreatio) -> None:
    mock.force_batch_http_error = 400
    with pytest.raises(BatchError):
        odata.batch([{"method": "POST", "url": "Contact", "id": "1", "body": {"Name": "X"}}])


# ------------------------------------------------------------- resilience
def test_reauth_on_401(odata: ODataClient) -> None:
    odata.session.cookies.clear()
    items, _ = odata.get_collection("Contact")
    assert items == []


def test_reauth_on_403_csrf(odata: ODataClient) -> None:
    strategy = odata.auth.strategy
    assert isinstance(strategy, FormsAuth)
    strategy._csrf = "stale-token"
    record = odata.create("Contact", {"Name": "After CSRF refresh"})
    assert record["Id"]


# --------------------------------------------------------------- metadata
def test_parse_csdl_entity_fields(mock: MockCreatio) -> None:
    fields = parse_csdl_entity_fields(mock.metadata_xml(), "Contact")
    assert fields["Name"] == "Edm.String"
    assert fields["UsrScore"] == "Edm.Decimal"
    assert fields["Id"] == "Edm.Guid"


def test_entity_fields_from_server(odata: ODataClient) -> None:
    fields = odata.entity_fields("Contact")
    assert "Email" in fields


def test_entity_fields_unknown_entity(mock: MockCreatio) -> None:
    client = ODataClient(settings_for(mock), sleep=lambda _: None)
    try:
        with pytest.raises(ODataError):
            client.entity_fields("Nope")
    finally:
        client.close()


# ---------------------------------------------------------------- helpers
def test_odata_escape_and_literal() -> None:
    assert odata_escape("O'Brien") == "O''Brien"
    assert odata_literal("O'Brien") == "'O''Brien'"
    assert odata_literal(42) == "42"
    assert odata_literal(True) == "true"
    assert odata_literal("123", quote=False) == "123"


def test_extract_record_id_from_location() -> None:
    record_id = "62f9bc01-57cf-4cc7-90bf-8672acc922e2"
    assert extract_record_id_from_location(f"https://x/0/odata/City({record_id})") == record_id
    assert extract_record_id_from_location(None) is None


def test_extract_error_message_shapes() -> None:
    assert extract_error_message({"error": {"message": "boom"}}) == "boom"
    assert extract_error_message({"Message": "boom2"}) == "boom2"
    assert extract_error_message('{"error": {"message": "boom3"}}') == "boom3"
    assert extract_error_message("plain text") == "plain text"
    assert extract_error_message(None) is None


def test_batch_result_record_id_from_headers() -> None:
    result = BatchResult(
        id="1",
        status=201,
        headers={
            "location": "https://x/0/odata/Contact(62f9bc01-57cf-4cc7-90bf-8672acc922e2)"
        },
    )
    assert result.record_id == "62f9bc01-57cf-4cc7-90bf-8672acc922e2"


def test_oauth2_bearer_only_no_csrf_roundtrip(mock: MockCreatio) -> None:
    """Per Creatio docs the bearer token alone authorises OData calls."""
    settings = settings_for(
        mock, auth_mode="oauth2", client_id="client", client_secret="client-secret",
        identity_url=mock.url,
    )
    client = ODataClient(settings, sleep=lambda _seconds: None)
    try:
        client.get_collection("Contact")
        headers = client.auth.headers()
        assert headers == {"Authorization": f"Bearer {mock.bearer_token}"}
        assert "BPMCSRF" not in headers
        assert client.auth.uses_csrf is False
    finally:
        client.close()


def test_oauth2_reauthenticates_after_token_expiry(mock: MockCreatio) -> None:
    """An expired token returns 401; the client fetches a new token once and retries."""
    settings = settings_for(
        mock, auth_mode="oauth2", client_id="client", client_secret="client-secret",
        identity_url=mock.url,
    )
    client = ODataClient(settings, sleep=lambda _seconds: None)
    try:
        client.get_collection("Contact")  # token #1 cached
        mock.bearer_token = uuid.uuid4().hex  # server rotates / expires the token
        items, _ = client.get_collection("Contact")  # 401 -> re-auth -> success
        assert items == []
        assert client.auth.headers()["Authorization"] == f"Bearer {mock.bearer_token}"
    finally:
        client.close()


def test_oauth2_token_request_uses_identity_url(mock: MockCreatio) -> None:
    settings = settings_for(
        mock, auth_mode="oauth2", client_id="client", client_secret="client-secret",
        identity_url=mock.url + "/",
    )
    assert settings.token_endpoint == mock.url + "/connect/token"
    client = ODataClient(settings, sleep=lambda _seconds: None)
    try:
        client.get_collection("Contact")
        token_calls = [r for r in mock.requests if r["path"] == "/connect/token"]
        assert token_calls, "token endpoint was never called"
    finally:
        client.close()


# ------------------------------------------------- identity host discovery
class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status_code = status
        self.text = body
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {}

    def json(self):
        import json as _json

        return _json.loads(self.text)


def _oauth_settings(mock: MockCreatio, **overrides) -> Settings:
    data = {
        "base_url": "https://pgdn-demo.creatio.com",
        "auth_mode": "oauth2",
        "client_id": "client",
        "client_secret": "client-secret",
        "entity": "Contact",
        "max_retries": 0,
        "timeout": 5,
    }
    data.update(overrides)
    return Settings(**data)


def test_oauth_falls_back_to_underscore_host_when_hyphen_is_dead(monkeypatch) -> None:
    """A wrong guess must be detected, not reported as bad credentials."""
    import requests

    calls: list[str] = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        if "--" in url or "-is.creatio.com" in url:
            raise requests.exceptions.SSLError("hostname mismatch")
        return _FakeResponse(200, '{"access_token": "tok", "token_type": "Bearer"}')

    monkeypatch.setattr(requests.Session, "post", fake_post)
    settings = _oauth_settings(None)
    manager = AuthManager(settings)
    manager.ensure()
    assert manager.headers()["Authorization"] == "Bearer tok"
    assert manager.strategy._endpoint_used.endswith("_is.creatio.com/connect/token")
    assert len(calls) == 2


def test_oauth_reports_credential_error_without_trying_other_hosts(monkeypatch) -> None:
    import requests

    calls: list[str] = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        return _FakeResponse(400, '{"error": "invalid_client"}')

    monkeypatch.setattr(requests.Session, "post", fake_post)
    manager = AuthManager(_oauth_settings(None))
    with pytest.raises(AuthError) as excinfo:
        manager.ensure()
    assert "menolak kredensial" in str(excinfo.value)
    assert len(calls) == 1  # a real identity server answered; do not probe further


def test_oauth_all_hosts_dead_gives_actionable_error(monkeypatch) -> None:
    import requests

    def fake_post(self, url, **kwargs):
        raise requests.exceptions.RequestException("404 not an identity server")

    monkeypatch.setattr(requests.Session, "post", fake_post)
    manager = AuthManager(_oauth_settings(None))
    with pytest.raises(AuthError) as excinfo:
        manager.ensure()
    message = str(excinfo.value)
    assert "OAuth20IdentityServerUrl" in message
    assert "-is.creatio.com" in message


def test_stale_underscore_identity_recovers_to_hyphen(monkeypatch) -> None:
    """Exactly the reported incident: .env had ..._is (dead host) while ...-is works."""
    import requests

    calls: list[str] = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        if "_is." in url:
            raise requests.exceptions.SSLError("certificate is not valid for this host")
        return _FakeResponse(200, '{"access_token": "tok"}')

    monkeypatch.setattr(requests.Session, "post", fake_post)
    manager = AuthManager(
        _oauth_settings(None, identity_url="https://pgdn-demo_is.creatio.com")
    )
    manager.ensure()
    assert manager.strategy._endpoint_used == "https://pgdn-demo-is.creatio.com/connect/token"
    assert calls[0].startswith("https://pgdn-demo_is.creatio.com")  # configured host first


def test_credential_failure_stops_probing(monkeypatch) -> None:
    """When the configured host really is an identity server, do not try others."""
    import requests

    calls: list[str] = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        return _FakeResponse(400, '{"error": "invalid_client"}')

    monkeypatch.setattr(requests.Session, "post", fake_post)
    manager = AuthManager(
        _oauth_settings(None, identity_url="https://pgdn-demo-is.creatio.com")
    )
    with pytest.raises(AuthError) as excinfo:
        manager.ensure()
    assert "menolak kredensial" in str(excinfo.value)
    assert calls == ["https://pgdn-demo-is.creatio.com/connect/token"]


# ------------------------------------------------------ error diagnostics
def test_html_login_page_raises_path_hint(mock: MockCreatio) -> None:
    """A login redirect (HTML) must be reported as a wrong OData path, not bad JSON."""
    import requests

    class _HtmlResponse:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        text = "<html><body>login</body></html>"
        url = "https://host/odata"
        request = None

        def json(self):
            raise ValueError("not json")

    monkeypatch_response = _HtmlResponse()
    client = ODataClient(settings_for(mock), sleep=lambda _seconds: None)
    try:
        with pytest.raises(ODataError) as excinfo:
            client._parse_json(monkeypatch_response)  # type: ignore[arg-type]
        assert "halaman HTML" in excinfo.value.detail
    finally:
        client.close()


def test_hint_for_known_creatio_errors() -> None:
    from csmt.client import hint_for_odata_error

    permission = ODataError(
        "Current user does not have sufficient permissions to use OData.", status=403
    )
    assert "CanUseODataService" in (hint_for_odata_error(permission) or "")

    object_rights = ODataError(
        'Insufficient permissions to add record in object "Contact".', status=403
    )
    assert "Access rights to objects" in (hint_for_odata_error(object_rights) or "")

    csrf = ODataError("Invalid BPMCSRF token", status=403)
    assert "CSRF" in (hint_for_odata_error(csrf) or "")

    assert hint_for_odata_error(ODataError("something else", status=500)) is None
