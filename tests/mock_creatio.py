"""An in-memory Creatio OData 4 mock used by the test suite.

It implements just enough of the real service to exercise the client and
pipeline end to end:

* ``AuthService.svc/Login`` forms authentication with ``.ASPXAUTH`` and
  ``BPMCSRF`` cookies
* OAuth 2.0 client-credentials token endpoint
* service document, ``$metadata``, ``$filter``/``$select``/``$top``/``$count``
* ``POST``, ``PATCH``, ``GET`` on entity sets, with required fields and unique
  constraints
* JSON ``$batch`` with per-request results and ``Prefer: continue-on-error``
* failure injection through magic ``Name`` values (503 then success, always
  500, 400) so retry and reconciliation paths can be tested
"""

from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


@dataclass
class EntityDef:
    name: str
    fields: dict[str, str]
    required: tuple[str, ...] = ("Name",)
    unique: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)


DEFAULT_ENTITIES = {
    "Contact": EntityDef(
        name="Contact",
        fields={
            "Id": "Edm.Guid",
            "Name": "Edm.String",
            "Email": "Edm.String",
            "BirthDate": "Edm.DateTime",
            "CountryId": "Edm.Guid",
            "UsrScore": "Edm.Decimal",
            "Active": "Edm.Boolean",
            "ProcessListeners": "Edm.Int32",
        },
        required=("Name",),
        unique=("Email",),
        defaults={"ProcessListeners": 0, "Active": True},
    ),
    "Country": EntityDef(
        name="Country",
        fields={"Id": "Edm.Guid", "Name": "Edm.String"},
        required=("Name",),
        unique=("Name",),
    ),
    "Gender": EntityDef(
        name="Gender",
        fields={"Id": "Edm.Guid", "Name": "Edm.String"},
        required=("Name",),
        unique=("Name",),
    ),
}


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip().casefold()


def split_top_level(text: str, separator: str) -> list[str]:
    """Split on ``separator`` ignoring parentheses and quoted strings."""
    parts: list[str] = []
    depth = 0
    in_quotes = False
    buffer = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_quotes and index + 1 < len(text) and text[index + 1] == "'":
                buffer += "''"
                index += 2
                continue
            in_quotes = not in_quotes
            buffer += char
        elif not in_quotes and char == "(":
            depth += 1
            buffer += char
        elif not in_quotes and char == ")":
            depth = max(0, depth - 1)
            buffer += char
        elif not in_quotes and depth == 0 and text.startswith(separator, index):
            parts.append(buffer)
            buffer = ""
            index += len(separator)
            continue
        else:
            buffer += char
        index += 1
    if buffer:
        parts.append(buffer)
    return [part.strip() for part in parts if part.strip()]


def strip_outer_parens(text: str) -> str:
    """Remove parentheses that wrap the entire expression."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        in_quotes = False
        balanced = True
        index = 0
        while index < len(text):
            char = text[index]
            if char == "'":
                if in_quotes and index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                in_quotes = not in_quotes
            elif not in_quotes:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0 and index != len(text) - 1:
                        balanced = False
                        break
            index += 1
        if not balanced or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def parse_literal(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _split_odata_path(path: str) -> str | None:
    """Normalise both /0/odata and /odata prefixes so the mock serves both runtimes."""
    for prefix in ("/0/odata", "/odata"):
        if path == prefix:
            return ""
        if path.startswith(prefix + "/"):
            return path[len(prefix) + 1 :]
        if path.rstrip("/") == prefix.rstrip("/"):
            return ""
    return None


class MockCreatio:
    """Threaded HTTP server exposing a small OData 4 surface."""

    def __init__(
        self,
        *,
        username: str = "Supervisor",
        password: str = "secret",
        client_id: str = "client",
        client_secret: str = "client-secret",
        entities: dict[str, EntityDef] | None = None,
        require_csrf: bool = True,
    ) -> None:
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.entities = dict(entities or DEFAULT_ENTITIES)
        self.require_csrf = require_csrf
        self.store: dict[str, dict[str, dict[str, Any]]] = {
            name: {} for name in self.entities
        }
        self.session_token = uuid.uuid4().hex
        self.csrf_token = uuid.uuid4().hex
        self.bearer_token = uuid.uuid4().hex
        self.requests: list[dict[str, Any]] = []
        self.attempts: dict[str, int] = {}
        self.force_batch_garbage = False
        self.force_batch_http_error: int | None = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockCreatio":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> "MockCreatio":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # ------------------------------------------------------------- utilities
    def seed(self, entity: str, record: dict[str, Any]) -> str:
        record = dict(record)
        record.setdefault("Id", str(uuid.uuid4()))
        self.store[entity][record["Id"]] = record
        return str(record["Id"])

    def records(self, entity: str) -> list[dict[str, Any]]:
        return list(self.store[entity].values())

    def find(self, entity: str, **criteria: Any) -> list[dict[str, Any]]:
        return [
            record
            for record in self.store[entity].values()
            if all(_norm(record.get(key)) == _norm(value) for key, value in criteria.items())
        ]

    def metadata_xml(self) -> str:
        types = []
        sets = []
        for name, definition in self.entities.items():
            properties = "".join(
                f'<Property Name="{field_name}" Type="{field_type}" />'
                for field_name, field_type in definition.fields.items()
            )
            types.append(
                f'<EntityType Name="{name}"><Key><PropertyRef Name="Id" /></Key>{properties}</EntityType>'
            )
            sets.append(f'<EntitySet Name="{name}" EntityType="Default.{name}" />')
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
            "<edmx:DataServices>"
            '<Schema Namespace="Default" xmlns="http://docs.oasis-open.org/odata/ns/edm">'
            + "".join(types)
            + '<EntityContainer Name="Container">'
            + "".join(sets)
            + "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
        )

    # ------------------------------------------------------------------ core
    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "MockCreatio/1.0"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            # -------------------------------------------------- plumbing
            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send_json(
                self, status: int, payload: Any, headers: dict[str, str] | None = None
            ) -> None:
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; odata.metadata=minimal")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _send_text(
                self, status: int, text: str, content_type: str = "text/plain"
            ) -> None:
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _cookies(self) -> dict[str, str]:
                cookie = SimpleCookie()
                raw = self.headers.get("Cookie")
                if raw:
                    try:
                        cookie.load(raw)
                    except Exception:
                        return {}
                return {key: morsel.value for key, morsel in cookie.items()}

            def _record_request(self, method: str) -> None:
                mock.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                    }
                )

            # -------------------------------------------------- auth gate
            def _authorised(self) -> tuple[bool, int]:
                auth = self.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    return (auth.split(" ", 1)[1].strip() == mock.bearer_token, 401)
                if auth.lower().startswith("basic "):
                    try:
                        decoded = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
                        user, _, password = decoded.partition(":")
                    except Exception:
                        return (False, 401)
                    return (user == mock.username and password == mock.password, 401)

                cookies = self._cookies()
                if cookies.get(".ASPXAUTH") != mock.session_token:
                    return (False, 401)

                if mock.require_csrf:
                    header = self.headers.get("BPMCSRF") or ""
                    if not header or header != cookies.get("BPMCSRF"):
                        return (False, 403)
                return (True, 200)

            # -------------------------------------------------- GET / POST
            def do_GET(self) -> None:  # noqa: N802
                self._record_request("GET")
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query, keep_blank_values=True)

                if path.rstrip("/").endswith("/ServiceModel/AuthService.svc/Login"):
                    self._send_json(405, {"error": {"message": "use POST"}})
                    return

                ok, status = self._authorised() if self._is_odata(path) else (True, 200)
                if not ok:
                    self._send_json(
                        status,
                        {
                            "error": {
                                "code": str(status),
                                "message": "Session or CSRF token is invalid",
                            }
                        },
                    )
                    return

                odata_rest = _split_odata_path(unquote(path))
                if odata_rest == "":
                    self._send_json(
                        200,
                        {
                            "@odata.context": f"{mock.url}/0/odata/$metadata",
                            "value": [
                                {"name": name, "kind": "EntitySet", "url": name}
                                for name in mock.entities
                            ],
                        },
                    )
                    return

                if odata_rest == "$metadata":
                    self._send_text(
                        200, mock.metadata_xml(), "application/xml; charset=utf-8"
                    )
                    return

                if odata_rest:
                    self._handle_get_entity(odata_rest, query)
                    return

                self._send_json(404, {"error": {"message": f"Not found: {path}"}})

            def _is_odata(self, path: str) -> bool:
                return _split_odata_path(path) is not None

            def _handle_get_entity(
                self, remainder: str, query: dict[str, list[str]]
            ) -> None:
                match = re.match(r"^([A-Za-z_][\w]*)(?:\(([^)]+)\))?$", remainder)
                if not match:
                    self._send_json(404, {"error": {"message": f"Unknown path {remainder}"}})
                    return
                entity, record_id = match.group(1), match.group(2)
                if entity not in mock.entities:
                    self._send_json(404, {"error": {"message": f"Unknown entity {entity}"}})
                    return

                if record_id:
                    record = mock.store[entity].get(record_id)
                    if record is None:
                        self._send_json(404, {"error": {"message": "Record not found"}})
                        return
                    self._send_json(200, record)
                    return

                records = list(mock.store[entity].values())
                filter_text = (query.get("$filter") or [None])[0]
                if filter_text:
                    records = [
                        record for record in records if mock.match_filter(record, filter_text)
                    ]

                orderby = (query.get("$orderby") or [None])[0]
                if orderby:
                    parts = orderby.split()
                    field_name = parts[0]
                    reverse = len(parts) > 1 and parts[1].lower() == "desc"
                    records.sort(
                        key=lambda record: str(record.get(field_name, "")), reverse=reverse
                    )

                total = len(records)
                skip = int((query.get("$skip") or ["0"])[0] or 0)
                top_raw = (query.get("$top") or [None])[0]
                top = int(top_raw) if top_raw not in (None, "") else None
                records = records[skip : skip + top if top is not None else None]

                select = (query.get("$select") or [None])[0]
                if select:
                    fields = [part.strip() for part in select.split(",") if part.strip()]
                    records = [
                        {
                            field_name: record.get(field_name)
                            for field_name in fields
                            if field_name in record
                        }
                        for record in records
                    ]

                payload: dict[str, Any] = {
                    "@odata.context": f"{mock.url}/0/odata/$metadata#{entity}",
                    "value": records,
                }
                if (query.get("$count") or ["false"])[0].lower() == "true":
                    payload["@odata.count"] = total
                self._send_json(200, payload)

            def do_POST(self) -> None:  # noqa: N802
                self._record_request("POST")
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                body = self._read_body()

                if path.rstrip("/").endswith("/ServiceModel/AuthService.svc/Login"):
                    self._handle_login(body)
                    return

                if path.rstrip("/") == "/connect/token":
                    self._handle_token(body)
                    return

                ok, status = self._authorised()
                if not ok:
                    self._send_json(
                        status,
                        {
                            "error": {
                                "code": str(status),
                                "message": "Session or CSRF token is invalid",
                            }
                        },
                    )
                    return

                if _split_odata_path(path.rstrip("/")) == "$batch":
                    self._handle_batch(body)
                    return

                odata_rest = _split_odata_path(path)
                if odata_rest is not None and odata_rest.strip("/") and odata_rest != "$metadata":
                    entity = odata_rest.strip("/")
                    status, payload, headers = mock.apply_create(entity, body)
                    self._send_json(status, payload, headers)
                    return

                self._send_json(404, {"error": {"message": f"Not found: {path}"}})

            def do_PATCH(self) -> None:  # noqa: N802
                self._record_request("PATCH")
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                body = self._read_body()
                ok, status = self._authorised()
                if not ok:
                    self._send_json(
                        status,
                        {
                            "error": {
                                "code": str(status),
                                "message": "Session or CSRF token is invalid",
                            }
                        },
                    )
                    return

                match = re.match(r"^(/0)?/odata/([A-Za-z_][\w]*)\(([^)]+)\)$", path)
                if not match:
                    self._send_json(404, {"error": {"message": f"Not found: {path}"}})
                    return
                entity, record_id = match.group(2), match.group(3)
                result_status, payload, headers = mock.apply_patch(entity, record_id, body)
                if result_status == 204:
                    self.send_response(204)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send_json(result_status, payload, headers)

            # ---------------------------------------------------- handlers
            def _handle_login(self, body: bytes) -> None:
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                if (
                    data.get("UserName") == mock.username
                    and data.get("UserPassword") == mock.password
                ):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header(
                        "Set-Cookie", f".ASPXAUTH={mock.session_token}; Path=/; HttpOnly"
                    )
                    self.send_header("Set-Cookie", f"BPMCSRF={mock.csrf_token}; Path=/")
                    self.send_header("Set-Cookie", "BPMLOADER=1; Path=/; HttpOnly")
                    payload = json.dumps(
                        {"Code": 0, "Message": "", "Exception": None}
                    ).encode()
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._send_json(200, {"Code": 1, "Message": "Invalid credentials"})

            def _handle_token(self, body: bytes) -> None:
                fields = parse_qs((body or b"").decode("utf-8", errors="replace"))
                client_id = (fields.get("client_id") or [""])[0]
                client_secret = (fields.get("client_secret") or [""])[0]
                grant = (fields.get("grant_type") or [""])[0]
                if (
                    client_id != mock.client_id
                    or client_secret != mock.client_secret
                    or grant != "client_credentials"
                ):
                    self._send_json(
                        400,
                        {"error": "invalid_client", "error_description": "Bad credentials"},
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"BPMCSRF={mock.csrf_token}; Path=/")
                payload = json.dumps(
                    {
                        "access_token": mock.bearer_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                ).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle_batch(self, body: bytes) -> None:
                try:
                    document = json.loads(body or b"{}")
                except ValueError:
                    self._send_json(400, {"error": {"message": "Batch body is not JSON"}})
                    return

                if mock.force_batch_http_error:
                    status = mock.force_batch_http_error
                    mock.force_batch_http_error = None
                    self._send_json(status, {"error": {"message": "Injected batch failure"}})
                    return

                operations = document.get("requests") if isinstance(document, dict) else None
                if not isinstance(operations, list):
                    self._send_json(400, {"error": {"message": "Batch body needs 'requests'"}})
                    return

                responses = []
                for operation in operations:
                    method = str(operation.get("method", "")).upper()
                    url = str(operation.get("url", ""))
                    request_id = str(operation.get("id", len(responses) + 1))
                    raw_body = operation.get("body")
                    body_bytes = json.dumps(raw_body).encode() if raw_body is not None else b""
                    if method == "POST":
                        status, payload, headers = mock.apply_create(url.strip("/"), body_bytes)
                    elif method == "PATCH":
                        match = re.match(r"^([A-Za-z_][\w]*)\(([^)]+)\)$", url)
                        if match:
                            status, payload, headers = mock.apply_patch(
                                match.group(1), match.group(2), body_bytes
                            )
                        else:
                            status, payload, headers = 400, {"error": {"message": "Bad url"}}, {}
                    else:
                        status, payload, headers = 405, {"error": {"message": "Unsupported"}}, {}

                    entry: dict[str, Any] = {
                        "id": request_id,
                        "status": status,
                        "headers": headers,
                    }
                    if status != 204 and payload is not None:
                        entry["body"] = payload
                    responses.append(entry)

                if mock.force_batch_garbage:
                    mock.force_batch_garbage = False
                    self._send_text(200, "this-is-not-json", "text/plain")
                    return

                self._send_json(200, {"responses": responses})

            # silence broken pipe noise from tests
            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    # -------------------------------------------------------------- matching
    def match_filter(self, record: dict[str, Any], filter_text: str) -> bool:
        """Evaluate the subset of OData filter syntax the importer generates."""
        text = strip_outer_parens(filter_text)

        or_parts = split_top_level(text, " or ")
        if len(or_parts) > 1:
            return any(self.match_filter(record, part) for part in or_parts)

        and_parts = split_top_level(text, " and ")
        if len(and_parts) > 1:
            return all(self.match_filter(record, part) for part in and_parts)

        match = re.match(
            r"^([\w.]+)\s+(eq|ne|gt|ge|lt|le)\s+(.+)$", text.strip(), re.IGNORECASE
        )
        if not match:
            return False
        field_name, operator, raw_literal = (
            match.group(1),
            match.group(2).lower(),
            match.group(3),
        )
        expected = parse_literal(raw_literal)
        actual = record.get(field_name)

        if isinstance(actual, bool) or isinstance(expected, bool):
            equal = bool(actual) == bool(expected)
            return equal if operator == "eq" else (not equal if operator == "ne" else False)

        # Compare numerically when both sides are numeric, otherwise as text.
        try:
            left: Any = float(str(actual))
            right: Any = float(str(expected))
        except (TypeError, ValueError):
            left, right = _norm(actual), _norm(expected)

        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if not isinstance(left, float):
            return False
        if operator == "gt":
            return left > right
        if operator == "ge":
            return left >= right
        if operator == "lt":
            return left < right
        if operator == "le":
            return left <= right
        return False

    # ------------------------------------------------------------ mutations
    def apply_create(
        self, entity: str, body: bytes
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if entity not in self.entities:
            return 404, {"error": {"message": f"Unknown entity {entity}"}}, {}
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": {"message": "Body is not JSON"}}, {}
        if not isinstance(payload, dict):
            return 400, {"error": {"message": "Body must be an object"}}, {}

        value_name = str(payload.get("Name") or "")
        with self._lock:
            attempt = self.attempts.get(value_name, 0) + 1
            self.attempts[value_name] = attempt

        if value_name == "ALWAYS-500":
            return 500, {"error": {"message": "Injected server error"}}, {}
        if value_name == "TRIGGER-503" and attempt == 1:
            return 503, {"error": {"message": "Injected transient error"}}, {}
        if value_name == "BAD-REQUEST":
            return (
                400,
                {"error": {"code": "400", "message": "Name value rejected by validation"}},
                {},
            )

        definition = self.entities[entity]
        for required in definition.required:
            if payload.get(required) in (None, ""):
                return 400, {"error": {"message": f"Field {required} is required"}}, {}

        for unique_field in definition.unique:
            value = payload.get(unique_field)
            if value in (None, ""):
                continue
            if any(
                record.get(unique_field) is not None
                and _norm(record.get(unique_field)) == _norm(value)
                for record in self.store[entity].values()
            ):
                return (
                    400,
                    {"error": {"message": f"{entity}.{unique_field} must be unique"}},
                    {},
                )

        record_id = str(payload.get("Id") or uuid.uuid4())
        if record_id in self.store[entity]:
            return 400, {"error": {"message": "Record already exists"}}, {}

        record = dict(definition.defaults)
        record.update(
            {key: value for key, value in payload.items() if key in definition.fields}
        )
        record["Id"] = record_id
        self.store[entity][record_id] = record

        location = f"{self.url}/0/odata/{entity}({record_id})"  # mirrors Creatio response shape
        return 201, record, {"Location": location}

    def apply_patch(
        self, entity: str, record_id: str, body: bytes
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if entity not in self.entities:
            return 404, {"error": {"message": f"Unknown entity {entity}"}}, {}
        record = self.store[entity].get(record_id)
        if record is None:
            return 404, {"error": {"message": "Record not found"}}, {}
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": {"message": "Body is not JSON"}}, {}
        if not isinstance(payload, dict):
            return 400, {"error": {"message": "Body must be an object"}}, {}

        definition = self.entities[entity]
        for unique_field in definition.unique:
            value = payload.get(unique_field)
            if value in (None, ""):
                continue
            for other_id, other in self.store[entity].items():
                if other_id == record_id:
                    continue
                if _norm(other.get(unique_field)) == _norm(value):
                    return (
                        400,
                        {"error": {"message": f"{entity}.{unique_field} must be unique"}},
                        {},
                    )

        record.update(
            {key: value for key, value in payload.items() if key in definition.fields}
        )
        return 204, {}, {}


__all__ = [
    "DEFAULT_ENTITIES",
    "EntityDef",
    "MockCreatio",
    "parse_literal",
    "split_top_level",
    "strip_outer_parens",
]
