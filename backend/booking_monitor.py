"""
Runs independently of the video/detector, on real wall-clock time, and
enforces the booking timing rules:

  Check-in:
    - 5+ min after booking with no check-in  -> status = checkin_warning
    - 10+ min after booking with no check-in -> status = cancelled, slot freed

  Check-out (measured from each booking's expected_end_at):
    - 5-10 min late   -> status = exit_warning (no charge yet)
    - 15+ min late    -> status = overstay, extra_charge accrues per minute
      (charging starts at the 15-minute mark; minutes 10-15 are a grace
      window with a warning already showing but no charge yet)

Run this alongside parking_detector.py, in a separate terminal:
    python booking_monitor.py
"""
import time
from datetime import datetime, timezone

from config import (
    CHECKIN_WARNING_MIN, CHECKIN_CANCEL_MIN,
    EXIT_WARNING_MIN, OVERSTAY_CHARGE_START_MIN, OVERSTAY_RATE_PER_MIN,
    MONITOR_POLL_SECONDS,
)
from supabase_client import get_client

sb = get_client()


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def minutes_since(ts_str):
    return (datetime.now(timezone.utc) - parse_ts(ts_str)).total_seconds() / 60.0


def log_event(booking_id, event_type, message):
    sb.table("booking_events").insert({
        "booking_id": booking_id, "event_type": event_type, "message": message,
    }).execute()


def check_pending_bookings():
    res = sb.table("bookings").select("*").in_("status", ["pending", "checkin_warning"]).execute()
    for b in res.data:
        elapsed = minutes_since(b["booked_at"])

        if elapsed >= CHECKIN_CANCEL_MIN:
            sb.table("bookings").update({"status": "cancelled"}).eq("id", b["id"]).execute()
            sb.table("slots").update({"status": "free"}).eq("id", b["slot_id"]).execute()
            log_event(b["id"], "cancelled", f"No check-in after {elapsed:.1f} min — booking cancelled")

        elif elapsed >= CHECKIN_WARNING_MIN and b["status"] != "checkin_warning":
            sb.table("bookings").update({"status": "checkin_warning"}).eq("id", b["id"]).execute()
            sb.table("slots").update({"status": "checkin_warning"}).eq("id", b["slot_id"]).execute()
            log_event(b["id"], "checkin_warning", f"{elapsed:.1f} min late for check-in")


def check_active_bookings():
    res = sb.table("bookings").select("*").in_("status", ["checked_in", "exit_warning", "overstay"]).execute()
    for b in res.data:
        late = minutes_since(b["expected_end_at"])
        if late < EXIT_WARNING_MIN:
            continue

        if late >= OVERSTAY_CHARGE_START_MIN:
            charged_minutes = int(late - (OVERSTAY_CHARGE_START_MIN - 1))
            new_charge = round(charged_minutes * OVERSTAY_RATE_PER_MIN, 2)
            if new_charge != b.get("extra_charge", 0):
                sb.table("bookings").update({
                    "status": "overstay", "extra_charge": new_charge,
                }).eq("id", b["id"]).execute()
                sb.table("slots").update({"status": "overstay"}).eq("id", b["slot_id"]).execute()
                log_event(b["id"], "overstay_charge", f"{late:.1f} min overstay — charge now ₹{new_charge}")

        elif late >= EXIT_WARNING_MIN and b["status"] != "exit_warning":
            sb.table("bookings").update({"status": "exit_warning"}).eq("id", b["id"]).execute()
            sb.table("slots").update({"status": "exit_warning"}).eq("id", b["slot_id"]).execute()
            log_event(b["id"], "exit_warning", f"{late:.1f} min late leaving")


def main():
    print(f"[INFO] Booking monitor running (checking every {MONITOR_POLL_SECONDS}s). Ctrl+C to stop.")
    while True:
        try:
            check_pending_bookings()
            check_active_bookings()
        except Exception as e:
            print(f"[WARN] monitor loop error: {e}")
        time.sleep(MONITOR_POLL_SECONDS)


if __name__ == "__main__":
    main()
