"""CSV -> Creatio importer built on the OData 4 service.

Public entry points are re-exported here so the package can be used either as a
library or through ``python -m csmt``.
"""

from .config import Settings, load_env_file
from .errors import (
    AuthError,
    BatchError,
    ConfigError,
    CreatioError,
    CsvError,
    MappingError,
    ODataError,
    ValidationError,
)

__all__ = [
    "AuthError",
    "BatchError",
    "ConfigError",
    "CreatioError",
    "CsvError",
    "MappingError",
    "ODataError",
    "Settings",
    "ValidationError",
    "load_env_file",
]

__version__ = "1.0.1"
