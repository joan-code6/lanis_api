"""Select local SQLite persistence or Appwrite TablesDB at runtime."""

import os

if os.getenv("LANIS_APPWRITE_NATIVE", "").lower() in {"1", "true", "yes"}:
    from .appwrite_state import *  # noqa: F403
else:
    from .auth_db import *  # noqa: F403
