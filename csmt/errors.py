"""Exception hierarchy for the importer.

Every error raised on purpose by this package derives from :class:`CreatioError`
so callers can catch one type. Errors that carry an HTTP response keep the
status code and the server payload around for reporting.
"""

from __future__ import annotations

from typing import Any, Mapping


class CreatioError(Exception):
    """Base class for all importer errors."""


class ConfigError(CreatioError):
    """Invalid or missing configuration (CLI flag, env var, mapping file)."""


class AuthError(CreatioError):
    """Authentication against Creatio failed."""


class CsvError(CreatioError):
    """The CSV file is missing, unreadable or malformed."""


class MappingError(CreatioError):
    """The mapping definition is invalid or references unknown fields."""


class ValidationError(CreatioError):
    """A row or a mapping value did not pass validation."""

    def __init__(self, message: str, *, field: str | None = None, value: Any = None) -> None:
        self.field = field
        self.value = value
        if field:
            message = f"{field}: {message}"
        super().__init__(message)


class ODataError(CreatioError):
    """Creatio returned a non-success HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        payload: Any = None,
        method: str | None = None,
        url: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.status = status
        self.code = code
        self.payload = payload
        self.method = method
        self.url = url
        self.retryable = retryable
        self.detail = message
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        parts = []
        if self.method and self.url:
            parts.append(f"{self.method} {self.url}")
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.code:
            parts.append(f"code={self.code}")
        parts.append(self.detail)
        return " | ".join(str(part) for part in parts if part)

    @classmethod
    def from_response(
        cls, response: Any, *, method: str | None = None, url: str | None = None
    ) -> "ODataError":
        """Build an error from a ``requests.Response``.

        Creatio reports failures either with the OData envelope
        (``{"error": {"code": ..., "message": ...}}``) or with a plain
        ``{"Message": ...}`` object, so both shapes are handled.
        """
        status = getattr(response, "status_code", None)
        payload: Any = None
        code: str | None = None
        message: str | None = None

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                code = _stringify(error.get("code"))
                message = _stringify(error.get("message"))
                details = error.get("details")
                if not message and details:
                    message = _stringify(details)
            else:
                code = _stringify(payload.get("code") or payload.get("Code"))
                message = _stringify(payload.get("Message") or payload.get("message"))

        if not message:
            text = getattr(response, "text", "") or ""
            message = text.strip()[:2000] or getattr(response, "reason", "") or "request failed"

        retryable = status in {408, 425, 429} or (status or 0) >= 500
        return cls(
            message,
            status=status,
            code=code,
            payload=payload if payload is not None else getattr(response, "text", None),
            method=method,
            url=url,
            retryable=retryable,
        )


class BatchError(ODataError):
    """A ``$batch`` request failed or its response could not be parsed."""


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)
