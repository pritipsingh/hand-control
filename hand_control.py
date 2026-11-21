"""Control your Mac with hand gestures through the webcam.

Gestures (see README.md for the full table):
  point (index finger only)        -> move the cursor
  pinch thumb + index              -> left click (hold the pinch to drag)
  pinch thumb + middle             -> right click
  peace sign (index + middle up)   -> scroll: move hand up/down/left/right from where you started
  fist                             -> pause (nothing happens)
  hold a shortcut pose ~1s         -> runs the action from actions.json

Keys in the preview window:  q = quit,  p = pause/resume,  h = hide/show preview overlay help
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import mediapipe as mp
import pyautogui
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "hand_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
ACTIONS_PATH = HERE / "actions.json"

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False  # the cursor reaches corners on purpose; use fist / 'p' / 'q' instead

# ---------- tuning ----------
ACTIVE_BOX = (0.18, 0.12, 0.82, 0.68)  # (x0, y0, x1, y1) part of the camera frame mapped to the full screen
SMOOTHING = 0.30          # 0..1, lower = smoother but laggier cursor
PINCH_ON = 0.32           # pinch distance / hand size to start a pinch
PINCH_OFF = 0.45          # ... and to release it (hysteresis avoids flicker)
PINCH_FREEZE_S = 0.18     # freeze the cursor briefly when a pinch starts so clicks land where you aimed
SCROLL_DEADZONE = 0.035   # how far you must move from the scroll anchor before it scrolls
SCROLL_SPEED = 22.0       # scroll lines per second per unit of offset
SHORTCUT_HOLD_S = 0.9     # how long to hold a shortcut pose
SHORTCUT_COOLDOWN_S = 2.0
POSE_STABLE_FRAMES = 3    # frames a pose must persist before mode switches

# MediaPipe landmark indices
WRIST, THUMB_IP, THUMB_TIP = 0, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_PIP, RING_TIP = 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]


# ---------- gesture recognition ----------
def _d(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def finger_states(lm) -> dict[str, bool]:
    """Which fingers are extended. Uses distances from the wrist so it works at any hand rotation."""
    w = lm[WRIST]

    def ext(tip, pip):
        return _d(lm[tip], w) > _d(lm[pip], w) * 1.12

    # Thumb: extended when its tip is well away from the palm (index MCP) and the pinky base.
    hand = _d(lm[WRIST], lm[MIDDLE_MCP])
    thumb = _d(lm[THUMB_TIP], lm[INDEX_MCP]) > 0.55 * hand and _d(lm[THUMB_TIP], lm[PINKY_MCP]) > _d(lm[THUMB_IP], lm[PINKY_MCP]) * 1.05
    return {
        "thumb": thumb,
        "index": ext(INDEX_TIP, INDEX_PIP),
        "middle": ext(MIDDLE_TIP, MIDDLE_PIP),
        "ring": ext(RING_TIP, RING_PIP),
        "pinky": ext(PINKY_TIP, PINKY_PIP),
    }


def hand_size(lm) -> float:
    return max(_d(lm[WRIST], lm[MIDDLE_MCP]), 1e-6)


def classify(lm) -> str:
    """Return a pose name. Pinches are handled separately so they work from the pointing pose."""
    f = finger_states(lm)
    t, i, m, r, p = f["thumb"], f["index"], f["middle"], f["ring"], f["pinky"]
    if i and m and r and p and t:
        return "open_palm"
    if i and m and r and not p:
        return "three"
    if i and m and not r and not p:
        return "scroll"
    if i and not m and not r and p:
        return "rock"
    if t and p and not i and not m and not r:
        return "call_me"
    if p and not t and not i and not m and not r:
        return "pinky"
    if i and not m and not r and not p:
        return "point"
    if t and not i and not m and not r and not p:
        # thumb clearly above the knuckles (image y grows downward)
        if lm[THUMB_TIP].y < lm[INDEX_MCP].y - 0.3 * hand_size(lm):
            return "thumbs_up"
        return "fist"
    if not i and not m and not r and not p:
        return "fist"
    return "unknown"


# ---------- actions ----------
def load_actions() -> dict:
    try:
        data = json.loads(ACTIONS_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not read {ACTIONS_PATH.name}: {e}")
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def run_action(action: dict) -> None:
    kind = action.get("type")
    try:
        if kind == "open_app":
            subprocess.Popen(["open", "-a", action["app"]])
        elif kind == "open_url":
            webbrowser.open(action["url"])
        elif kind == "hotkey":
            pyautogui.hotkey(*action["keys"])
        elif kind == "shell":
            subprocess.Popen(action["cmd"], shell=True)
        else:
            print(f"[warn] unknown action type: {kind}")
            return
        print(f"[action] {action.get('label', kind)}")
    except Exception as e:  # noqa: BLE001
        print(f"[error] action failed: {e}")


# ---------- controller ----------
@dataclass
class Controller:
    screen_w: int
    screen_h: int
    actions: dict
    paused: bool = False

    cur_x: float | None = None
    cur_y: float | None = None
    mode: str = "none"
    candidate: str = "none"
    candidate_frames: int = 0

    left_down: bool = False
    right_pinched: bool = False
    freeze_until: float = 0.0

    scroll_anchor: tuple[float, float] | None = None
    scroll_acc: list[float] = field(default_factory=lambda: [0.0, 0.0])

    shortcut_pose: str | None = None
    shortcut_since: float = 0.0
    last_shortcut: float = 0.0
    status: str = ""

    def release_all(self):
        if self.left_down:
            pyautogui.mouseUp()
            self.left_down = False
        self.right_pinched = False

    def update(self, lm, now: float, dt: float) -> None:
        pose = classify(lm)
        size = hand_size(lm)
        pinch_idx = _d(lm[THUMB_TIP], lm[INDEX_TIP]) / size
        pinch_mid = _d(lm[THUMB_TIP], lm[MIDDLE_TIP]) / size
        f = finger_states(lm)
        others_down = not f["ring"] and not f["pinky"]

        # Pinch takes priority: while dragging, keep it until fingers clearly separate.
        if self.left_down:
            pose = "point"
        elif others_down and not f["middle"] and pinch_idx < PINCH_ON:
            pose = "point"  # pinching from the pointing pose may curl the index; still pointer mode
        elif others_down and f["index"] and pinch_mid < PINCH_ON:
            pose = "point"

        # Debounce pose changes
        if pose == self.candidate:
            self.candidate_frames += 1
        else:
            self.candidate, self.candidate_frames = pose, 1
        if self.candidate_frames >= POSE_STABLE_FRAMES and self.mode != self.candidate:
            self._enter(self.candidate, lm)

        if self.paused:
            self.status = "PAUSED (press p)"
            return

        if self.mode == "point":
            self._pointer(lm, now, pinch_idx, pinch_mid)
        elif self.mode == "scroll":
            self._scroll(lm, dt)
        elif self.mode in self.actions:
            self._shortcut(now)
        elif self.mode == "fist":
            self.status = "fist: paused"
        else:
            self.status = self.mode

    def _enter(self, new_mode: str, lm):
        if self.mode == "point":
            self.release_all()
        self.mode = new_mode
        self.scroll_anchor = None
        self.shortcut_pose = None
        if new_mode == "scroll":
            self.scroll_anchor = self._scroll_point(lm)
            self.scroll_acc = [0.0, 0.0]

    # --- pointer / click / drag ---
    def _pointer(self, lm, now, pinch_idx, pinch_mid):
        tip = lm[INDEX_TIP]
        x0, y0, x1, y1 = ACTIVE_BOX
        nx = min(max((tip.x - x0) / (x1 - x0), 0.0), 1.0)
        ny = min(max((tip.y - y0) / (y1 - y0), 0.0), 1.0)
        tx, ty = nx * (self.screen_w - 1), ny * (self.screen_h - 1)

        if self.cur_x is None:
            self.cur_x, self.cur_y = tx, ty
        # Adaptive smoothing: move faster when the hand moves a lot, stay steady when it's still.
        dist = math.hypot(tx - self.cur_x, ty - self.cur_y)
        a = min(1.0, SMOOTHING + dist / 600.0)
        if now >= self.freeze_until:
            self.cur_x += (tx - self.cur_x) * a
            self.cur_y += (ty - self.cur_y) * a
            if dist > 1.5:
                pyautogui.moveTo(self.cur_x, self.cur_y)

        # Left click / drag
        if not self.left_down and pinch_idx < PINCH_ON:
            self.freeze_until = now + PINCH_FREEZE_S
            pyautogui.mouseDown()
            self.left_down = True
        elif self.left_down and pinch_idx > PINCH_OFF:
            pyautogui.mouseUp()
            self.left_down = False

        # Right click
        if not self.left_down:
            if not self.right_pinched and pinch_mid < PINCH_ON:
                self.right_pinched = True
                self.freeze_until = now + PINCH_FREEZE_S
                pyautogui.click(button="right")
            elif self.right_pinched and pinch_mid > PINCH_OFF:
                self.right_pinched = False

        self.status = "drag / click" if self.left_down else ("right click" if self.right_pinched else "pointer")

    # --- scroll ---
    @staticmethod
    def _scroll_point(lm):
        return ((lm[INDEX_TIP].x + lm[MIDDLE_TIP].x) / 2, (lm[INDEX_TIP].y + lm[MIDDLE_TIP].y) / 2)

    def _scroll(self, lm, dt):
        if self.scroll_anchor is None:
            self.scroll_anchor = self._scroll_point(lm)
        px, py = self._scroll_point(lm)
        ax, ay = self.scroll_anchor
        dy = ay - py  # hand up => positive => scroll up
        dx = px - ax  # hand right => scroll right

        def rate(v):
            if abs(v) < SCROLL_DEADZONE:
                return 0.0
            v = v - math.copysign(SCROLL_DEADZONE, v)
            return math.copysign((abs(v) * 10) ** 1.5, v) * SCROLL_SPEED  # gentle near the anchor, faster further away

        self.scroll_acc[1] += rate(dy) * dt
        self.scroll_acc[0] += rate(dx) * dt
        sy, sx = int(self.scroll_acc[1]), int(self.scroll_acc[0])
        if sy:
            pyautogui.scroll(sy)
            self.scroll_acc[1] -= sy
        if sx:
            pyautogui.hscroll(-sx)
            self.scroll_acc[0] -= sx
        arrow = "up" if dy > SCROLL_DEADZONE else "down" if dy < -SCROLL_DEADZONE else "hold"
        self.status = f"scroll {arrow}"

    # --- shortcuts ---
    def _shortcut(self, now):
        action = self.actions[self.mode]
        label = action.get("label", self.mode)
        if now - self.last_shortcut < SHORTCUT_COOLDOWN_S:
            self.status = f"{label} (cooldown)"
            return
        if self.shortcut_pose != self.mode:
            self.shortcut_pose, self.shortcut_since = self.mode, now
        held = now - self.shortcut_since
        if held >= SHORTCUT_HOLD_S:
            run_action(action)
            self.last_shortcut = now
            self.shortcut_pose = None
            self.status = f"ran: {label}"
        else:
            self.status = f"hold for {label}: {int(100 * held / SHORTCUT_HOLD_S)}%"

    def no_hand(self):
        if self.mode == "point":
            self.release_all()
        self.mode = self.candidate = "none"
        self.candidate_frames = 0
        self.status = "no hand"


# ---------- drawing ----------
def draw(frame, lm, ctrl: Controller, fps: float, show_help: bool):
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = ACTIVE_BOX
    cv2.rectangle(frame, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)), (90, 90, 90), 1)
    if lm is not None:
        pts = [(int(p.x * w), int(p.y * h)) for p in lm]
        for a, b in HAND_CONNECTIONS:
            cv2.line(frame, pts[a], pts[b], (0, 200, 255), 2)
        for p in pts:
            cv2.circle(frame, p, 3, (255, 255, 255), -1)
        cv2.circle(frame, pts[INDEX_TIP], 8, (0, 255, 0) if not ctrl.left_down else (0, 0, 255), 2)

    cv2.rectangle(frame, (0, 0), (w, 34), (0, 0, 0), -1)
    cv2.putText(frame, f"{ctrl.mode.upper()}  |  {ctrl.status}", (10, 23),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
    cv2.putText(frame, f"{fps:4.0f} fps", (w - 95, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 180), 1)

    if show_help:
        lines = ["point: move | pinch: click/drag | thumb+middle: right click",
                 "peace: scroll | fist: pause | q quit, p pause, h help"]
        for k, v in ctrl.actions.items():
            lines.append(f"hold {k}: {v.get('label', '')}")
        for n, line in enumerate(lines):
            y = h - 12 - 20 * (len(lines) - 1 - n)
            cv2.putText(frame, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
            cv2.putText(frame, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)


# ---------- main ----------
def ensure_model():
    if MODEL_PATH.exists():
        return
    print("Downloading MediaPipe hand model (~8 MB, one time)...")
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0, help="camera index (default 0)")
    ap.add_argument("--no-preview", action="store_true", help="run without the preview window")
    ap.add_argument("--check", action="store_true", help="open the camera, grab one frame, and exit")
    args = ap.parse_args()

    ensure_model()
    cap = cv2.VideoCapture(args.camera)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 960)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 540)
    if not cap.isOpened():
        sys.exit("Could not open the camera. Grant Camera access to your terminal app in "
                 "System Settings > Privacy & Security > Camera, then run again.")

    options = vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.5,
    )
    landmarker = vision.HandLandmarker.create_from_options(options)

    if args.check:
        ok, frame = cap.read()
        cap.release()
        print("camera ok" if ok else "camera opened but returned no frame (permission?)")
        if ok:
            print("frame", frame.shape, "| screen", pyautogui.size())
        return

    sw, sh = pyautogui.size()
    ctrl = Controller(sw, sh, load_actions())
    print(f"Running. Screen {sw}x{sh}. Shortcuts: {', '.join(ctrl.actions) or 'none'}. Press q in the preview to quit.")

    show_help, last, fps, t0 = True, time.monotonic(), 0.0, time.monotonic()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            frame = cv2.flip(frame, 1)  # mirror, so moving right moves the cursor right
            now = time.monotonic()
            dt, last = now - last, now
            fps = 0.9 * fps + 0.1 * (1 / dt if dt > 0 else 0)

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int((now - t0) * 1000))
            lm = result.hand_landmarks[0] if result.hand_landmarks else None
            if lm is not None:
                ctrl.update(lm, now, dt)
            else:
                ctrl.no_hand()

            if not args.no_preview:
                draw(frame, lm, ctrl, fps, show_help)
                cv2.imshow("Hand Control", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("p"):
                    ctrl.paused = not ctrl.paused
                    ctrl.release_all()
                if key == ord("h"):
                    show_help = not show_help
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.release_all()
        cap.release()
        landmarker.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
