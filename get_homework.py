from main import SchulportalHessenAPI
from datetime import datetime, timedelta
import os
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

api = SchulportalHessenAPI()

print("[LOGIN] Logging in with env...")
result = api.login_using_env()

if result.get("success"):
    print("[OK] Login successful!\n")

    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
    print(f"[DATE] Looking for homework due: {tomorrow}\n")

    overview = api.meinunterricht_get_overview()
    if overview.get("success"):
        found = False

        # First check exact date match for tomorrow
        for entry in overview["entries"]:
            if entry.get("homework"):
                datum = entry.get("datum", "")
                name = entry.get("name", "").lower()

                if tomorrow in datum:
                    if "latein" in name or "deutsch" in name:
                        found = True
                        status = "DONE" if entry.get("homework_done") else "PENDING"
                        print(f"[SUBJECT] {entry['name']}")
                        print(f"   [DATE] {datum}")
                        print(f"   [HOMEWORK] ({status}): {entry['homework']}")
                        print()

        if not found:
            # Show all homework for these subjects to help user
            print(
                "[INFO] No homework found due tomorrow specifically. Showing all homework for Latein/Deutsch:\n"
            )
            for entry in overview["entries"]:
                if entry.get("homework"):
                    name = entry.get("name", "").lower()
                    if "latein" in name or "deutsch" in name:
                        status = "DONE" if entry.get("homework_done") else "PENDING"
                        print(f"[SUBJECT] {entry['name']}")
                        print(f"   [DATE] {entry.get('datum', 'N/A')}")
                        print(f"   [HOMEWORK] ({status}): {entry['homework']}")
                        print()
    else:
        print(f"[ERROR] {overview.get('error')}")
else:
    print(f"[ERROR] Login failed: {result.get('error')}")

api.close()
