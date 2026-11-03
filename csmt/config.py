"""Configuration: CLI flags, environment variables and ``.env`` files.

The importer deliberately keeps configuration in one dataclass so that the
client, the pipeline and the CLI all read the same values. Credentials may come
from the environment; they are never logged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ConfigError

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_AUTH_MODES = {"auto", "basic", "forms", "oauth2"}
_ODATA_SUFFIXES = ("/0/odata", "/odata")


def _as_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean, got {value!r}")


def _as_float(value: Any, *, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _as_int(value: Any, *, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


CREATIO_CLOUD_SUFFIX = ".creatio.com"


def identity_url_candidates(base_url: str) -> list[str]:
    """Candidate identity-service URLs for a Creatio cloud site, best first.

    Creatio cloud instances use ``<subdomain>-is.creatio.com`` (hyphen). Some
    tenants are published as ``<subdomain>_is.creatio.com`` (underscore), which is
    why both are returned: the client probes them in order and keeps the one that
    actually answers like an identity server.

    Returns an empty list for custom domains, where nothing can be derived and the
    value must come from the ``OAuth20IdentityServerUrl`` system setting.
    """
    text = str(base_url or "").strip()
    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        text = "https://" + text
    host = (urlparse(text).hostname or "").lower()
    if not host.endswith(CREATIO_CLOUD_SUFFIX):
        return []
    subdomain = host[: -len(CREATIO_CLOUD_SUFFIX)]
    if not subdomain or "." in subdomain:
        return []
    return [
        f"https://{subdomain}-is{CREATIO_CLOUD_SUFFIX}",
        f"https://{subdomain}_is{CREATIO_CLOUD_SUFFIX}",
    ]


def default_identity_url(base_url: str) -> str | None:
    """Primary identity-service guess for Creatio cloud sites (see candidates)."""
    candidates = identity_url_candidates(base_url)
    return candidates[0] if candidates else None


def normalize_base_url(raw: str, odata_path: str = "/0/odata") -> tuple[str, str]:
    """Split a user supplied URL into ``(root_url, odata_path)``.

    Accepts ``https://host``, ``https://host/``, ``https://host/0``,
    ``https://host/0/odata`` and ``https://host/Creatio/0/odata``. When the user
    pastes the full OData URL the path is extracted so callers cannot end up
    requesting ``.../0/odata/0/odata``.
    """
    if not raw or not str(raw).strip():
        raise ConfigError("Creatio base URL is required (--base-url or CREATIO_BASE_URL)")

    text = str(raw).strip().rstrip("/")
    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        text = "https://" + text

    parsed = urlparse(text)
    if not parsed.netloc:
        raise ConfigError(f"Invalid Creatio base URL: {raw!r}")
    # urlparse silently accepts nonsense hosts ("not a url") as long as there is
    # no slash; a real host allows only letters, digits, dots, dashes and an
    # optional port.
    if not re.match(r"^[A-Za-z0-9.[\]:\-]+(:\d+)?$", parsed.netloc):
        raise ConfigError(f"Invalid Creatio base URL host: {raw!r}")

    path = parsed.path.rstrip("/")
    resolved_odata = (odata_path or "/0/odata").strip()
    if not resolved_odata.startswith("/"):
        resolved_odata = "/" + resolved_odata

    for suffix in _ODATA_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            resolved_odata = suffix
            break

    root = f"{parsed.scheme}://{parsed.netloc}{path}"
    return root.rstrip("/"), resolved_odata


@dataclass
class Settings:
    """Everything needed to talk to a Creatio instance."""

    base_url: str = ""
    odata_path: str = "/0/odata"
    entity: str = ""
    auth_mode: str = "auto"

    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    # Authorization server URL for OAuth 2.0 = system setting OAuth20IdentityServerUrl.
    identity_url: str | None = None
    scope: str | None = None

    verify_ssl: bool = True
    timeout: float = 60.0
    max_retries: int = 5
    retry_backoff: float = 1.0
    retry_max_delay: float = 30.0
    user_agent: str = "csmter/1.0"

    # Runtime knob that is also handy for tests.
    page_size: int = 500

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ConfigError("Creatio base URL is required (--base-url or CREATIO_BASE_URL)")
        self.root_url, self.odata_path = normalize_base_url(self.base_url, self.odata_path)
        self.auth_mode = (self.auth_mode or "auto").strip().lower()
        if self.auth_mode not in _AUTH_MODES:
            raise ConfigError(
                f"Unknown auth mode {self.auth_mode!r}; expected one of {sorted(_AUTH_MODES)}"
            )
        self.timeout = float(self.timeout)
        if self.timeout <= 0:
            raise ConfigError("timeout must be greater than zero")
        self.max_retries = int(self.max_retries)
        if self.max_retries < 0:
            raise ConfigError("max_retries must be zero or greater")
        self.retry_backoff = float(self.retry_backoff)
        self.retry_max_delay = float(self.retry_max_delay)
        self.page_size = max(1, min(int(self.page_size), 20000))
        self.entity = (self.entity or "").strip()

    # -- derived URLs -----------------------------------------------------
    @property
    def odata_url(self) -> str:
        return f"{self.root_url}{self.odata_path}"

    @property
    def login_url(self) -> str:
        return f"{self.root_url}/ServiceModel/AuthService.svc/Login"

    @property
    def identity_candidates(self) -> list[str]:
        """Identity service URLs to try, in order.

        An explicit ``identity_url`` is authoritative (single candidate). Otherwise
        the cloud naming variants are probed, so a wrong guess fails loudly instead
        of silently pointing at a host that does not exist.
        """
        explicit = (self.identity_url or "").strip().rstrip("/")
        derived = identity_url_candidates(self.root_url)
        if explicit:
            # The configured host is tried first and always wins when it answers
            # like an identity server. The derived variants are kept as fallbacks
            # only, so a stale .env value pointing at a dead hostname (the common
            # _is vs -is mistake) self-heals instead of blocking the import.
            return [explicit] + [candidate for candidate in derived if candidate != explicit]
        return derived

    @property
    def resolved_identity_url(self) -> str | None:
        """Explicit identity URL, else the cloud default derived from the base URL."""
        candidates = self.identity_candidates
        return candidates[0] if candidates else None

    @property
    def token_endpoint(self) -> str:
        """OAuth 2.0 token endpoint: ``{IdentityServiceURL}/connect/token``.

        The identity service is NOT necessarily the Creatio site URL. Creatio
        stores its address in the ``OAuth20IdentityServerUrl`` system setting.
        For cloud sites the default is ``https://<subdomain>_is.creatio.com``;
        other deployments must set it explicitly.
        """
        identity = self.resolved_identity_url
        if not identity:
            raise ConfigError(
                "OAuth 2.0 membutuhkan identity service URL (--identity-url / "
                "CREATIO_IDENTITY_URL), yaitu nilai system setting OAuth20IdentityServerUrl. "
                "Untuk domain non-cloud tidak ada default."
            )
        base = identity.strip().rstrip("/")
        if not re.match(r"^https://|^http://", base, flags=re.IGNORECASE):
            raise ConfigError(f"Identity service URL harus absolut (http/https): {identity!r}")
        if base.lower().endswith("/connect/token"):
            return base
        return f"{base}/connect/token"

    @property
    def resolved_auth_mode(self) -> str:
        """Resolve ``auto`` to a concrete strategy and validate its credentials."""
        mode = self.auth_mode
        if mode == "auto":
            if self.client_id and self.client_secret:
                mode = "oauth2"
            elif self.username and self.password:
                mode = "forms"
            else:
                raise ConfigError(
                    "Cannot pick an authentication method: provide username/password "
                    "(Basic or Forms) or client-id/client-secret (OAuth 2.0), or set "
                    "--auth-mode explicitly."
                )

        if mode in {"basic", "forms"} and not (self.username and self.password):
            raise ConfigError(f"auth mode {mode!r} requires --username and --password")
        if mode == "oauth2" and not (self.client_id and self.client_secret):
            raise ConfigError("auth mode 'oauth2' requires --client-id and --client-secret")
        if mode == "oauth2" and not self.resolved_identity_url:
            raise ConfigError(
                "auth mode 'oauth2' requires --identity-url (OAuth20IdentityServerUrl); "
                "no default exists for non-cloud domains"
            )
        return mode

    def with_overrides(self, **overrides: Any) -> "Settings":
        """Return a copy with non-``None`` overrides applied."""
        data = {field_name: getattr(self, field_name) for field_name in self.__dataclass_fields__}
        for key, value in overrides.items():
            if value is not None and key in data:
                data[key] = value
        return Settings(**data)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> "Settings":
        """Build settings from ``CREATIO_*`` environment variables.

        Explicit keyword arguments win over environment values.
        """
        source = os.environ if env is None else env

        def get(*names: str) -> str | None:
            for name in names:
                value = source.get(name)
                if value is not None and str(value).strip() != "":
                    return str(value).strip()
            return None

        def pick(key: str, env_names: tuple[str, ...], caster=None) -> Any:
            if overrides.get(key) is not None:
                return overrides[key]
            raw = get(*env_names)
            if raw is None:
                return None
            return caster(raw) if caster else raw

        data: dict[str, Any] = {
            "base_url": pick("base_url", ("CREATIO_BASE_URL", "CREATIO_URL")),
            "odata_path": pick("odata_path", ("CREATIO_ODATA_PATH",)) or "/0/odata",
            "entity": pick("entity", ("CREATIO_ENTITY",)) or "",
            "auth_mode": pick("auth_mode", ("CREATIO_AUTH_MODE",)) or "auto",
            "username": pick("username", ("CREATIO_USERNAME", "CREATIO_USER")),
            "password": pick("password", ("CREATIO_PASSWORD", "CREATIO_PASS")),
            "client_id": pick("client_id", ("CREATIO_CLIENT_ID",)),
            "client_secret": pick("client_secret", ("CREATIO_CLIENT_SECRET",)),
            "identity_url": pick("identity_url", ("CREATIO_IDENTITY_URL",)),
            "scope": pick("scope", ("CREATIO_SCOPE",)),
            "verify_ssl": pick(
                "verify_ssl", ("CREATIO_VERIFY_SSL",), lambda value: _as_bool(value, name="CREATIO_VERIFY_SSL")
            ),
            "timeout": pick("timeout", ("CREATIO_TIMEOUT",), lambda value: _as_float(value, name="CREATIO_TIMEOUT")),
            "max_retries": pick(
                "max_retries", ("CREATIO_MAX_RETRIES",), lambda value: _as_int(value, name="CREATIO_MAX_RETRIES")
            ),
            "retry_backoff": pick(
                "retry_backoff", ("CREATIO_RETRY_BACKOFF",), lambda value: _as_float(value, name="CREATIO_RETRY_BACKOFF")
            ),
            "retry_max_delay": pick(
                "retry_max_delay",
                ("CREATIO_RETRY_MAX_DELAY",),
                lambda value: _as_float(value, name="CREATIO_RETRY_MAX_DELAY"),
            ),
            "page_size": pick("page_size", ("CREATIO_PAGE_SIZE",), lambda value: _as_int(value, name="CREATIO_PAGE_SIZE")),
        }
        data = {key: value for key, value in data.items() if value is not None}
        data.setdefault("odata_path", "/0/odata")
        data.setdefault("entity", "")
        data.setdefault("auth_mode", "auto")
        for key, value in overrides.items():
            if value is not None and key in Settings.__dataclass_fields__:
                data[key] = value
        return cls(**data)


def load_env_file(path: str | Path, *, override: bool = False) -> dict[str, str]:
    """Minimal ``.env`` reader (no external dependency).

    Supports ``KEY=VALUE``, ``export KEY=VALUE``, ``#`` comments, blank lines and
    single/double quoted values. Values are exported to ``os.environ``.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise ConfigError(f"Env file not found: {file_path}")

    loaded: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded
