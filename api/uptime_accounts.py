"""Encrypt an explicitly designated monitor pool from stdin into a private file.

Usage: python -m api.uptime_accounts /absolute/private/accounts.json < input.json
The plaintext input is never printed. The same credential key must be available
when provisioning and when the monitor runs. Existing output is never overwritten.
"""

import json
import os
import sys
from pathlib import Path

from .auth_db import _encrypt_password


def normalize_monitor_identity(value: object) -> str:
    """Accept textual/integer identifiers, never stringify missing or compound data."""
    if type(value) not in (str, int):
        raise ValueError("Invalid monitor identity")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("Missing monitor identity")
    return normalized


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m api.uptime_accounts OUTPUT < INPUT")
    try:
        entries = json.load(sys.stdin)
        if not isinstance(entries, list) or not 2 <= len(entries) <= 100:
            raise ValueError
        accounts = []
        identities = set()
        for entry in entries:
            school = normalize_monitor_identity(entry["school_id"])
            username = normalize_monitor_identity(entry["username"])
            password = entry["password"]
            identity = (school, username.casefold())
            if (
                not school
                or not username
                or not isinstance(password, str)
                or not password
                or identity in identities
            ):
                raise ValueError
            identities.add(identity)
            accounts.append(
                {
                    "school_id": school,
                    "username": username,
                    "password": _encrypt_password(password),
                    "enabled": True,
                }
            )
        path = Path(sys.argv[1])
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(accounts, output)
            output.write("\n")
    except Exception:  # noqa: BLE001 - hide secret-bearing validation errors
        raise SystemExit(
            "Could not provision monitor pool; check input, key, and output path."
        ) from None


if __name__ == "__main__":
    main()
