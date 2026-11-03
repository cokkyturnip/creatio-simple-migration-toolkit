"""Authentication strategies for Creatio.

Creatio accepts four kinds of authentication. Three are implemented here:

* ``forms``  - ``AuthService.svc/Login`` returns ``.ASPXAUTH`` + ``BPMCSRF``
  cookies. Every subsequent request must send ``BPMCSRF`` as a header.
* ``basic``  - HTTP Basic. Works when CSRF protection is disabled server-side
  (``UseCsrfToken=false``); no CSRF header is needed.
* ``oauth2`` - client-credentials grant against the identity service
  (``{IdentityServiceURL}/connect/token``). Every request carries
  ``Authorization: Bearer <token>``; when the token expires Creatio answers
  401 and the client re-authenticates once.

``AuthManager`` wraps one strategy, caches the credentials, and exposes the
extra headers to the HTTP client.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import requests

from .config import Settings
from .errors import AuthError

_LOG = logging.getLogger(__name__)


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


class _NotIdentityServer(Exception):
    """The host answered, but not like an OAuth token endpoint."""


class AuthStrategy(ABC):
    """Contract for authentication providers."""

    name = "base"

    def __init__(self, settings: Settings, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.log = logger or _LOG

    @abstractmethod
    def authenticate(self, session: requests.Session) -> None:
        """Obtain credentials and store them on ``session`` / the strategy."""

    @abstractmethod
    def headers(self) -> dict[str, str]:
        """Headers that must be added to every request."""

    def invalidate(self) -> None:
        """Drop cached credentials so the next ``headers()`` re-authenticates."""


class BasicAuth(AuthStrategy):
    name = "basic"

    def __init__(self, settings: Settings, logger: logging.Logger | None = None) -> None:
        super().__init__(settings, logger)
        if not (settings.username and settings.password):
            raise AuthError("Basic authentication requires a username and password")

    def authenticate(self, session: requests.Session) -> None:
        session.auth = (self.settings.username or "", self.settings.password or "")

    def headers(self) -> dict[str, str]:
        # ``session.auth`` already sets the header; returning nothing keeps us
        # from duplicating it on every request.
        return {}


class FormsAuth(AuthStrategy):
    name = "forms"

    def __init__(self, settings: Settings, logger: logging.Logger | None = None) -> None:
        super().__init__(settings, logger)
        if not (settings.username and settings.password):
            raise AuthError("Forms authentication requires a username and password")
        self._csrf: str | None = None

    def authenticate(self, session: requests.Session) -> None:
        payload = {"UserName": self.settings.username, "UserPassword": self.settings.password}
        try:
            response = session.post(
                self.settings.login_url,
                json=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "ForceUseSession": "true",
                },
                timeout=self.settings.timeout,
                verify=self.settings.verify_ssl,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Login request failed: {exc}") from exc

        body = _safe_json(response)
        if response.status_code != 200:
            detail = body if body is not None else (response.text or "")[:500]
            raise AuthError(f"Login failed with HTTP {response.status_code}: {detail}")

        code = None
        message = ""
        if isinstance(body, dict):
            code = body.get("Code", body.get("code"))
            message = str(body.get("Message") or body.get("message") or "")
        if code not in (0, "0", None):
            raise AuthError(f"Login rejected (Code={code}): {message or 'invalid credentials'}")

        csrf = session.cookies.get("BPMCSRF") or response.cookies.get("BPMCSRF")
        if not csrf:
            # Some deployments return the token in the body instead of a cookie.
            if isinstance(body, dict):
                csrf = body.get("BPMCSRF") or body.get("CsrfToken")
        if not csrf:
            raise AuthError(
                "Login succeeded but no BPMCSRF token was returned; "
                "verify that CSRF protection is enabled on the Creatio site."
            )

        self._csrf = csrf
        self.log.debug("Forms authentication succeeded for user %s", self.settings.username)

    def headers(self) -> dict[str, str]:
        if not self._csrf:
            raise AuthError("Not authenticated; call authenticate() first")
        return {"BPMCSRF": self._csrf}

    def invalidate(self) -> None:
        self._csrf = None


class OAuth2Auth(AuthStrategy):
    name = "oauth2"

    def __init__(self, settings: Settings, logger: logging.Logger | None = None) -> None:
        super().__init__(settings, logger)
        if not (settings.client_id and settings.client_secret):
            raise AuthError("OAuth 2.0 requires a client id and client secret")
        self._token: str | None = None
        self._endpoint_used: str | None = None

    def authenticate(self, session: requests.Session) -> None:
        """Obtain a token, probing candidate identity hosts when auto-derived.

        A host that is not an identity server (DNS fallback, TLS mismatch, 404)
        must not be reported as "wrong credentials" — that sends people chasing the
        wrong problem. Only a real identity-server answer (400/invalid_client or
        invalid_grant) is treated as an authentication failure.
        """
        candidates = self.settings.identity_candidates
        if not candidates:
            raise AuthError(
                "OAuth 2.0 membutuhkan identity service URL (--identity-url / "
                "CREATIO_IDENTITY_URL), yaitu nilai system setting "
                "OAuth20IdentityServerUrl. Domain ini bukan *.creatio.com sehingga "
                "tidak ada nilai default."
            )

        problems: list[str] = []
        for candidate in candidates:
            endpoint = (
                candidate
                if candidate.lower().endswith("/connect/token")
                else f"{candidate.rstrip('/')}/connect/token"
            )
            self.log.debug("OAuth 2.0 token request to %s", endpoint)
            try:
                token = self._request_token(session, endpoint)
            except _NotIdentityServer as exc:
                problems.append(f"{endpoint}: {exc}")
                self.log.info(
                    "Identity endpoint %s is not an identity server (%s); trying next",
                    endpoint,
                    exc,
                )
                continue
            self._token = token
            self._endpoint_used = endpoint
            self.log.debug("OAuth 2.0 client-credentials token obtained from %s", endpoint)
            return

        raise AuthError(
            "OAuth 2.0 gagal: tidak ada identity service yang menjawab di "
            + ", ".join(candidates)
            + ". Nilai yang benar ada di system setting OAuth20IdentityServerUrl "
            "(Setup > System settings), biasanya https://<subdomain>-is.creatio.com. "
            "Set dengan --identity-url. Detail: " + " | ".join(problems)
        )

    def _request_token(self, session: requests.Session, endpoint: str) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
        }
        if self.settings.scope:
            data["scope"] = self.settings.scope

        try:
            response = session.post(
                endpoint,
                data=data,
                headers={"Accept": "application/json"},
                timeout=self.settings.timeout,
                verify=self.settings.verify_ssl,
            )
        except requests.exceptions.SSLError as exc:
            raise _NotIdentityServer(
                "TLS gagal (sertifikat tidak cocok untuk host ini). Biasanya berarti "
                "hostname identity service salah"
            ) from exc
        except requests.RequestException as exc:
            raise _NotIdentityServer(f"tidak bisa dihubungi: {exc}") from exc

        body = _safe_json(response)
        if response.status_code == 200 and isinstance(body, dict):
            token = body.get("access_token")
            if token:
                return str(token)
            raise AuthError(
                f"Respons token dari {endpoint} tidak memuat access_token: "
                f"{json.dumps(body)[:300]}"
            )

        if response.status_code in (400, 401) and isinstance(body, dict):
            # A real identity server answered: credentials or grant type are wrong.
            detail = body.get("error_description") or body.get("error") or (response.text or "")[:300]
            raise AuthError(
                f"Identity server {endpoint} menolak kredensial "
                f"(HTTP {response.status_code}): {detail}"
            )

        raise _NotIdentityServer(
            f"HTTP {response.status_code}, bukan respons token JSON"
        )

    def headers(self) -> dict[str, str]:
        if not self._token:
            raise AuthError("Not authenticated; call authenticate() first")
        return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self) -> None:
        self._token = None
        self._endpoint_used = None


class AuthManager:
    """Owns one strategy and guarantees it is initialised exactly once."""

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
        strategy: AuthStrategy | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.log = logger or _LOG
        self.strategy = strategy or self._build_strategy()
        self._authenticated = False

    def _build_strategy(self) -> AuthStrategy:
        mode = self.settings.resolved_auth_mode
        if mode == "basic":
            return BasicAuth(self.settings, self.log)
        if mode == "forms":
            return FormsAuth(self.settings, self.log)
        if mode == "oauth2":
            return OAuth2Auth(self.settings, self.log)
        raise AuthError(f"Unsupported auth mode: {mode}")

    @property
    def mode(self) -> str:
        return self.strategy.name

    @property
    def uses_csrf(self) -> bool:
        # Only Forms auth is tied to the BPMCSRF cookie. OAuth 2.0 bearer calls
        # re-authenticate on 401 (expired token), not on 403.
        return self.strategy.name == "forms"

    def ensure(self, force: bool = False) -> None:
        if force:
            self.strategy.invalidate()
            self._authenticated = False
        if self._authenticated:
            return
        self.strategy.authenticate(self.session)
        self._authenticated = True

    def headers(self) -> dict[str, str]:
        self.ensure()
        return self.strategy.headers()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass


__all__ = ["AuthManager", "AuthStrategy", "BasicAuth", "FormsAuth", "OAuth2Auth"]
