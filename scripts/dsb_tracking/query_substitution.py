"""
Query and display substitution plan data from the database.

Usage:
    python scripts/query_substitution.py
    python scripts/query_substitution.py --school-id XYZ --date 2026-04-30
    python scripts/query_substitution.py --school-id XYZ --conflicts
"""

import asyncio
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.dsb_tracking.substitution_db import SubstitutionPlanDB


async def query_entries(
    db: SubstitutionPlanDB,
    school_id: str,
    target_date: Optional[date] = None,
    days: int = 7,
    show_conflicts: bool = False,
    json_output: bool = False,
):
    """Query and display substitution entries."""
    await db.initialize()

    if show_conflicts and target_date:
        conflicts = await db.get_conflicts(school_id, target_date)
        if json_output:
            print(json.dumps({"conflicts": conflicts}, indent=2, default=str))
        else:
            if not conflicts:
                print(f"No conflicts found for {target_date}")
            else:
                print(f"\n=== Conflicts for {target_date} ===\n")
                for conflict in conflicts:
                    print(f"Period: {conflict['period']}, Class: {conflict['klasse']}")
                    print(f"  Versions: {conflict['version_count']}")
                    for v in conflict["versions"]:
                        print(
                            f"    v{v['version']}: {v['substitution_lesson']} (from {v['source_fetch_date']})"
                        )
                    print()
        return

    if target_date:
        entries = await db.get_entries_for_date(school_id, target_date)
        if json_output:
            print(
                json.dumps(
                    {
                        "entries": [e.to_dict() for e in entries],
                        "date": target_date.isoformat(),
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"\n=== Substitution Plan for {target_date} ===\n")
            if not entries:
                print("No entries found")
            else:
                for entry in entries:
                    print(f"Period: {entry.period}, Class: {entry.klasse}")
                    print(f"  {entry.original_lesson} -> {entry.substitution_lesson}")
                    if entry.teacher:
                        print(f"  Teacher: {entry.teacher}")
                    if entry.room:
                        print(f"  Room: {entry.room}")
                    if entry.info:
                        print(f"  Info: {entry.info}")
                    if entry.version > 1:
                        print(
                            f"  [Version {entry.version}, last updated: {entry.updated_at}]"
                        )
                    print()
    else:
        start_date = date.today()
        end_date = start_date + timedelta(days=days)
        entries = await db.get_entries_for_date_range(school_id, start_date, end_date)

        if json_output:
            print(
                json.dumps(
                    {
                        "entries": [e.to_dict() for e in entries],
                        "range": f"{start_date} to {end_date}",
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"\n=== Substitution Plan ({start_date} to {end_date}) ===\n")
            if not entries:
                print("No entries found")
            else:
                current_date = None
                for entry in sorted(entries, key=lambda e: (e.plan_date, e.period)):
                    if entry.plan_date != current_date:
                        current_date = entry.plan_date
                        print(f"\n--- {current_date.strftime('%A, %d.%m.%Y')} ---")

                    status_marker = "[*]" if entry.version > 1 else "[+]"
                    print(
                        f"{status_marker} Period {entry.period}: {entry.klasse} | {entry.original_lesson} -> {entry.substitution_lesson}"
                    )

            print(f"\nTotal: {len(entries)} entries")


async def show_stats(db: SubstitutionPlanDB):
    """Show database statistics."""
    await db.initialize()
    stats = await db.get_stats()

    print("\n=== Database Statistics ===\n")
    print(f"Total entries: {stats['total_entries']}")
    print(f"Unique schools: {stats['unique_schools']}")
    print(f"Covered dates: {stats['covered_dates']}")
    print(f"Modified entries: {stats['modified_entries']}")
    print(f"Total fetches: {stats['total_fetches']}")
    print(f"Database: {stats['db_path']}")


async def show_history(db: SubstitutionPlanDB, school_id: str, limit: int = 10):
    """Show fetch history."""
    await db.initialize()
    history = await db.get_fetch_history(school_id, limit)

    print(f"\n=== Fetch History for {school_id} ===\n")
    for h in history:
        print(f"{h['fetch_date']}: {h['entry_count']} entries")


async def main():
    parser = argparse.ArgumentParser(description="Query substitution plan data")
    parser.add_argument("--school-id", default="default", help="School ID")
    parser.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    parser.add_argument(
        "--days", type=int, default=7, help="Number of days to look ahead"
    )
    parser.add_argument(
        "--conflicts", action="store_true", help="Show conflicts for a date"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--stats", action="store_true", help="Show database statistics")
    parser.add_argument("--history", action="store_true", help="Show fetch history")

    args = parser.parse_args()

    db = SubstitutionPlanDB()

    if args.stats:
        await show_stats(db)
        return

    target_date = None
    if args.date:
        target_date = date.fromisoformat(args.date)

    if args.history:
        await show_history(db, args.school_id)
        return

    await query_entries(
        db,
        args.school_id,
        target_date,
        args.days,
        args.conflicts,
        args.json,
    )


if __name__ == "__main__":
    asyncio.run(main())
