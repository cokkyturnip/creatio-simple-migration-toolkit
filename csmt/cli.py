"""Command line interface.

Examples
--------
::

    # verify URL, credentials and entity
    python -m csmt check --base-url https://host --username u --password p --entity Contact

    # dry run first: shows what would be created/updated
    python -m csmt import --csv contacts.csv --entity Contact \
        --mapping mapping.json --mode upsert --dry-run

    # real import with batching, resume enabled and a report
    python -m csmt import --csv contacts.csv --entity Contact \
        --mapping mapping.json --mode upsert --batch-size 50 --resume

    # inspect the entity schema from $metadata
    python -m csmt schema --base-url https://host --username u --password p --entity Contact

    # generate a starter mapping from CSV headers
    python -m csmt init-mapping --csv contacts.csv --key Email --out mapping.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import __version__
import getpass
import re

from .client import ODataClient, hint_for_odata_error
import requests

from .config import Settings, default_identity_url, load_env_file, normalize_base_url
from .tabular import open_source
from .export import ExportOptions, Exporter, LookupExpansion
from .errors import ConfigError, CreatioError, ODataError
from .journal import Journal
from .mapping import Mapper, Mapping, build_identity_mapping
from .pipeline import ImportPipeline, PipelineOptions
from .report import ReportWriter

_LOG = logging.getLogger("csmt")


# --------------------------------------------------------------------- parser
def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("connection")
    group.add_argument("--base-url", help="Creatio base URL, e.g. https://mycreatio.com")
    group.add_argument(
        "--odata-path",
        help="OData service path (default /0/odata for .NET Framework, /odata for .NET 8)",
    )
    group.add_argument(
        "--auth-mode",
        choices=("auto", "basic", "forms", "oauth2"),
        help="Authentication method; 'auto' picks forms or oauth2 from the credentials given",
    )
    group.add_argument("--username")
    group.add_argument("--password")
    group.add_argument("--client-id", help="OAuth 2.0 client id")
    group.add_argument("--client-secret", help="OAuth 2.0 client secret")
    group.add_argument(
        "--identity-url",
        help="OAuth 2.0 identity service URL = system setting OAuth20IdentityServerUrl "
        "(token endpoint: {identity-url}/connect/token)",
    )
    group.add_argument("--scope", help="OAuth scope, if the identity server requires one")
    group.add_argument("--insecure", action="store_true", help="Do not verify TLS certificates")
    group.add_argument("--timeout", type=float, help="HTTP timeout in seconds (default 60)")
    group.add_argument("--max-retries", type=int, help="Retries for 429/5xx/transport errors")
    group.add_argument("--env-file", help="Load configuration from this .env file")


def _add_logging_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("logging")
    group.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Console log level (default INFO)",
    )
    group.add_argument("--log-file", help="Also append logs to this file")
    group.add_argument("--quiet", action="store_true", help="Only log warnings and errors")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csmt",
        description="Migrate spreadsheet (CSV) data to and from Creatio through the OData 4 web service.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- import -----------------------------------------------------------
    import_parser = subparsers.add_parser(
        "import",
        help="Import a CSV file",
        description="Map a CSV file onto a Creatio entity set and create/update records.",
    )
    _add_connection_args(import_parser)
    _add_logging_args(import_parser)
    import_parser.add_argument("--csv", required=True, help="Path to the CSV file")
    import_parser.add_argument("--entity", help="Entity set name, e.g. Contact")
    import_parser.add_argument(
        "--mapping", help="JSON mapping file; omit for a 1:1 column-to-field mapping"
    )
    import_parser.add_argument(
        "--key",
        help="Comma separated key field(s) for matching existing records, e.g. Email",
    )
    import_parser.add_argument(
        "--mode",
        choices=("create", "update", "upsert"),
        default="upsert",
        help="create = always POST, update = PATCH existing only, upsert = PATCH or POST (default)",
    )
    import_parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Records per $batch request, 1-100 (default 50)",
    )
    import_parser.add_argument("--no-batch", action="store_true", help="Send one request per record")
    import_parser.add_argument(
        "--dry-run", action="store_true", help="Validate and plan, but do not write to Creatio"
    )
    import_parser.add_argument("--limit", type=int, help="Only process the first N data rows")
    import_parser.add_argument(
        "--create-missing",
        action="store_true",
        help="In update mode, create records whose key is not found",
    )
    import_parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="In create mode, skip records whose key already exists",
    )
    import_parser.add_argument(
        "--duplicate-policy",
        choices=("error", "first_wins", "last_wins"),
        default="error",
        help="How to treat duplicate keys inside the CSV (default error)",
    )
    import_parser.add_argument(
        "--resume", action="store_true", help="Skip rows already completed in the journal"
    )
    import_parser.add_argument("--journal", help="Journal path (default <csv>.journal.jsonl)")
    import_parser.add_argument(
        "--no-journal", action="store_true", help="Disable the resume journal"
    )
    import_parser.add_argument("--report", help="CSV report path")
    import_parser.add_argument(
        "--no-report", action="store_true", help="Do not write a CSV report"
    )
    import_parser.add_argument(
        "--fail-fast",
        dest="continue_on_error",
        action="store_false",
        help="Stop at the first failed record",
    )
    import_parser.set_defaults(continue_on_error=True)
    import_parser.add_argument(
        "--max-errors", type=int, default=0, help="Stop after N failed records (0 = unlimited)"
    )
    import_parser.add_argument(
        "--strict-sources",
        action="store_true",
        help="Fail when the CSV lacks a column referenced by the mapping",
    )
    import_parser.add_argument(
        "--validate-fields",
        action="store_true",
        help="Check mapping targets against the entity schema from $metadata",
    )
    import_parser.add_argument("--delimiter", help="CSV delimiter (auto-detected by default)")
    import_parser.add_argument("--encoding", help="CSV encoding (auto-detected by default)")
    import_parser.add_argument(
        "--sheet", help="Worksheet name or 1-based index (.xlsx/.xlsm only)"
    )
    import_parser.add_argument(
        "--header-row",
        type=int,
        default=1,
        help="Row number holding the header (.xlsx/.xlsm only, default 1)",
    )
    import_parser.add_argument(
        "--chunk-rows", type=int, default=500, help="Rows buffered before lookups/requests"
    )
    import_parser.add_argument(
        "--progress-every", type=int, default=200, help="Log progress every N rows"
    )
    import_parser.add_argument(
        "--no-header",
        action="store_true",
        help="CSV has no header row; columns are named column_1..column_N",
    )

    # -- check ------------------------------------------------------------
    check_parser = subparsers.add_parser(
        "check", help="Verify URL, credentials and entity access"
    )
    _add_connection_args(check_parser)
    _add_logging_args(check_parser)
    check_parser.add_argument("--entity", help="Also check that this entity set is readable")

    # -- schema -----------------------------------------------------------
    schema_parser = subparsers.add_parser(
        "schema", help="Print entity fields and EDM types from $metadata"
    )
    _add_connection_args(schema_parser)
    _add_logging_args(schema_parser)
    schema_parser.add_argument("--entity", required=True, help="Entity set name")
    schema_parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    # -- init-mapping -----------------------------------------------------
    init_parser = subparsers.add_parser(
        "init-mapping", help="Generate a starter mapping JSON from CSV headers"
    )
    _add_logging_args(init_parser)
    init_parser.add_argument("--csv", required=True, help="Path to the CSV file")
    init_parser.add_argument("--out", default="mapping.json", help="Output mapping path")
    init_parser.add_argument("--entity", help="Entity set name to store in the mapping")
    init_parser.add_argument("--key", help="Comma separated key column(s)")
    init_parser.add_argument("--delimiter", help="CSV delimiter (auto-detected by default)")
    init_parser.add_argument("--encoding", help="CSV encoding (auto-detected by default)")
    init_parser.add_argument("--no-header", action="store_true")
    init_parser.add_argument("--sheet", help="Worksheet name or index (.xlsx/.xlsm only)")
    init_parser.add_argument(
        "--header-row", type=int, default=1, help="Header row number (.xlsx/.xlsm only)"
    )

    # -- export -----------------------------------------------------------
    export_parser = subparsers.add_parser(
        "export",
        help="Export Creatio records to a CSV file",
        description="Stream records from a Creatio entity set into a CSV file.",
    )
    _add_connection_args(export_parser)
    _add_logging_args(export_parser)
    export_parser.add_argument("--entity", required=True, help="Entity set name, e.g. Contact")
    export_parser.add_argument(
        "--out", help="Output CSV path (default <entity>-export-<timestamp>.csv)"
    )
    export_parser.add_argument(
        "--select", help="Comma separated fields to export (default: all fields returned)"
    )
    export_parser.add_argument("--filter", help="OData $filter expression, passed through as is")
    export_parser.add_argument("--orderby", help="OData $orderby, e.g. \"CreatedOn desc\"")
    export_parser.add_argument("--limit", type=int, help="Stop after N rows")
    export_parser.add_argument(
        "--page-size", type=int, help="Records per request (default from CREATIO_PAGE_SIZE/500)"
    )
    export_parser.add_argument(
        "--expand-lookups",
        action="append",
        default=[],
        metavar="FIELD=ENTITY[.CAPTION]",
        help="Replace GUIDs with captions, e.g. CountryId=Country.Name (repeatable)",
    )
    export_parser.add_argument(
        "--format",
        choices=("auto", "csv", "xlsx"),
        default="auto",
        help="Output format; default auto from the --out extension",
    )
    export_parser.add_argument(
        "--sheet-name",
        "--sheet",
        dest="sheet_name",
        help="Worksheet name for .xlsx output (default: entity name)",
    )
    export_parser.add_argument("--delimiter", default=",", help="CSV delimiter (default ,)")
    export_parser.add_argument(
        "--encoding", default="utf-8-sig", help="CSV encoding (default utf-8-sig, Excel friendly)"
    )
    export_parser.add_argument(
        "--no-header", action="store_true", help="Do not write the header row"
    )

    # -- wizard -----------------------------------------------------------
    wizard_parser = subparsers.add_parser(
        "wizard",
        help="Interactive wizard: create .env (URL, auth mode, credentials)",
        description=(
            "Step-by-step configuration wizard. Asks for the Creatio URL, the "
            "OData path for your runtime (.NET 8 -> /odata, .NET Framework -> "
            "/0/odata), the authentication method and credentials, then writes "
            "a ready-to-use .env and offers to test the connection."
        ),
    )
    wizard_parser.add_argument(
        "--out", default=".env", help="Target env file (default ./.env)"
    )

    return parser


# -------------------------------------------------------------------- helpers
def configure_logging(args: argparse.Namespace) -> None:
    level = logging.WARNING if getattr(args, "quiet", False) else getattr(args, "log_level", "INFO")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = getattr(args, "log_file", None)
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def build_settings(args: argparse.Namespace) -> Settings:
    env_file = getattr(args, "env_file", None)
    if env_file:
        load_env_file(env_file)
    else:
        default_env = Path.cwd() / ".env"
        if default_env.exists():
            load_env_file(default_env)

    overrides: dict[str, Any] = {
        "base_url": getattr(args, "base_url", None),
        "odata_path": getattr(args, "odata_path", None),
        "entity": getattr(args, "entity", None) or None,
        "auth_mode": getattr(args, "auth_mode", None),
        "username": getattr(args, "username", None),
        "password": getattr(args, "password", None),
        "client_id": getattr(args, "client_id", None),
        "client_secret": getattr(args, "client_secret", None),
        "identity_url": getattr(args, "identity_url", None),
        "scope": getattr(args, "scope", None),
        "timeout": getattr(args, "timeout", None),
        "max_retries": getattr(args, "max_retries", None),
    }
    if getattr(args, "insecure", False):
        overrides["verify_ssl"] = False

    # Explicit credentials on the command line beat an auth mode coming from .env:
    # passing --username/--password must not silently attempt OAuth just because
    # the environment file remembers a previous configuration.
    explicit_user = bool(getattr(args, "username", None) and getattr(args, "password", None))
    explicit_client = bool(
        getattr(args, "client_id", None) and getattr(args, "client_secret", None)
    )
    if not getattr(args, "auth_mode", None) and explicit_user and not explicit_client:
        overrides["auth_mode"] = "forms"
    return Settings.from_env(**overrides)


def parse_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _default_report_path(csv_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return csv_path.with_name(f"{csv_path.stem}-report-{stamp}.csv")


def _print_summary(summary: Any, stream: Any = None) -> None:
    stream = stream or sys.stdout
    print("", file=stream)
    print("Import summary", file=stream)
    print("--------------", file=stream)
    for key, value in summary.as_dict().items():
        print(f"  {key:>17}: {value}", file=stream)


# ------------------------------------------------------------------- commands
def cmd_check(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    client = ODataClient(settings)
    try:
        info = client.ping()
    except ODataError as exc:
        hint = hint_for_odata_error(exc)
        if hint:
            raise ODataError(
                f"{exc.detail} | {hint}",
                status=exc.status,
                code=exc.code,
                payload=exc.payload,
                method=exc.method,
                url=exc.url,
            ) from exc
        raise
    finally:
        client.close()

    if settings.auth_mode in {"oauth2", "auto"} and settings.client_id:
        identity = getattr(client.auth.strategy, "_endpoint_used", None)
        if identity:
            info["identity_endpoint_used"] = identity
        else:
            info["identity_candidates"] = settings.identity_candidates
    print(json.dumps(info, indent=2, default=str))
    print("OK: connection and credentials are valid", file=sys.stdout)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    client = ODataClient(settings)
    try:
        fields = client.entity_fields(args.entity)
    finally:
        client.close()
    if args.json:
        print(json.dumps(fields, indent=2, sort_keys=True))
        return 0
    width = max((len(name) for name in fields), default=10)
    print(f"Entity set: {args.entity} ({len(fields)} fields)")
    for name in sorted(fields):
        print(f"  {name:<{width}}  {fields[name]}")
    return 0


def cmd_init_mapping(args: argparse.Namespace) -> int:
    source = open_source(
        args.csv,
        encoding=args.encoding,
        delimiter=args.delimiter,
        has_header=not args.no_header,
        sheet=args.sheet,
        header_row=args.header_row,
    )
    document = build_identity_mapping(
        source.fieldnames, entity=args.entity, key_columns=parse_keys(args.key)
    )
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(f"Wrote starter mapping to {out_path}")
    info = source.info()
    if info.format == "csv":
        print(f"Detected delimiter {info.delimiter!r}, encoding {info.encoding}")
    else:
        print(
            f"Detected sheet {info.sheet!r}, header row {info.header_row}, "
            f"{len(info.fieldnames)} columns"
        )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    csv_path = Path(args.csv).expanduser()
    entity = (args.entity or settings.entity or "").strip()

    source = open_source(
        csv_path,
        encoding=args.encoding,
        delimiter=args.delimiter,
        has_header=not args.no_header,
        sheet=args.sheet,
        header_row=args.header_row,
    )
    keys = parse_keys(args.key)

    if args.mapping:
        mapping = Mapping.load(args.mapping)
        if keys:
            mapping.key_fields = keys
            mapping.validate_keys()
        if entity:
            mapping.entity = entity
    else:
        mapping = Mapping.identity(source.fieldnames, key_fields=keys, entity=entity or None)

    entity = (mapping.entity or entity or "").strip()
    if not entity:
        raise ConfigError("Entity set name is required (--entity or 'entity' in the mapping)")
    mapping.entity = entity

    mapper = Mapper(mapping)

    journal_path = None
    if not args.no_journal:
        journal_path = (
            Path(args.journal).expanduser()
            if args.journal
            else csv_path.with_suffix(csv_path.suffix + ".journal.jsonl")
        )

    report_path = None
    if not args.no_report:
        report_path = (
            Path(args.report).expanduser() if args.report else _default_report_path(csv_path)
        )

    options = PipelineOptions(
        mode=args.mode,
        batch_size=args.batch_size,
        use_batch=not args.no_batch,
        dry_run=args.dry_run,
        limit=args.limit,
        create_missing=args.create_missing,
        skip_existing=args.skip_existing,
        continue_on_error=args.continue_on_error,
        duplicate_policy=args.duplicate_policy,
        resume=args.resume,
        strict_sources=args.strict_sources,
        progress_every=args.progress_every,
        chunk_rows=args.chunk_rows,
        max_errors=args.max_errors,
    )

    client = ODataClient(settings)
    journal = Journal(journal_path)
    report = ReportWriter(report_path)

    try:
        if args.validate_fields:
            _LOG.info("Validating mapping against $metadata of %s", entity)
            fields = client.entity_fields(entity)
            mapper.mapping.validate_targets(fields)

        pipeline = ImportPipeline(
            settings=settings,
            client=client,
            mapper=mapper,
            source=source,
            journal=journal,
            report=report,
            options=options,
        )
        summary = pipeline.run()
    finally:
        journal.close()
        report.close()
        client.close()

    _print_summary(summary)
    if summary.report_path:
        print(f"Report: {summary.report_path}")
    if summary.journal_path:
        print(f"Journal: {summary.journal_path}")
    if summary.has_failures:
        print(
            f"WARNING: {summary.failed} failed and {summary.unknown} unknown row(s). "
            "See the report for details.",
            file=sys.stderr,
        )
        return 1
    return 0


def _ask(message: str, *, default: str | None = None) -> str:
    """input() with graceful EOF/abort handling."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{message}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise CreatioError("Wizard dibatalkan")
    if not raw and default is not None:
        return default
    return raw


def _ask_secret(label: str) -> str:
    """Prompt for a secret without echoing it, explaining that up front.

    Pasting is recommended over typing: hidden input gives no visual feedback, so
    typos in long secrets are easy and hard to notice.
    """
    print(f"  Info: {label} TIDAK ditampilkan saat diketik (input tersembunyi).")
    print("        Disarankan COPY-PASTE nilai dari password manager/Creatio,")
    print("        jangan mengetik manual — salah ketik tidak terlihat dan sulit dilacak.")
    try:
        return getpass.getpass(f"{label}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise CreatioError("Wizard dibatalkan")


def _ask_choice(
    message: str,
    options: list[tuple[str, str, str]],
    *,
    default_index: int,
) -> tuple[str, str, str]:
    """Render numbered choices with a description line, return the chosen tuple.

    ``options`` items are ``(label, value, description)``. Printing the
    description next to every number is deliberate: the operator must be able to
    decide without guessing what "1" or "2" means.
    """
    for index, (label, _value, description) in enumerate(options, start=1):
        marker = " (default)" if index - 1 == default_index else ""
        print(f"  {index}. {label}{marker}")
        if description:
            print(f"     {description}")
    while True:
        raw = _ask(message, default=str(default_index + 1))
        try:
            index = int(raw) - 1
        except ValueError:
            print(f"  Masukkan angka 1-{len(options)}")
            continue
        if 0 <= index < len(options):
            return options[index]
        print(f"  Masukkan angka 1-{len(options)}")


def _probe_identity_endpoint(
    identity_url: str, client_id: str | None = None, client_secret: str | None = None
) -> tuple[str, str]:
    """Probe ``{identity}/connect/token`` using the credentials already entered.

    Returns ``(status, message)`` where status is:

    * ``"token"``   - a token was issued: host AND credentials are valid
    * ``"host"``    - the host is a real identity server but rejected the client
    * ``"bad_host"``- TLS/404/connection problem, i.e. wrong hostname

    Using the real credentials (rather than dummy ones) makes this a single,
    meaningful check and avoids pointless failed logins that a SIEM may flag.
    A JSON OAuth error from the endpoint is what proves the host is an identity
    server; the exact error then tells whether the secret is wrong.
    """
    endpoint = identity_url.rstrip("/") + "/connect/token"
    data = {"grant_type": "client_credentials", "client_id": client_id or "csmt-probe"}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        response = requests.post(
            endpoint,
            data=data,
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except requests.exceptions.SSLError:
        return "bad_host", (
            f"TLS gagal untuk {endpoint} (sertifikat tidak cocok untuk host ini). "
            "Hostname identity service kemungkinan salah."
        )
    except requests.RequestException as exc:
        return "bad_host", f"tidak bisa menghubungi {endpoint}: {exc}"

    if response.status_code == 200:
        return "token", "token diterima: host dan kredensial valid"

    body: dict = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        body = {}
    error = str(body.get("error") or "")
    detail = str(body.get("error_description") or error or (response.text or "")[:120])

    if response.status_code in (400, 401) and body:
        return "host", (
            f"host identity benar, tetapi kredensial ditolak "
            f"(HTTP {response.status_code}: {detail})"
        )
    if response.status_code == 404:
        return "bad_host", f"HTTP 404 di {endpoint} (host ini bukan identity server)"
    return "bad_host", f"HTTP {response.status_code} dari {endpoint} (bukan respons token OAuth)"


def _quiet_insecure_warnings() -> None:
    """Silence urllib3's per-request warning; we log one explicit notice instead."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # pragma: no cover - urllib3 always ships with requests
        pass


def _probe_odata_path(
    base_url: str, path: str, *, auth_mode: str, identity_url: str | None,
    client_id: str | None, client_secret: str | None,
    username: str | None, password: str | None, verify_ssl: bool,
) -> tuple[str, str]:
    """Check that ``{base}{path}`` really serves OData for the current credentials.

    Returns ``("ok"|"permission"|"bad_path"|"error", message)``. A login redirect
    (HTML) or 404 means the path is wrong; a 403 mentioning OData permissions means
    the path is right and the integration user needs the "Access to OData" operation.
    """
    from .client import hint_for_odata_error

    settings = Settings(
        base_url=base_url,
        odata_path=path,
        auth_mode=auth_mode,
        identity_url=identity_url,
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
    )
    client = ODataClient(settings)
    try:
        document = client.service_document()
        collections = document.get("value")
        count = len(collections) if isinstance(collections, list) else 0
        return "ok", f"{path} menjawab JSON OData ({count} collection)"
    except ODataError as exc:
        text = f"{exc.detail} {exc.payload or ''}".lower()
        hint = hint_for_odata_error(exc)
        if exc.status in (401, 403) or "permissions to use odata" in text:
            return "permission", hint or exc.detail
        if "html" in text or exc.status in (301, 302, 404):
            return "bad_path", exc.detail
        return "error", hint or exc.detail
    finally:
        client.close()


def cmd_wizard(args: argparse.Namespace) -> int:
    """Interactive .env generator (see wizard_parser help)."""
    out_path = Path(args.out).expanduser()
    print("=== Creatio Simple Migration Toolkit — Wizard Konfigurasi ===")
    print("(jawaban di dalam kurung siku adalah default; Enter untuk pakai)")
    print()

    # ---------------------------------------------------------- base URL
    while True:
        raw = _ask("Creatio base URL (mis. https://mycreatio.com)")
        try:
            root, _ = normalize_base_url(raw)
            break
        except ConfigError as exc:
            print(f"  {exc}")
    print()

    # ------------------------------------------------------- odata path
    print("Runtime Creatio Anda?")
    print("  Catatan: wizard memverifikasi path ini; bila tidak menjawab JSON OData, path")
    print("  alternatif dicoba otomatis. Path kustom (mis. /1/odata) diisi manual di .env.")
    _, odata_path, _ = _ask_choice(
        "Pilih runtime",
        [
            (
                "Creatio .NET Framework (klasik) -> /0/odata",
                "/0/odata",
                "Instance Creatio lama/on-premise. URL OData memakai prefix workspace /0, "
                "mis. https://host/0/odata. Paling umum.",
            ),
            (
                "Creatio .NET 8 (site baru) -> /odata",
                "/odata",
                "Instance dengan runtime .NET 8. URL OData tanpa prefix, mis. "
                "https://host/odata.",
            ),
        ],
        default_index=0,
    )
    print()

    # ------------------------------------------------------- auth mode
    print("Metode autentikasi?")
    auth_key, auth_value, _ = _ask_choice(
        "Pilih auth",
        [
            (
                "OAuth 2.0 (client credentials)",
                "oauth2",
                "Direkomendasikan Creatio untuk integrasi server-ke-server. Butuh client ID, "
                "client secret, dan identity service URL (system setting "
                "OAuth20IdentityServerUrl).",
            ),
            (
                "Forms auth (username & password)",
                "forms",
                "Login memakai akun Creatio, lalu cookie BPMCSRF dikirim di setiap request. "
                "Sederhana, tetapi memakai kredensial user.",
            ),
            (
                "Basic auth (username & password)",
                "basic",
                "HTTP Basic di setiap request. Hanya bekerja bila perlindungan CSRF dimatikan "
                "di server (UseCsrfToken=false). Untuk development, bukan produksi.",
            ),
        ],
        default_index=0,
    )
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    identity_url: str | None = None
    creds_verified = False

    if auth_value in {"forms", "basic"}:
        username = _ask("Username")
        password = _ask_secret("Password")
        if not username or not password:
            raise ConfigError("Username dan password wajib diisi untuk forms/basic")
    else:
        client_id = _ask("Client ID")
        client_secret = _ask_secret("Client secret")
        if not client_id or not client_secret:
            raise ConfigError("Client ID dan client secret wajib diisi untuk oauth2")
        identity_default = default_identity_url(root)
        print("  Identity service URL = system setting OAuth20IdentityServerUrl")
        print("  (token endpoint: {identity-url}/connect/token)")
        if identity_default:
            print(f"  Default untuk site cloud Anda: {identity_default}")
        else:
            print("  Domain ini bukan *.creatio.com, jadi tidak ada default. Isi manual.")
        while True:
            raw_identity = _ask("Identity service URL", default=identity_default)
            if not re.match(r"^https?://", raw_identity, flags=re.IGNORECASE):
                print("  Masukkan URL absolut, mis. https://pegadaian-is.creatio.com")
                continue
            identity_url = raw_identity.rstrip("/")
            status, note = _probe_identity_endpoint(
                identity_url, client_id=client_id, client_secret=client_secret
            )
            if status == "token":
                print(f"  OK: {note}")
                creds_verified = True
                break
            if status == "host":
                print(f"  Host benar, kredensial belum: {note}")
                print("  Periksa/regenerate client secret di Creatio (OAuth 2.0 integration).")
                creds_verified = False
                break
            print(f"  PERINGATAN: {note}")
            retry = _ask_choice(
                "Gunakan URL ini tetap?",
                [
                    (
                        "Pakai URL ini",
                        "keep",
                        "Config disimpan apa adanya; perbaiki nanti di .env bila perlu.",
                    ),
                    (
                        "Masukkan URL lain",
                        "retry",
                        "Ulangi pertanyaan identity service URL dengan nilai lain.",
                    ),
                ],
                default_index=1,
            )[1]
            if retry == "keep":
                creds_verified = False
                break
    print()

    # ---------------------------------------------------------- entity
    entity = _ask("Entity set default (mis. Contact; Enter untuk lewati)") or ""
    verify = _ask_choice(
        "Verifikasi sertifikat TLS?",
        [
            (
                "Tidak (menerima sertifikat apa pun)",
                "false",
                "Koneksi tetap terenkripsi, tetapi identitas server tidak diperiksa. "
                "Berguna bila instance memakai sertifikat internal/self-signed.",
            ),
            (
                "Ya (memeriksa sertifikat)",
                "true",
                "Lebih aman dan disarankan untuk produksi; gagal bila sertifikat tidak valid "
                "atau hostname tidak cocok.",
            ),
        ],
        default_index=0,
    )[1]
    print()

    # Verify the OData path with the credentials just entered. Creatio answers a
    # wrong path with a login redirect (HTML), which used to look like a JSON error.
    alternate = "/0/odata" if odata_path != "/0/odata" else "/odata"
    status, note = _probe_odata_path(
        root, odata_path,
        auth_mode=auth_value, identity_url=identity_url, client_id=client_id,
        client_secret=client_secret, username=username, password=password,
        verify_ssl=verify == "true",
    )
    if status == "ok":
        print(f"  OK: path OData {note}")
    elif status == "bad_path":
        alt_status, alt_note = _probe_odata_path(
            root, alternate,
            auth_mode=auth_value, identity_url=identity_url, client_id=client_id,
            client_secret=client_secret, username=username, password=password,
            verify_ssl=verify == "true",
        )
        if alt_status == "ok":
            print(f"  Path {odata_path} tidak valid ({note}); memakai {alternate} ({alt_note})")
            odata_path = alternate
        else:
            print(f"  PERINGATAN: path {odata_path} tidak menjawab OData ({note})")
            print(f"  Path {alternate} juga tidak: {alt_note}")
    elif status == "permission":
        print(f"  Path OData benar, tetapi akses ditolak: {note}")
        print("  (config tetap disimpan; izin bisa diperbaiki di Creatio)")
    else:
        print(f"  PERINGATAN: cek path OData gagal: {note}")

    if verify != "true":
        _quiet_insecure_warnings()
        print("  Catatan: verifikasi TLS dimatikan (CREATIO_VERIFY_SSL=false). "
              "Aktifkan untuk produksi.")

    settings = Settings(
        base_url=root,
        odata_path=odata_path,
        auth_mode=auth_value,
        username=username,
        password=password,
        client_id=client_id,
        client_secret=client_secret,
        identity_url=identity_url,
        entity=entity,
        verify_ssl=verify == "true",
    )

    # --------------------------------------------------- test connection
    test_now = _ask_choice(
        "Test koneksi sekarang?",
        [
            (
                "Ya, test sekarang",
                "yes",
                "Memanggil autentikasi + service document + satu record entity. "
                "Tidak menulis apa pun.",
            ),
            (
                "Tidak, hanya simpan config",
                "no",
                "Lewati test. Jalankan 'csmt check' kapan saja setelah ini.",
            ),
        ],
        default_index=0,
    )[1]
    if test_now == "yes":
        try:
            client = ODataClient(settings)
            try:
                info = client.ping()
            finally:
                client.close()
            print(f"  OK: {info['odata_url']} | auth={info['auth_mode']} | "
                  f"entity_reachable={info.get('entity_reachable')}")
        except CreatioError as exc:
            print(f"  TIDAK BERHASIL: {exc}")
            print("  (config tetap bisa disimpan dan diperbaiki nanti)")
    print()

    # --------------------------------------------------------- write .env
    if out_path.exists():
        replace = _ask_choice(
            f"{out_path} sudah ada. Timpa?",
            [
                (
                    "Timpa",
                    "yes",
                    f"File lama dipindahkan ke {out_path}.bak lebih dulu.",
                ),
                ("Batal", "no", "Tidak ada file yang ditulis."),
            ],
            default_index=0,
        )[1]
        if replace == "no":
            print("Batal. Tidak ada file yang ditulis.")
            return 2
        out_path.rename(out_path.with_suffix(out_path.suffix + ".bak"))

    lines = [
        "# Dibuat oleh: csmt wizard",
        "",
        "CREATIO_BASE_URL=" + root,
        "CREATIO_ODATA_PATH=" + odata_path,
        "CREATIO_AUTH_MODE=" + auth_value,
    ]
    if auth_value in {"forms", "basic"}:
        lines += ["CREATIO_USERNAME=" + (username or ""), "CREATIO_PASSWORD=" + (password or "")]
    if auth_value == "oauth2":
        lines += [
            "CREATIO_CLIENT_ID=" + (client_id or ""),
            "CREATIO_CLIENT_SECRET=" + (client_secret or ""),
        ]
        lines.append("CREATIO_IDENTITY_URL=" + (identity_url or ""))
    lines += [
        "CREATIO_ENTITY=" + entity,
        "CREATIO_VERIFY_SSL=" + verify,
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        out_path.chmod(0o600)  # kredensial: hanya owner yang boleh baca
    except OSError:  # pragma: no cover - some filesystems do not support chmod
        pass

    print(f"Config tersimpan: {out_path}")
    print(f"Lanjutkan dengan: uv run csmt check --env-file {out_path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    entity = (args.entity or settings.entity or "").strip()

    out_path = (
        Path(args.out).expanduser()
        if args.out
        else Path.cwd() / f"{entity}-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    )
    options = ExportOptions(
        entity=entity,
        out=out_path,
        select=tuple(part.strip() for part in (args.select or "").split(",") if part.strip()),
        filter=args.filter,
        orderby=args.orderby,
        limit=args.limit,
        page_size=args.page_size,
        delimiter=args.delimiter,
        encoding=args.encoding,
        write_header=not args.no_header,
        format=args.format,
        sheet_name=args.sheet_name,
        lookups=[LookupExpansion.parse(raw) for raw in args.expand_lookups],
    )

    client = ODataClient(settings)
    try:
        result = Exporter(client, options).run()
    finally:
        client.close()

    print("", file=sys.stdout)
    print("Export summary", file=sys.stdout)
    print("--------------", file=sys.stdout)
    for key, value in result.items():
        print(f"  {key:>10}: {value}", file=sys.stdout)
    return 0


COMMANDS = {
    "import": cmd_import,
    "check": cmd_check,
    "schema": cmd_schema,
    "init-mapping": cmd_init_mapping,
    "export": cmd_export,
    "wizard": cmd_wizard,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args)

    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces the choices
        parser.error(f"Unknown command {args.command!r}")
        return 2
    try:
        return handler(args)
    except CreatioError as exc:
        _LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        _LOG.error("Interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
