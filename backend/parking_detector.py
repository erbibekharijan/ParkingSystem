"""
Real-time parking occupancy + check-in/check-out engine.

For each slot, when a vehicle transitions free -> occupied, a short "read
window" opens: up to MAX_PLATE_READ_ATTEMPTS plate-OCR attempts, spaced
PLATE_READ_FRAME_STRIDE processed-frames apart, run on a background thread
so they never block video playback or occupancy detection. As soon as
stable_plate() is confident (or the window runs out), the slot resolves:
  - plate matches (or the whole window passed with no confident read)
        -> check the booking in
  - plate clearly mismatches
        -> logged as a mismatch, booking left as-is (booking_monitor.py
           will still auto-cancel it if no valid check-in follows)

When a slot transitions occupied -> free, the active booking (if any) is
marked completed. Booking *timing* rules (late check-in warnings/
cancellation, late check-out warnings/overstay charges) are handled by
booking_monitor.py, which runs independently on wall-clock time so it keeps
working correctly regardless of video playback speed.

Run this alongside booking_monitor.py, in a separate terminal:
    python parking_detector.py
"""
import json
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import cv2
import numpy as np
from shapely.geometry import Polygon, box
from ultralytics import YOLO

from config import (
    VIDEO_SOURCE, SLOTS_FILE, MODEL_WEIGHTS, CONFIDENCE_THRESHOLD,
    IOU_OCCUPANCY_THRESHOLD, TEMPORAL_BUFFER_FRAMES, TARGET_CLASSES,
    PROCESS_EVERY_N_FRAMES, MAX_PLATE_READ_ATTEMPTS, PLATE_READ_FRAME_STRIDE,
    OCR_WORKER_THREADS, DEVICE,
)
from supabase_client import get_client
from plate_reader import PlateReader

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("parking_detector")

sb = get_client()
log.info(f"Compute device: {DEVICE}")
plate_reader = PlateReader(gpu=(DEVICE != "cpu"))
# See OCR_WORKER_THREADS in config.py for why this stays at 1 by default.
ocr_executor = ThreadPoolExecutor(max_workers=OCR_WORKER_THREADS, thread_name_prefix="ocr")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_plate(p):
    return "".join(ch for ch in p.upper() if ch.isalnum())


def db_call(fn, *, what, retries=2, backoff=0.5):
    """Run a Supabase call with a couple of retries so a brief network blip
    doesn't take the whole detector down. Returns None on final failure."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                log.warning(f"{what} failed after {retries + 1} attempt(s): {e}")
                return None
            time.sleep(backoff * (attempt + 1))


def log_event(booking_id, event_type, message):
    db_call(
        lambda: sb.table("booking_events").insert({
            "booking_id": booking_id,
            "event_type": event_type,
            "message": message,
        }).execute(),
        what="log_event",
    )


def fetch_slot_rows():
    res = db_call(lambda: sb.table("slots").select("*").execute(), what="fetch_slot_rows")
    return {row["slot_number"]: row for row in res.data} if res else {}


def fetch_active_booking(slot_db_id):
    res = db_call(
        lambda: (
            sb.table("bookings")
            .select("*")
            .eq("slot_id", slot_db_id)
            .in_("status", ["pending", "checkin_warning", "checked_in", "exit_warning", "overstay"])
            .order("booked_at", desc=True)
            .limit(1)
            .execute()
        ),
        what="fetch_active_booking",
    )
    return res.data[0] if res and res.data else None


class SlotState:
    def __init__(self, slot_id, points, buffer_len=TEMPORAL_BUFFER_FRAMES):
        self.id = slot_id
        self.points = points
        self.poly_np = np.array(points, np.int32).reshape((-1, 1, 2))
        self.shapely_poly = Polygon(points)
        self.slot_area = self.shapely_poly.area
        self.history = deque(maxlen=buffer_len)
        self.is_occupied = False
        self.current_ratio = 0.0
        self.matched_box = None

        # --- plate-read window state (opens on free -> occupied) ---
        self.plate_attempts_left = 0
        self.plate_frame_counter = 0
        self.pending_future = None      # in-flight OCR job for this slot, if any
        self.checkin_resolved = True    # False while a check-in decision is pending
        self.last_plate_text = None     # OCR-read plate, for on-screen overlay only
        self.active_car_number = None   # booked car number once checked in, for overlay

    def update(self, detected_boxes):
        max_overlap_ratio = 0.0
        best_box = None
        for (x1, y1, x2, y2) in detected_boxes:
            b_poly = box(x1, y1, x2, y2)
            if self.shapely_poly.intersects(b_poly):
                intersection_area = self.shapely_poly.intersection(b_poly).area
                ratio = intersection_area / max(self.slot_area, 1.0)
                if ratio > max_overlap_ratio:
                    max_overlap_ratio = ratio
                    best_box = (x1, y1, x2, y2)

        self.current_ratio = max_overlap_ratio
        self.matched_box = best_box
        instant_occupied = max_overlap_ratio >= IOU_OCCUPANCY_THRESHOLD
        was_occupied = self.is_occupied
        self.history.append(instant_occupied)

        if len(self.history) > 0:
            occupied_count = sum(self.history)
            self.is_occupied = occupied_count >= (self.history.maxlen // 2 + 1)

        return was_occupied, self.is_occupied


def handle_checkin(slot, slot_row, detected_plate):
    """
    Attempt to resolve check-in for a slot given the best plate reading
    available so far (may be None if the read window hasn't produced a
    confident result yet). Returns True once the decision is resolved
    (checked in, mismatched, or no active booking to check in) and no
    further attempts are needed this occupancy; False if it should be
    retried on a later frame.
    """
    booking = fetch_active_booking(slot_row["id"])
    if not booking or booking["status"] not in ("pending", "checkin_warning"):
        # No booking to check in -- but a vehicle IS physically sitting in the
        # slot right now (walk-in car, or one that was already parked before
        # the system started). Mark the slot occupied anyway so the frontend
        # blocks it from being booked instead of showing it as free.
        if slot_row["status"] not in ("occupied", "checked_in"):
            db_call(
                lambda: sb.table("slots").update({"status": "occupied"}).eq("id", slot_row["id"]).execute(),
                what="slot_occupied_walkin_update",
            )
            log_event(None, "walk_in_occupied",
                      f"Slot P{slot.id} occupied by a car with no active booking")
        return True

    verified = None  # None = couldn't verify (occupancy-only fallback)
    if detected_plate:
        verified = normalize_plate(detected_plate) == normalize_plate(booking["car_number"])
        if not verified:
            log_event(booking["id"], "plate_mismatch",
                       f"Slot P{slot.id}: camera saw '{detected_plate}', "
                       f"booking expects '{booking['car_number']}'")
            return True  # resolved as a mismatch; booking_monitor.py handles the fallout

    db_call(
        lambda: sb.table("bookings").update({
            "status": "checked_in",
            "check_in_time": now_iso(),
        }).eq("id", booking["id"]).execute(),
        what="check_in_update",
    )
    db_call(
        lambda: sb.table("slots").update({"status": "occupied"}).eq("id", slot_row["id"]).execute(),
        what="slot_occupied_update",
    )

    slot.active_car_number = booking["car_number"]
    note = "plate verified" if verified else "plate unread, occupancy-based check-in"
    log_event(booking["id"], "checked_in", f"Car {booking['car_number']} checked in to slot P{slot.id} ({note})")
    return True


def handle_checkout(slot, slot_row):
    booking = fetch_active_booking(slot_row["id"])
    if not booking or booking["status"] not in ("checked_in", "exit_warning", "overstay"):
        db_call(lambda: sb.table("slots").update({"status": "free"}).eq("id", slot_row["id"]).execute(),
                what="slot_free_update")
        return

    db_call(
        lambda: sb.table("bookings").update({
            "status": "completed",
            "check_out_time": now_iso(),
        }).eq("id", booking["id"]).execute(),
        what="check_out_update",
    )
    db_call(lambda: sb.table("slots").update({"status": "free"}).eq("id", slot_row["id"]).execute(),
            what="slot_free_update")
    log_event(booking["id"], "checked_out", f"Car {booking['car_number']} left slot P{slot.id}")
    plate_reader.reset(slot.id)
    slot.active_car_number = None


def draw_overlay(frame, slot, occupied):
    color = (0, 0, 255) if occupied else (0, 255, 0)
    cv2.polylines(frame, [slot.poly_np], True, color, 2)
    c_x = int(np.mean([p[0] for p in slot.points]))
    c_y = int(np.mean([p[1] for p in slot.points]))

    # Always show *something* once occupied, even if we never got a booking
    # or a confident plate read (e.g. a car that was already parked before
    # the system started, sitting there with no reservation behind it).
    label = f"P{slot.id}"
    if slot.active_car_number:
        label += f" - {slot.active_car_number}"
    elif slot.last_plate_text:
        label += f" [{slot.last_plate_text}]"
    elif occupied:
        label += " - Occupied"

    # Draw a filled background strip so the text stays legible over any
    # background in the video, then put text on top of it.
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
    cv2.rectangle(frame, (c_x - 12, c_y - th - 8), (c_x - 12 + tw + 8, c_y - 2), (0, 0, 0), -1)
    cv2.putText(frame, label, (c_x - 8, c_y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)


def process_slot(slot, slot_row, before, after, frame):
    """Advance a slot's plate-read window / check-in-checkout state machine
    by one frame. Mutates `slot` in place."""

    # Open a fresh read window the moment the slot becomes occupied.
    if not before and after:
        slot.plate_attempts_left = MAX_PLATE_READ_ATTEMPTS
        slot.plate_frame_counter = 0
        slot.checkin_resolved = False
        slot.last_plate_text = None

    if before and not after:
        if slot_row:
            handle_checkout(slot, slot_row)
        slot.plate_attempts_left = 0
        slot.pending_future = None
        slot.checkin_resolved = True
        return

    if not after:
        return

    # Collect a finished OCR result, if any, before deciding what to do this frame.
    reading = None
    if slot.pending_future is not None and slot.pending_future.done():
        try:
            reading = slot.pending_future.result()
        except Exception as e:
            log.warning(f"OCR job for slot P{slot.id} failed: {e}")
        slot.pending_future = None

    # Submit a new OCR job if we're still inside the read window, nothing is
    # already in flight for this slot, and it's this slot's turn per the
    # frame stride (keeps OCR calls spread out rather than firing every frame).
    if (slot.plate_attempts_left > 0 and slot.pending_future is None
            and slot.matched_box is not None):
        slot.plate_frame_counter += 1
        if slot.plate_frame_counter % PLATE_READ_FRAME_STRIDE == 0:
            box_snapshot = slot.matched_box
            frame_snapshot = frame.copy()  # OCR runs off-thread; freeze the pixels it reads
            slot.pending_future = ocr_executor.submit(plate_reader.read_plate, frame_snapshot, box_snapshot)
            slot.plate_attempts_left -= 1

    stable = plate_reader.stable_plate(slot.id, reading)
    if stable:
        slot.last_plate_text = stable

    if slot_row and not slot.checkin_resolved and (stable or slot.plate_attempts_left == 0):
        if handle_checkin(slot, slot_row, stable):
            slot.checkin_resolved = True
            slot.plate_attempts_left = 0  # stop burning OCR calls once resolved


def run_detection(model, frame):
    """Runs off the main thread -- see detection_executor below. Returns a
    list of (x1, y1, x2, y2) vehicle boxes in ORIGINAL frame coordinates."""
    # Downscaling before inference is the single biggest speed win on CPU;
    # YOLO cost scales roughly with pixel count. 640px on the long side is
    # plenty for occupancy/IOU purposes even though it loses fine plate detail
    # (plate OCR still reads from the full-res frame separately).
    h, w = frame.shape[:2]
    scale = 640.0 / max(h, w) if max(h, w) > 640 else 1.0
    small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame

    results = model.predict(
        source=small, classes=TARGET_CLASSES,
        conf=CONFIDENCE_THRESHOLD, verbose=False, imgsz=640, device=DEVICE,
    )
    boxes = []
    for r in results:
        if r.boxes is not None:
            for b in r.boxes.xyxy.cpu().numpy():
                boxes.append(b[:4] / scale)  # back to original-frame coordinates
    return boxes


def main():
    with open(SLOTS_FILE, "r") as f:
        raw_slots = json.load(f)
    if not raw_slots:
        log.error(f"No slots in {SLOTS_FILE}. Run slot_marker.py first.")
        return

    log.info(f"Loading YOLO model: {MODEL_WEIGHTS}...")
    model = YOLO(MODEL_WEIGHTS)
    slots = [SlotState(s["id"], s["polygon"]) for s in raw_slots]
    slot_rows = fetch_slot_rows()

    missing = [s.id for s in slots if s.id not in slot_rows]
    if missing:
        log.warning(f"Slots {missing} exist locally but not in Supabase. Run sync_slots.py.")

    cap = cv2.VideoCapture(VIDEO_SOURCE)
    if not cap.isOpened():
        log.error(f"Could not open video: {VIDEO_SOURCE}")
        return

    # Pace playback to the source video's own frame rate. Detection now runs
    # off-thread and no longer throttles us, so without this the loop reads
    # and displays frames as fast as the disk/decoder can go -- i.e. fast
    # forward. Falls back to 25fps if the file doesn't report a valid rate.
    source_fps = cap.get(cv2.CAP_PROP_FPS)
    if not source_fps or source_fps <= 1 or source_fps > 240:
        source_fps = 25.0
    frame_interval = 1.0 / source_fps

    cv2.namedWindow("Parking Booking - Live Detection", cv2.WINDOW_NORMAL)
    frame_count = 0
    vehicle_boxes = []
    fps_t0 = time.time()
    fps_frames = 0
    fps_display = 0.0

    # YOLO inference runs on its own single-worker thread, same pattern as
    # OCR. The video loop never blocks on it -- it just picks up the latest
    # finished detection whenever one is ready, so playback stays smooth even
    # if a detection pass takes longer than one frame interval.
    detection_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detect")
    pending_detection = None

    try:
        while cap.isOpened():
            loop_t0 = time.time()
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue

            frame_count += 1
            fps_frames += 1
            if time.time() - fps_t0 >= 1.0:
                fps_display = fps_frames / (time.time() - fps_t0)
                fps_frames = 0
                fps_t0 = time.time()

            if pending_detection is not None and pending_detection.done():
                try:
                    vehicle_boxes = pending_detection.result()
                except Exception as e:
                    log.warning(f"Detection job failed: {e}")
                pending_detection = None

            if (pending_detection is None
                    and frame_count % PROCESS_EVERY_N_FRAMES == 0):
                pending_detection = detection_executor.submit(run_detection, model, frame.copy())

            for slot in slots:
                before, after = slot.update(vehicle_boxes)
                slot_row = slot_rows.get(slot.id)
                process_slot(slot, slot_row, before, after, frame)
                draw_overlay(frame, slot, after)

            for (x1, y1, x2, y2) in vehicle_boxes:
                cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (255, 200, 0), 1)

            cv2.putText(frame, f"FPS: {fps_display:.1f}", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

            cv2.imshow("Parking Booking - Live Detection", frame)

            # Sleep off whatever time is left in this frame's real-world
            # slot, then poll for 'q' during that wait instead of a fixed 1ms.
            elapsed = time.time() - loop_t0
            remaining_ms = max(1, int((frame_interval - elapsed) * 1000))
            if cv2.waitKey(remaining_ms) & 0xFF == ord("q"):
                break
    finally:
        ocr_executor.shutdown(wait=False, cancel_futures=True)
        detection_executor.shutdown(wait=False, cancel_futures=True)
        cap.release()
        cv2.destroyAllWindows()
        log.info("Detector stopped.")


if __name__ == "__main__":
    main()