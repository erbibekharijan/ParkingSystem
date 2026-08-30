"""
Lightweight ALPR (Automatic License Plate Recognition) built on EasyOCR.

No pretrained plate-detector model is downloaded here on purpose (keeps
setup to a single `pip install`). Instead, for each vehicle box the
occupancy detector finds, we crop the lower portion of that box (where a
plate typically sits) using a few different crop windows, clean each one up,
and run OCR. A single frame's OCR read is noisy, so `stable_plate()` keeps a
short rolling history per slot (weighted by OCR confidence) and only commits
to a reading once it's seen consistent agreement.

Threading: OCR (`read_plate`) is the slow step here (tens-to-hundreds of ms).
Callers should run it on a background thread rather than the main video
loop -- see OCR_WORKER_THREADS / ocr_executor in parking_detector.py.
EasyOCR's Reader is not documented as safe for *concurrent* calls on the
same instance, so keep calls to a single instance serialized (one worker at
a time) unless you give each worker its own PlateReader. `stable_plate()`
and `reset()` mutate shared per-slot state and are guarded by an internal
lock, so they're safe to call from any thread.

If your camera angle is very different from a typical rear/front-on shot,
tune CROP_WINDOWS below.
"""
import re
import threading
from collections import deque, Counter

import cv2
import easyocr

MIN_OCR_CONFIDENCE = 0.35
PLATE_REGEX = re.compile(r"[A-Z0-9]{4,10}")

# (top_fraction, bottom_fraction) of the vehicle bbox height to crop, tried
# in order for every read -- plate position within the box varies with
# camera angle and vehicle type, so trying a few windows and keeping the
# highest-confidence hit beats guessing one fixed crop.
CROP_WINDOWS = [
    (0.55, 1.00),
    (0.65, 1.00),
    (0.45, 0.90),
]


class PlateReader:
    def __init__(self, langs=("en",), history_len=10, gpu=False):
        print("[INFO] Loading EasyOCR model (first run downloads weights)...")
        self.reader = easyocr.Reader(list(langs), gpu=gpu)
        self.history = {}  # slot_id -> deque[(plate_str, confidence)]
        self.history_len = history_len
        self._lock = threading.Lock()

    @staticmethod
    def _preprocess(crop):
        """Upscale small crops, denoise, and boost local contrast before OCR."""
        if crop is None or crop.size == 0:
            return None

        h, w = crop.shape[:2]
        # OCR accuracy drops sharply once character height gets small; upscale
        # short crops so text is a reasonable pixel height before reading it.
        scale = max(1.0, 80.0 / max(h, 1))
        if scale > 1.0:
            crop = cv2.resize(crop, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.bilateralFilter(gray, 11, 17, 17)

        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        return gray

    def _ocr_pass(self, gray):
        if gray is None:
            return []
        candidates = []
        for (_, text, conf) in self.reader.readtext(gray):
            cleaned = re.sub(r"[^A-Z0-9]", "", text.upper())
            if PLATE_REGEX.fullmatch(cleaned) and conf > MIN_OCR_CONFIDENCE:
                candidates.append((cleaned, conf))
        return candidates

    def read_plate(self, frame, bbox):
        """
        Run OCR once on a vehicle bbox, trying each entry in CROP_WINDOWS and
        keeping the highest-confidence valid-looking plate string found.
        Returns (plate_str, confidence) or None.
        """
        x1, y1, x2, y2 = [int(v) for v in bbox]
        x1, y1 = max(x1, 0), max(y1, 0)
        h = y2 - y1
        if h <= 0 or x2 <= x1:
            return None

        best = None
        for top_frac, bottom_frac in CROP_WINDOWS:
            crop_y1 = y1 + int(h * top_frac)
            crop_y2 = y1 + int(h * bottom_frac)
            crop = frame[crop_y1:crop_y2, x1:x2]
            gray = self._preprocess(crop)
            for cleaned, conf in self._ocr_pass(gray):
                if best is None or conf > best[1]:
                    best = (cleaned, conf)

        return best

    def stable_plate(self, slot_id, reading):
        """
        Feed one (plate_str, confidence) reading (or None) for a slot.
        Returns a plate string once confidence-weighted agreement across the
        recent history is strong enough, else None.
        """
        with self._lock:
            buf = self.history.setdefault(slot_id, deque(maxlen=self.history_len))
            if reading:
                buf.append(reading)
            if len(buf) < 3:
                return None

            weight = Counter()
            for plate, conf in buf:
                weight[plate] += conf
            plate, _ = weight.most_common(1)[0]

            votes = sum(1 for p, _ in buf if p == plate)
            if votes >= max(3, len(buf) // 2):
                return plate
            return None

    def reset(self, slot_id):
        with self._lock:
            self.history.pop(slot_id, None)