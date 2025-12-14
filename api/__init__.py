"""
API Package - FastAPI wrapper for Schulportal Hessen.

Run locally:
    uvicorn api.api:app --reload
"""

from .api import app

__all__ = ["app"]
