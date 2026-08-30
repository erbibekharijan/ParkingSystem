"""
Run this once (and again any time you re-mark slots with slot_marker.py)
to push the slot layout into Supabase so the frontend knows how many
slots exist and their numbers.

    python sync_slots.py
"""
import json
from config import SLOTS_FILE
from supabase_client import get_client


def main():
    with open(SLOTS_FILE, "r") as f:
        raw = json.load(f)

    if not raw:
        print(f"[ERROR] No slots found in {SLOTS_FILE}. Run slot_marker.py first.")
        return

    sb = get_client()
    for s in raw:
        sb.table("slots").upsert(
            {"slot_number": s["id"], "status": "free"},
            on_conflict="slot_number",
        ).execute()

    print(f"[SUCCESS] Synced {len(raw)} slots to Supabase.")


if __name__ == "__main__":
    main()
