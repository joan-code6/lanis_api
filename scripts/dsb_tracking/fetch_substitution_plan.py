"""
Daily Substitution Plan Fetcher.

Fetches the current substitution plan from DSBmobile and stores it in the database.
Runs on a schedule (daily at 1:00 PM) or can be run manually.

Usage:
    python scripts/fetch_substitution_plan.py
    python scripts/fetch_substitution_plan.py --school-id XYZ --username USER --password PASS
    python scripts/fetch_substitution_plan.py --run-once
"""

import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from functions.base import SchulportalHessenAPI
from functions.external.dsb.api import (
    dsb_login,
    dsb_get_plan_urls,
    dsb_get_substitution_plan,
)

from scripts.dsb_tracking.substitution_db import SubstitutionPlanDB

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("substitution_fetcher")


def parse_table_entries(
    tables: List[Dict[str, Any]], raw_html: str = ""
) -> List[Dict[str, Any]]:
    """
    Parse DSB substitution plan tables into structured entries.

    The tables contain entries for different dates, with dates in mon_title divs
    in the raw HTML (e.g., '30.4.2026 Donnerstag, Woche B (Seite 1 / 2)').
    """
    entries = []

    page_dates = _extract_dates_from_raw_html(raw_html)

    data_tables = [
        t for t in tables if t.get("headers") and "Klasse" in str(t.get("headers", []))
    ]

    table_idx = 0
    for table in tables:
        headers = table.get("headers", [])

        if not headers or "Klasse" not in str(headers):
            continue

        plan_date = (
            page_dates[table_idx] if table_idx < len(page_dates) else date.today()
        )
        table_idx += 1

        rows = table.get("rows", [])
        for row in rows:
            if isinstance(row, dict):
                entry = {
                    "plan_date": plan_date.isoformat(),
                    "period": row.get("Stunde", ""),
                    "klasse": row.get("Klasse(n)", row.get("Klasse", "")),
                    "original_lesson": row.get("Fach", ""),
                    "substitution_lesson": row.get("Vertreter", ""),
                    "teacher": row.get("Vertreter", ""),
                    "room": row.get("Raum", ""),
                    "info": row.get("Bemerkungen / Hinweise", row.get("Bemerkung", "")),
                    "status": _determine_status(row),
                }

                if entry["klasse"] or entry["period"]:
                    entries.append(entry)

    return entries


def _extract_dates_from_raw_html(raw_html: str) -> List[date]:
    """Extract dates from mon_title divs in raw HTML."""
    import re

    dates = []

    matches = re.findall(r'<div class="mon_title">([^<]+)</div>', raw_html)
    for title in matches:
        d = _extract_date_from_title(title)
        if d:
            dates.append(d)

    return dates


def _extract_date_from_title(title: str) -> Optional[date]:
    """Extract date from page title (e.g., '30.4.2026 Donnerstag, Woche B (Seite 1 / 2)')."""
    if not title:
        return None

    import re

    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", title)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            pass

    return None


def _extract_date_from_caption(caption: str) -> Optional[date]:
    """Extract date from table caption (e.g., 'Donnerstag, 30.04.2026')."""
    if not caption:
        return None

    caption = caption.strip()

    date_formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%A, %d.%m.%Y",
        "%A %d.%m.%Y",
    ]

    for fmt in date_formats:
        try:
            parsed = datetime.strptime(caption, fmt)
            return parsed.date()
        except ValueError:
            continue

    import re

    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", caption)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            pass

    return None


def _determine_status(row: Dict[str, Any]) -> str:
    """Determine entry status based on content."""
    info = row.get("Info", row.get("Bemerkung", "")).lower()

    if "entfall" in info or "fällt aus" in info:
        return "cancelled"
    elif "verlegt" in info or "vertauscht" in info:
        return "modified"

    return "active"


async def fetch_and_store(
    school_id: str,
    dsb_username: str,
    dsb_password: str,
    db: SubstitutionPlanDB,
    klasse: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch substitution plan from DSB and store in database."""
    api = SchulportalHessenAPI()

    logger.info(f"Logging in to DSB for school {school_id}")
    login_result = api.dsb_login(dsb_username, dsb_password)

    if not login_result.get("success"):
        logger.error(f"DSB login failed: {login_result.get('error')}")
        return {"success": False, "error": f"Login failed: {login_result.get('error')}"}

    logger.info("Fetching plan URLs")
    plan_result = api.dsb_get_plan_urls(dsb_username, dsb_password)

    if not plan_result.get("success"):
        logger.error(f"Failed to get plan URLs: {plan_result.get('error')}")
        return {
            "success": False,
            "error": f"Plan URLs failed: {plan_result.get('error')}",
        }

    plan_url = plan_result.get("html_plan_url")
    logger.info(f"Fetching substitution plan from: {plan_url}")

    plan_result = api.dsb_get_substitution_plan(
        dsb_username,
        dsb_password,
        plan_url=plan_url,
        klasse=klasse,
        include_raw=True,
    )

    if not plan_result.get("success"):
        logger.error(f"Failed to get substitution plan: {plan_result.get('error')}")
        return {
            "success": False,
            "error": f"Plan fetch failed: {plan_result.get('error')}",
        }

    tables = plan_result.get("tables", [])
    raw_html = plan_result.get("raw_html", "")
    logger.info(f"Found {len(tables)} tables in the plan")

    entries = parse_table_entries(tables, raw_html)
    logger.info(f"Parsed {len(entries)} entries from tables")

    if not entries:
        for i, table in enumerate(tables[:3]):
            logger.debug(
                f"Table {i}: caption='{table.get('caption')}', headers={table.get('headers')}, rows sample: {table.get('rows', [])[:2]}"
            )

    if entries:
        new_count, updated_count, unchanged_count = await db.upsert_entries(
            school_id, entries, source_url=plan_url
        )
        logger.info(
            f"Stored: {new_count} new, {updated_count} updated, {unchanged_count} unchanged"
        )

        date_range = _get_date_range_from_entries(entries)
        return {
            "success": True,
            "entry_count": len(entries),
            "new_entries": new_count,
            "updated_entries": updated_count,
            "date_range": date_range,
        }
    else:
        logger.warning("No entries found in the plan")
        return {"success": True, "entry_count": 0, "message": "No entries found"}


def _get_date_range_from_entries(entries: List[Dict[str, Any]]) -> Dict[str, str]:
    """Get the date range covered by the entries."""
    dates = []
    for entry in entries:
        plan_date = entry.get("plan_date")
        if plan_date:
            try:
                dates.append(date.fromisoformat(plan_date))
            except ValueError:
                pass

    if dates:
        return {
            "start": min(dates).isoformat(),
            "end": max(dates).isoformat(),
        }
    return {}


async def run_scheduler(
    school_id: str,
    dsb_username: str,
    dsb_password: str,
    hour: int = 13,
    minute: int = 0,
    klasse: Optional[str] = None,
):
    """Run the scheduler that fetches the plan daily at specified time."""
    import schedule
    import time

    db = SubstitutionPlanDB()
    await db.initialize()

    target_time = f"{hour:02d}:{minute:02d}"

    def job():
        logger.info(f"Running scheduled fetch at {target_time}")
        return asyncio.run(
            fetch_and_store(school_id, dsb_username, dsb_password, db, klasse)
        )

    schedule.every().day.at(target_time).do(job)

    logger.info(f"Scheduler started. Will fetch daily at {target_time}")
    logger.info("Press Ctrl+C to stop")

    while True:
        schedule.run_pending()
        time.sleep(60)


async def main():
    parser = argparse.ArgumentParser(
        description="Fetch and store DSB substitution plan"
    )
    parser.add_argument(
        "--school-id", help="School ID", default=os.getenv("DSB_SCHOOL_ID", "")
    )
    parser.add_argument(
        "--username", help="DSB username", default=os.getenv("DSB_USERNAME", "")
    )
    parser.add_argument(
        "--password", help="DSB password", default=os.getenv("DSB_PASSWORD", "")
    )
    parser.add_argument("--klasse", help="Filter by class (e.g., '10C')", default=None)
    parser.add_argument(
        "--run-once", action="store_true", help="Run once and exit (don't schedule)"
    )
    parser.add_argument(
        "--hour", type=int, default=13, help="Hour to run scheduler (0-23)"
    )
    parser.add_argument(
        "--minute", type=int, default=0, help="Minute to run scheduler (0-59)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.school_id or not args.username or not args.password:
        parser.error(
            "Either provide --school-id, --username, --password or set DSB_* environment variables"
        )

    db = SubstitutionPlanDB()
    await db.initialize()

    if args.run_once:
        logger.info("Running single fetch")
        result = await fetch_and_store(
            args.school_id, args.username, args.password, db, args.klasse
        )
        print(f"\nResult: {result}")

        if result.get("success"):
            stats = await db.get_stats()
            print(f"Database stats: {stats}")
    else:
        await run_scheduler(
            args.school_id,
            args.username,
            args.password,
            args.hour,
            args.minute,
            args.klasse,
        )


if __name__ == "__main__":
    asyncio.run(main())
