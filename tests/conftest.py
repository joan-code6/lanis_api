import os

# The application intentionally refuses to start with an ephemeral JWT key.
# Tests use a fixed, non-production secret so importing the FastAPI app remains
# deterministic without weakening the runtime configuration check.
os.environ.setdefault("JWT_SECRET", "lanis-api-test-secret-not-for-production")
