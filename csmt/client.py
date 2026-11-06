"""Thin, resilient OData 4 client for Creatio.

Only the operations an importer needs are implemented:

* ``GET`` a collection (with ``$filter``/``$select``/``$top``/``$skip``)
* ``GET`` all pages of a collection
* ``POST`` a new record
* ``PATCH`` an existing record by id
* ``POST $batch`` with up to 100 sub-requests (JSON batch format)
* ``GET $metadata`` and a small CSDL parser used to validate field names

Creatio specifics handled here:

* ``ForceUseSession: true`` on every request (recommended by Creatio docs).
* ``BPMCSRF`` header from the authentication strategy.
* ``Content-Type: application/json; charset=utf-8; IEEE754Compatible=true`` for
  request bodies, which is what Creatio expects and what makes ``Edm.Decimal``
  and ``Edm.Int64`` serialise as strings.
* Retry with exponential backoff + jitter on 408/425/429/5xx and transport
  errors, honouring ``Retry-After``.
* One transparent re-authentication on 401 (and on 403 when cookie/CSRF based),
  because Creatio sessions expire.
* Batch requests use ``Prefer: continue-on-error`` so one bad row does not
  abort the remaining rows in the same batch.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any, Iterator, Mapping, Sequence
from uuid import UUID

import requests

from .auth import AuthManager
from .config import Settings
from .errors import BatchError, ConfigError, ODataError

_LOG = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# Methods whose responses are safe to resend after a transport error. POST and
# $batch are missing on purpose: a timeout may mean Creatio already applied the
# write, and resending would create duplicates.
IDEMPOTENT_METHODS = {"GET", "PATCH", "DELETE"}
JSON_CONTENT_TYPE = "application/json; charset=utf-8; IEEE754Compatible=true"
BATCH_CONTENT_TYPE = "application/json; odata=verbose; IEEE754Compatible=true"
EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"


def odata_escape(value: Any) -> str:
    """Escape a literal for use inside an OData ``$filter`` string."""
    return str(value).replace("'", "''")


def odata_literal(value: Any, *, quote: bool = True) -> str:
    """Render a value as an OData filter literal.

    Strings are single quoted, numbers and booleans are not, datetimes use the
    unquoted ISO form accepted by OData 4.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if quote:
        return f"'{odata_escape(value)}'"
    return str(value)


def json_default(value: Any) -> Any:
    """``json.dumps`` fallback for OData friendly scalar types."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat(sep="T", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dt_time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass
class BatchResult:
    id: str
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def record_id(self) -> str | None:
        if isinstance(self.body, Mapping):
            for key in ("Id", "id"):
                value = self.body.get(key)
                if isinstance(value, str) and value:
                    return value
        location = self.headers.get("location") or self.headers.get("Location")
        if location:
            match = re.search(r"\(([0-9a-fA-F-]{36})\)", location)
            if match:
                return match.group(1)
        return None

    @property
    def error_message(self) -> str | None:
        if self.ok:
            return None
        return extract_error_message(self.body) or f"HTTP {self.status}"


def extract_error_message(body: Any) -> str | None:
    """Pull a human readable message out of an OData/Creatio error body."""
    if body is None:
        return None
    if isinstance(body, str):
        text = body.strip()
        if not text:
            return None
        try:
            body = json.loads(text)
        except ValueError:
            return text[:1000]
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if message:
                return str(message)
        for key in ("Message", "message", "error_description", "detail"):
            if body.get(key):
                return str(body[key])
        return json.dumps(body, ensure_ascii=False)[:1000]
    return str(body)[:1000]


def hint_for_odata_error(error: "ODataError") -> str | None:
    """Map known Creatio error messages to the actual fix, in operator language."""
    message = (getattr(error, "detail", "") or "").lower()
    payload = str(getattr(error, "payload", "") or "").lower()
    blob = f"{message} {payload}"

    if "permissions to use odata" in blob:
        return (
            "User integrasi belum punya izin OData. Di Creatio: Setup > Users > "
            "System operation permissions, aktifkan operation \"Access to OData\" "
            "(code CanUseODataService) untuk user tersebut; lalu cek juga hak baca/tulis "
            "pada object yang dipakai (Access rights to objects)."
        )
    if "html" in message or "halaman html" in message:
        return (
            "Path OData kemungkinan salah. Untuk Creatio .NET Framework gunakan "
            "/0/odata; untuk .NET 8 gunakan /odata. Cek dengan "
            "'csmt check --odata-path /0/odata'."
        )
    if "bpmcsrf" in blob:
        return (
            "Token CSRF ditolak. Untuk Forms auth pastikan cookie BPMCSRF terkirim; "
            "untuk Basic auth perlindungan CSRF harus dimatikan (UseCsrfToken=false)."
        )
    if "insufficient permissions to" in blob and "object" in blob:
        return (
            "Izin pada object kurang. Beri user integrasi hak baca/tambah/ubah pada "
            "object tersebut (System Designer > Access rights to objects)."
        )
    return None


class ODataClient:
    """Session aware OData 4 client."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        auth: AuthManager | None = None,
        logger: logging.Logger | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings
        self.log = logger or _LOG
        self.session = session or requests.Session()
        self.auth = auth or AuthManager(settings, session=self.session, logger=self.log)
        self._sleep = sleep
        self.session.headers.setdefault("User-Agent", settings.user_agent)
        if not settings.verify_ssl:
            # One clear notice beats one urllib3 warning per request.
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover
                pass
            self.log.warning(
                "TLS verification is disabled for %s; set CREATIO_VERIFY_SSL=true in production",
                settings.root_url,
            )

    # ------------------------------------------------------------------ URLs
    def _url(self, path: str = "") -> str:
        path = (path or "").strip()
        if path.lower().startswith(("http://", "https://")):
            return path
        if not path:
            return self.settings.odata_url
        return f"{self.settings.odata_url}/{path.lstrip('/')}"

    @staticmethod
    def _require_entity(entity: str | None) -> str:
        entity = (entity or "").strip().strip("/")
        if not entity:
            raise ConfigError("An entity set name is required (--entity, e.g. Contact)")
        return entity

    # --------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str = "",
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        raw_body: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
        expected: Sequence[int] = (200, 201, 204),
        allow_404: bool = False,
        max_auth_retries: int = 1,
    ) -> requests.Response:
        method = method.upper()
        url = self._url(path)
        retries = 0
        auth_retries = 0

        if json_body is not None and raw_body is not None:
            raise ConfigError("Pass either json_body or raw_body, not both")

        while True:
            request_headers: dict[str, str] = {
                "Accept": "application/json",
                "ForceUseSession": "true",
            }
            if json_body is not None or raw_body is not None:
                request_headers["Content-Type"] = content_type or JSON_CONTENT_TYPE
            elif content_type:
                request_headers["Content-Type"] = content_type
            try:
                request_headers.update(self.auth.headers())
            except Exception as exc:  # auth failure should be reported, not retried forever
                raise ODataError(
                    f"Authentication failed before {method} {url}: {exc}",
                    method=method,
                    url=url,
                ) from exc
            if headers:
                request_headers.update(headers)

            if json_body is not None:
                body: bytes | str | None = json.dumps(
                    json_body, ensure_ascii=False, default=json_default
                ).encode("utf-8")
            else:
                body = raw_body

            try:
                response = self.session.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    data=body,
                    headers=request_headers,
                    timeout=self.settings.timeout,
                    verify=self.settings.verify_ssl,
                )
            except requests.RequestException as exc:
                # POST and $batch are not idempotent: a transport timeout may
                # mean the server already processed the write, so the transport
                # layer must NOT resend it. The pipeline reconciles via the key
                # instead. Any write method other than PATCH is also refused.
                if retries < self.settings.max_retries and method in IDEMPOTENT_METHODS:
                    retries += 1
                    self._backoff(retries, None, exc)
                    continue
                raise ODataError(
                    f"{method} {url} failed after {retries + 1} attempt(s): {exc}",
                    method=method,
                    url=url,
                    retryable=True,
                ) from exc

            status = response.status_code
            self.log.debug("%s %s -> HTTP %s", method, url, status)

            if status == 401 or (status == 403 and self.auth.uses_csrf):
                if auth_retries < max_auth_retries:
                    auth_retries += 1
                    self.log.info(
                        "Session rejected (HTTP %s); re-authenticating with %s and retrying",
                        status,
                        self.auth.mode,
                    )
                    self.auth.ensure(force=True)
                    continue

            # 429 means the rate limiter rejected the request BEFORE the server
            # executed it, so it is safe to honour Retry-After for every method.
            if status == 429 or (status in RETRY_STATUSES and method in IDEMPOTENT_METHODS):
                if retries < self.settings.max_retries:
                    retries += 1
                    self._backoff(retries, response.headers.get("Retry-After"), None)
                    continue
                raise ODataError.from_response(response, method=method, url=url)

            if status in RETRY_STATUSES:
                # Not retryable (POST/$batch on 5xx): the write may already have
                # been applied, so surface it to the pipeline for
                # reconciliation instead of resending a duplicate.
                raise ODataError.from_response(response, method=method, url=url)

            if status in expected:
                return response
            if allow_404 and status == 404:
                return response

            raise ODataError.from_response(response, method=method, url=url)

    def _backoff(self, attempt: int, retry_after: str | None, exc: Exception | None) -> None:
        delay: float
        try:
            if retry_after is not None:
                delay = max(0.0, float(str(retry_after).strip()))
            else:
                raise ValueError
        except (TypeError, ValueError):
            delay = self.settings.retry_backoff * (2 ** max(0, attempt - 1))
        delay = min(delay, self.settings.retry_max_delay)
        delay += random.uniform(0, 0.25 * delay) if delay else 0.0
        self.log.warning(
            "Retrying in %.2fs (attempt %s, %s)", delay, attempt, exc or "server asked us to wait"
        )
        self._sleep(delay)

    # ----------------------------------------------------------------- reads
    def get_collection(
        self,
        entity: str,
        *,
        filter: str | None = None,
        select: Sequence[str] | None = None,
        top: int | None = None,
        skip: int | None = None,
        orderby: str | None = None,
        count: bool = False,
    ) -> tuple[list[dict[str, Any]], int | None]:
        entity = self._require_entity(entity)
        params: dict[str, Any] = {}
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = ",".join(select)
        if top is not None:
            params["$top"] = int(top)
        if skip is not None:
            params["$skip"] = int(skip)
        if orderby:
            params["$orderby"] = orderby
        if count:
            params["$count"] = "true"

        response = self.request("GET", entity, params=params, expected=(200,))
        payload = self._parse_json(response, entity=entity)
        items = payload.get("value") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            items = []
        total = None
        if count and isinstance(payload, Mapping):
            raw_total = payload.get("@odata.count")
            if raw_total is not None:
                try:
                    total = int(raw_total)
                except (TypeError, ValueError):
                    total = None
        return items, total

    def iter_collection(
        self,
        entity: str,
        *,
        filter: str | None = None,
        select: Sequence[str] | None = None,
        page_size: int | None = None,
        orderby: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every record, paging with ``$top``/``$skip`` until exhausted.

        Stops as soon as a page returns fewer rows than ``page_size`` (which also
        covers the empty page after an exact multiple). This is skip-based paging,
        so the server-side order should be stable for the whole run; pass
        ``orderby`` when the data may change while the export is in progress.
        """
        entity = self._require_entity(entity)
        page_size = page_size or self.settings.page_size
        skip = 0
        while True:
            items, _ = self.get_collection(
                entity,
                filter=filter,
                select=select,
                top=page_size,
                skip=skip if skip else None,
                orderby=orderby,
            )
            if not items:
                return
            for item in items:
                yield item
            if len(items) < page_size:
                return
            skip += len(items)

    def count(self, entity: str, *, filter: str | None = None) -> int:
        entity = self._require_entity(entity)
        params: dict[str, Any] = {"$count": "true", "$top": 0}
        if filter:
            params["$filter"] = filter
        response = self.request("GET", entity, params=params, expected=(200,))
        payload = self._parse_json(response, entity=entity)
        if isinstance(payload, Mapping):
            raw = payload.get("@odata.count")
            if raw is None:
                raw = payload.get("value")
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        # Fallback for services that ignore $count: count pages.
        return sum(1 for _ in self.iter_collection(entity, filter=filter, select=["Id"]))

    def get_record(self, entity: str, record_id: str) -> dict[str, Any] | None:
        entity = self._require_entity(entity)
        response = self.request(
            "GET", f"{entity}({record_id})", expected=(200,), allow_404=True
        )
        if response.status_code == 404:
            return None
        payload = self._parse_json(response, entity=entity)
        return payload if isinstance(payload, dict) else None

    # ---------------------------------------------------------------- writes
    def create(self, entity: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        entity = self._require_entity(entity)
        response = self.request("POST", entity, json_body=dict(payload), expected=(200, 201))
        parsed = self._parse_json(response, entity=entity, allow_empty=True)
        if isinstance(parsed, dict):
            return parsed
        record_id = extract_record_id_from_location(response.headers.get("Location"))
        return {"Id": record_id} if record_id else {}

    def update(self, entity: str, record_id: str, payload: Mapping[str, Any]) -> None:
        entity = self._require_entity(entity)
        if not record_id:
            raise ODataError("Cannot PATCH a record without an id", method="PATCH")
        self.request(
            "PATCH",
            f"{entity}({record_id})",
            json_body=dict(payload),
            expected=(200, 204),
        )

    def batch(
        self,
        operations: Sequence[Mapping[str, Any]],
        *,
        continue_on_error: bool = True,
    ) -> dict[str, BatchResult]:
        """Execute JSON batch operations and return results keyed by request id.

        Each operation is a dict with ``method``, ``url``, ``id``, optional
        ``body`` and optional ``headers``. ``url`` is relative to the OData root
        (e.g. ``Contact`` or ``Contact(guid)``).
        """
        if not operations:
            return {}
        if len(operations) > 100:
            raise ConfigError(
                f"A Creatio batch can contain at most 100 requests, got {len(operations)}"
            )

        body = {"requests": [dict(op) for op in operations]}
        headers = {"Prefer": "continue-on-error"} if continue_on_error else {}

        try:
            response = self.request(
                "POST",
                "$batch",
                json_body=body,
                headers=headers,
                content_type=BATCH_CONTENT_TYPE,
                expected=(200, 202),
            )
        except ODataError as exc:
            raise BatchError(
                f"Batch request failed: {exc.detail}",
                status=exc.status,
                code=exc.code,
                payload=exc.payload,
                method="POST",
                url=self._url("$batch"),
                retryable=exc.retryable,
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BatchError(
                "Batch response was not JSON; cannot map results back to rows",
                status=response.status_code,
                payload=(response.text or "")[:2000],
                method="POST",
                url=self._url("$batch"),
            ) from exc

        return self._parse_batch_payload(payload)

    def _parse_batch_payload(self, payload: Any) -> dict[str, BatchResult]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("responses"), list):
            raise BatchError(
                "Unrecognised batch response shape; expected {'responses': [...]}",
                payload=payload,
            )
        results: dict[str, BatchResult] = {}
        for item in payload["responses"]:
            if not isinstance(item, Mapping):
                continue
            request_id = item.get("id")
            if request_id is None:
                continue
            raw_status = item.get("status", 0)
            try:
                status = int(raw_status)
            except (TypeError, ValueError):
                status = 0
            body = item.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    pass
            raw_headers = item.get("headers")
            headers = (
                {str(key).lower(): str(value) for key, value in raw_headers.items()}
                if isinstance(raw_headers, Mapping)
                else {}
            )
            results[str(request_id)] = BatchResult(
                id=str(request_id), status=status, body=body, headers=headers
            )
        return results

    # -------------------------------------------------------------- metadata
    def fetch_metadata(self) -> str:
        # Creatio .NET Framework tidak mendukung metadata JSON dan menjawab 500
        # ("The JSON metadata is not supported at this platform") bila Accept
        # berisi application/json. 500 juga masuk RETRY_STATUSES, jadi tanpa
        # override ini sketch request diulang 5 kali sebelum gagal.
        response = self.request(
            "GET",
            "$metadata",
            expected=(200,),
            headers={"Accept": "application/xml"},
        )
        return response.text

    def entity_fields(self, entity: str) -> dict[str, str]:
        """Return ``{field_name: edm_type}`` for an entity set, parsed from CSDL."""
        return parse_csdl_entity_fields(self.fetch_metadata(), self._require_entity(entity))

    def entity_field_details(self, entity: str) -> list[CsdlFieldDetail]:
        """Return every field with CSDL attributes (nullable, key, lookup, ...)."""
        return parse_csdl_field_details(self.fetch_metadata(), self._require_entity(entity))

    # --------------------------------------------------------------- helpers
    def service_document(self) -> dict[str, Any]:
        response = self.request("GET", "", expected=(200,))
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}

    def ping(self) -> dict[str, Any]:
        """Verify the URL, credentials and that the configured entity exists."""
        info: dict[str, Any] = {
            "root_url": self.settings.root_url,
            "odata_url": self.settings.odata_url,
            "auth_mode": self.auth.mode,
        }
        document = self.service_document()
        collections = document.get("value")
        info["collections"] = len(collections) if isinstance(collections, list) else None

        entity = self.settings.entity
        if entity:
            items, _ = self.get_collection(entity, top=1)
            info["entity"] = entity
            info["entity_reachable"] = True
            info["sample_fields"] = sorted(items[0].keys()) if items else []
        return info

    def _parse_json(
        self, response: requests.Response, *, entity: str | None = None, allow_empty: bool = False
    ) -> Any:
        text = response.text or ""
        if not text.strip():
            if allow_empty:
                return None
            raise ODataError(
                "Empty response body",
                status=response.status_code,
                method=response.request.method if response.request else "GET",
                url=response.url,
            )
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" in content_type:
            # Creatio redirects unauthenticated/unknown OData paths to the web login
            # page, which requests follows and turns into a 200 HTML body.
            raise ODataError(
                "Endpoint mengembalikan halaman HTML, bukan JSON OData. "
                "Kemungkinan path OData salah untuk instance ini (diarahkan ke halaman "
                "login), atau sesi tidak diotorisasi. Coba /0/odata dan /odata secara "
                "bergantian.",
                status=response.status_code,
                payload=text[:500],
                method=response.request.method if response.request else None,
                url=response.url,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ODataError(
                f"Response is not valid JSON for entity {entity or '?'}: {text[:500]}",
                status=response.status_code,
                method=response.request.method if response.request else None,
                url=response.url,
            ) from exc

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass


def extract_record_id_from_location(location: str | None) -> str | None:
    if not location:
        return None
    match = re.search(r"\(([0-9a-fA-F-]{36})\)", location)
    if match:
        return match.group(1)
    tail = location.rsplit("/", 1)[-1].strip(")")
    return tail or None


def _looks_like_html(xml_text: str) -> bool:
    head = xml_text[:2048].lower()
    return "<!doctype html" in head or "<html" in head


@dataclass(frozen=True)
class LookupRef:
    """Navigation name plus the entity it points to (e.g. ``Owner`` -> ``Contact``)."""

    navigation: str
    entity: str


@dataclass(frozen=True)
class CsdlFieldDetail:
    """One CSDL property with the attributes that matter for templates."""

    name: str
    type: str
    nullable: bool = True
    max_length: int | None = None
    key: bool = False
    lookup: LookupRef | None = None


def _csdl_index(xml_text: str) -> tuple[dict[str, ET.Element], dict[str, str]]:
    """Return ``(entity_types, entity_sets)`` from a CSDL document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ODataError(f"Could not parse $metadata: {exc}") from exc

    entity_types: dict[str, ET.Element] = {}
    for element in root.iter(f"{{{EDM_NS}}}EntityType"):
        name = element.get("Name")
        if name:
            entity_types[name] = element

    entity_sets: dict[str, str] = {}
    for element in root.iter(f"{{{EDM_NS}}}EntitySet"):
        name = element.get("Name")
        if name:
            raw = element.get("EntityType") or ""
            entity_sets[name] = raw.split(".")[-1] if raw else name

    return entity_types, entity_sets


def _resolve_type_name(
    entity_types: dict[str, ET.Element], entity_sets: dict[str, str], entity_set: str
) -> str:
    type_name = entity_sets.get(entity_set)
    if type_name is None and entity_set in entity_types:
        type_name = entity_set
    if type_name is None or type_name not in entity_types:
        raise ODataError(
            f"Entity set {entity_set!r} was not found in $metadata "
            f"(known: {', '.join(sorted(entity_types)[:15])})"
        )
    return type_name


def parse_csdl_field_details(xml_text: str, entity_set: str) -> list[CsdlFieldDetail]:
    """Return every property of ``entity_set`` with its CSDL attributes.

    Lookup columns are detected from ``NavigationProperty`` entries: a GUID
    property named ``OwnerId`` whose ``Owner`` navigation exists is a lookup to
    the navigation target entity. Collection navigations are ignored.
    """
    if _looks_like_html(xml_text):
        # Halaman login sering berupa XHTML yang sempat lolos parser XML dan
        # berujung diagnosa "entity set not found"; periksa sebelum parsing.
        raise ODataError(
            "Endpoint mengembalikan halaman HTML, bukan dokumen CSDL. "
            "Kemungkinan path OData salah (diarahkan ke halaman login) "
            "atau token tidak diterima. Coba /0/odata dan /odata secara bergantian."
        )

    entity_types, entity_sets = _csdl_index(xml_text)
    type_name = _resolve_type_name(entity_types, entity_sets, entity_set)

    details: dict[str, CsdlFieldDetail] = {}
    visited: set[str] = set()

    def collect(current: str, *, depth: int = 0) -> None:
        # CSDL entities inherit properties from their ``BaseType``; the walk is
        # defensive against cycles and depth so metadata never hangs the run.
        if depth > 10 or current in visited or current not in entity_types:
            return
        visited.add(current)
        element = entity_types[current]

        nav_targets: dict[str, str] = {}
        for nav in element.findall(f"{{{EDM_NS}}}NavigationProperty"):
            nav_name = nav.get("Name")
            nav_type = nav.get("Type") or ""
            if not nav_name or nav_type.startswith("Collection("):
                continue
            nav_targets[nav_name] = nav_type.split(".")[-1]

        key_names = {
            ref.get("Name")
            for ref in element.findall(f"{{{EDM_NS}}}Key/{{{EDM_NS}}}PropertyRef")
            if ref.get("Name")
        }

        for prop in element.findall(f"{{{EDM_NS}}}Property"):
            name = prop.get("Name")
            if not name or name in details:
                continue

            edm_type = prop.get("Type") or ""
            raw_max = prop.get("MaxLength")
            lookup: LookupRef | None = None
            if edm_type == "Edm.Guid":
                direct = nav_targets.get(name)
                stripped = nav_targets.get(name[:-2]) if name.endswith("Id") else None
                target = direct or stripped
                if target:
                    lookup = LookupRef(
                        navigation=name if direct else name[:-2], entity=target
                    )

            details[name] = CsdlFieldDetail(
                name=name,
                type=edm_type,
                nullable=prop.get("Nullable") != "false",
                max_length=int(raw_max) if raw_max and raw_max.isdigit() else None,
                key=name in key_names,
                lookup=lookup,
            )

        base = element.get("BaseType")
        if base:
            collect(base.split(".")[-1], depth=depth + 1)

    collect(type_name)
    return list(details.values())


def parse_csdl_entity_fields(xml_text: str, entity_set: str) -> dict[str, str]:
    """Map an entity set name to its property names and EDM types.

    Thin wrapper over :func:`parse_csdl_field_details` kept for compatibility.
    """
    return {detail.name: detail.type for detail in parse_csdl_field_details(xml_text, entity_set)}


__all__ = [
    "BATCH_CONTENT_TYPE",
    "CsdlFieldDetail",
    "LookupRef",
    "IDEMPOTENT_METHODS",
    "BatchResult",
    "JSON_CONTENT_TYPE",
    "ODataClient",
    "extract_error_message",
    "extract_record_id_from_location",
    "json_default",
    "odata_escape",
    "odata_literal",
    "parse_csdl_entity_fields",
    "parse_csdl_field_details",
]
