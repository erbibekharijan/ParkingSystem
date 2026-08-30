import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

VIDEO_SOURCE = os.getenv("VIDEO_SOURCE", "parking_lot.mp4")
SLOTS_FILE = os.getenv("SLOTS_FILE", "parking_slots.json")
MODEL_WEIGHTS = os.getenv("MODEL_WEIGHTS", "yolov8m.pt")

# ---- compute device ----
# Auto-uses your GPU if one is visible to torch; override with DEVICE=cpu (or
# a specific id like "cuda:1") in .env if you ever need to force CPU.
def _detect_device():
    forced = os.getenv("DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
    except Exception:
        pass
    return "cpu"

DEVICE = _detect_device()

# ---- occupancy detection ----
CONFIDENCE_THRESHOLD = 0.35
IOU_OCCUPANCY_THRESHOLD = 0.28
TEMPORAL_BUFFER_FRAMES = 8
TARGET_CLASSES = [2, 3, 5, 7]  # COCO: car, motorcycle, bus, truck
PROCESS_EVERY_N_FRAMES = 2

# ---- plate OCR window ----
# EasyOCR is by far the slowest step in the pipeline, so reads are spread out
# and run off the main thread (see OCR_WORKER_THREADS in parking_detector.py).
# After a slot flips free -> occupied, we get up to MAX_PLATE_READ_ATTEMPTS
# tries at reading the plate, one every PLATE_READ_FRAME_STRIDE processed
# frames, until stable_plate() is confident or the window runs out (at which
# point check-in falls back to occupancy-only, same as before).
MAX_PLATE_READ_ATTEMPTS = 8
PLATE_READ_FRAME_STRIDE = 3
# EasyOCR's Reader is not safe to call concurrently from multiple threads on
# one instance, so this stays at 1 by default -- it still keeps OCR off the
# video/detection thread, which is the actual win. Only raise this if you
# give each worker its own PlateReader instance.
OCR_WORKER_THREADS = 1

# ---- booking timing rules ----
CHECKIN_WARNING_MIN = 5     # warn if not checked in 5 min after booking
CHECKIN_CANCEL_MIN = 10     # auto-cancel if not checked in 10 min after booking
EXIT_WARNING_MIN = 0        # warn as soon as expected_end_at passes (no grace period)
OVERSTAY_CHARGE_START_MIN = 15   # start charging once 15 min over expected_end_at

# ₹5/min (₹300/hr) once overstay charging kicks in. Set deliberately steep relative to
# a typical ~₹20-40/hr base parking rate in India, since its job is to make leaving on
# time clearly cheaper than not — tune freely for your context.
OVERSTAY_RATE_PER_MIN = 5

MONITOR_POLL_SECONDS = 5