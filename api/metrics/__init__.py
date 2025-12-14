"""
API Metrics Package - User Data Collection and Storage.

Provides SQLite-based storage for user metrics collected from the Schulportal.
"""

from .user_metrics_db import user_metrics_db, UserMetricsDB, UserRecord

__all__ = ["user_metrics_db", "UserMetricsDB", "UserRecord"]
