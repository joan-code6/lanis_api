"""Helpers for deriving stable internal identities from SPH credentials."""


def normalize_school_id(school_id: str) -> str:
    """Return the canonical representation used for a school identifier."""
    return str(school_id).strip()


def normalize_username(username: str) -> str:
    """Return the case-insensitive representation used for account identity.

    The original spelling is still available to the SPH login client. This
    value is only for LANIS-owned keys, caches, and metrics records.
    """
    return str(username).strip().casefold()


def make_user_id(school_id: str, username: str) -> str:
    """Build a stable LANIS user ID for one SPH account."""
    return f"{normalize_school_id(school_id)}:{normalize_username(username)}"


def canonicalize_user_id(user_id: str) -> str:
    """Canonicalize a LANIS ``school_id:username`` ID.

    A few tests and older callers use opaque IDs such as ``user-a``. Those IDs
    are intentionally left untouched because they are not credential-derived
    identities.
    """
    value = str(user_id)
    school_id, separator, username = value.partition(":")
    if not separator:
        return value
    return make_user_id(school_id, username)
