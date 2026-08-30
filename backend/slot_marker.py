import cv2
import json
import os
import numpy as np

# Configuration
SOURCE = "parking_lot.mp4"  # Updated to your video file
OUTPUT_FILE = "parking_slots.json"

# How many frames each nav key-press jumps. 'a'/'d' = coarse, 'q'/'e' = fine.
COARSE_STEP = 30
FINE_STEP = 1

slots = []
current_points = []
frame_cache = None

# Load existing slots if available
if os.path.exists(OUTPUT_FILE):
    try:
        with open(OUTPUT_FILE, "r") as f:
            slots = json.load(f)
        print(f"[INFO] Loaded {len(slots)} existing slots from {OUTPUT_FILE}")
    except Exception as e:
        print(f"[WARN] Failed to load {OUTPUT_FILE}: {e}")


def mouse_event(event, x, y, flags, param):
    global current_points, slots, frame_cache

    if event == cv2.EVENT_LBUTTONDOWN:
        current_points.append([x, y])
        if len(current_points) == 4:
            slots.append({
                "id": len(slots) + 1,
                "polygon": current_points.copy()
            })
            current_points = []
            print(f"[INFO] Slot {len(slots)} created.")

    elif event == cv2.EVENT_RBUTTONDOWN:
        if not slots:
            return
        click_pt = np.array([x, y])
        min_dist = float("inf")
        target_idx = -1
        for idx, s in enumerate(slots):
            poly = np.array(s["polygon"])
            centroid = np.mean(poly, axis=0)
            dist = np.linalg.norm(click_pt - centroid)
            if dist < min_dist:
                min_dist = dist
                target_idx = idx

        if target_idx != -1 and min_dist < 60:
            removed = slots.pop(target_idx)
            for i, s in enumerate(slots):
                s["id"] = i + 1
            print(f"[INFO] Removed slot {removed['id']}.")


def seek_frame(cap, frame_idx, total_frames):
    """Jump to frame_idx (clamped) and read it into frame_cache. Returns the
    (possibly clamped) frame_idx actually landed on, or None on read failure."""
    frame_idx = max(0, min(frame_idx, total_frames - 1))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    if not ret or frame is None:
        return None, None
    return frame, frame_idx


def main():
    global current_points, slots, frame_cache

    cap = cv2.VideoCapture(SOURCE)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open source: {SOURCE}")
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    native_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1
    native_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1

    # Window size follows the video's real aspect ratio instead of a fixed
    # 1280x720 -- a mismatched window size stretches/squashes the picture,
    # which was making everything look off. Scale down only if it's larger
    # than a comfortable screen size; never scale up past native.
    max_dim = 1280
    scale = min(1.0, max_dim / max(native_w, native_h))
    win_w, win_h = int(native_w * scale), int(native_h * scale)

    # Skip past the first frame blindly -- lots of source videos open on a
    # black/fade-in frame. Start a little way in instead of at frame 0, and
    # you can still scrub freely with the nav keys below either way.
    start_idx = min(int(total_frames * 0.02) + 5, total_frames - 1)
    frame_cache, frame_idx = seek_frame(cap, start_idx, total_frames)
    if frame_cache is None:
        # fall back to frame 0 if that seek somehow failed
        frame_cache, frame_idx = seek_frame(cap, 0, total_frames)
    if frame_cache is None:
        print("[ERROR] Cannot read any frame from video file.")
        cap.release()
        return

    cv2.namedWindow("Parking Slot Annotator", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Parking Slot Annotator", win_w, win_h)
    cv2.setMouseCallback("Parking Slot Annotator", mouse_event)

    print("\n--- INSTRUCTIONS ---")
    print("1. Click 4 corners (TL -> TR -> BR -> BL) to mark a slot.")
    print("2. Right-click inside a slot to delete it.")
    print(f"3. 'd' / 'a' -> jump {COARSE_STEP} frames fwd/back (find a clear frame).")
    print(f"4. 'e' / 'q' -> step {FINE_STEP} frame fwd/back (fine-tune).")
    print("5. Press 'u' to undo last point.")
    print("6. Press 's' to SAVE to parking_slots.json.")
    print("7. Press 'c' to CLEAR all slots.")
    print("8. Press 'r' to QUIT.\n")

    while True:
        display = frame_cache.copy()

        for s in slots:
            pts = np.array(s["polygon"], np.int32).reshape((-1, 1, 2))
            cv2.polylines(display, [pts], isClosed=True, color=(0, 255, 255), thickness=2)
            c_x = int(np.mean([p[0] for p in s["polygon"]]))
            c_y = int(np.mean([p[1] for p in s["polygon"]]))
            cv2.circle(display, (c_x, c_y), 4, (0, 0, 255), -1)
            cv2.putText(display, f"P{s['id']}", (c_x - 10, c_y + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

        for i, pt in enumerate(current_points):
            cv2.circle(display, (pt[0], pt[1]), 5, (0, 255, 0), -1)
            if i > 0:
                cv2.line(display, tuple(current_points[i - 1]), tuple(pt), (0, 255, 0), 2)

        cv2.rectangle(display, (0, 0), (display.shape[1], 40), (20, 20, 20), -1)
        cv2.putText(
            display,
            f"Frame {frame_idx}/{total_frames} | Slots: {len(slots)} | d/a=jump  q/e=step  s=save  c=clear  r=quit",
            (15, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 200), 2,
        )

        cv2.imshow("Parking Slot Annotator", display)
        key = cv2.waitKey(20) & 0xFF

        if key == ord('r'):
            break
        elif key == ord('u'):
            if current_points:
                current_points.pop()
        elif key == ord('c'):
            slots = []
            current_points = []
            print("[INFO] Cleared all slots.")
        elif key == ord('s'):
            with open(OUTPUT_FILE, "w") as f:
                json.dump(slots, f, indent=4)
            print(f"[SUCCESS] Saved {len(slots)} slots to {OUTPUT_FILE}")
        elif key in (ord('d'), ord('a'), ord('q'), ord('e')):
            step = COARSE_STEP if key in (ord('d'), ord('a')) else FINE_STEP
            direction = 1 if key in (ord('d'), ord('e')) else -1
            new_frame, new_idx = seek_frame(cap, frame_idx + step * direction, total_frames)
            if new_frame is not None:
                frame_cache, frame_idx = new_frame, new_idx
            else:
                print("[WARN] Could not read frame there — staying put.")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()