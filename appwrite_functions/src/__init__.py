"""Self-contained Appwrite backend used by the LANiS Function handlers."""

from .backend import (
    AppwriteBackend,
    BackendSettings,
    SettingsError,
    get_backend,
    reset_backend,
)

__all__ = ["AppwriteBackend", "BackendSettings", "SettingsError", "get_backend", "reset_backend"]
