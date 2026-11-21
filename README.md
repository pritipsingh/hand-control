# Hand Control

Control your Mac with your fingers through the webcam. MediaPipe tracks 21 points on your hand, the script works out which pose you're making, and pyautogui moves the mouse, clicks, scrolls, or runs a shortcut.

## Run it

```bash
cd ~/Desktop/hand-control
./run.sh
```

The first run downloads the MediaPipe hand model (about 8 MB). A preview window opens showing your hand, the current mode, and a gray box: that box maps to your whole screen.

**macOS permissions (one time):** in System Settings → Privacy & Security, allow your terminal app (Terminal, iTerm, VS Code…) under **Camera** and under **Accessibility**. Without Accessibility the cursor won't move. Restart the terminal after granting.

## Gestures

| Pose | What it does |
|---|---|
| ☝️ Point (index finger only) | Move the cursor; your fingertip is the pointer |
| 🤏 Pinch thumb + index | Left click. Keep pinching and move to drag, open to drop |
| Thumb touches middle fingertip (index still up) | Right click |
| ✌️ Peace sign (index + middle) | Scroll. Your hand's position when you make the sign is "zero": move up/down (or left/right) from there to scroll, farther = faster. Return to the start point to stop |
| ✊ Fist | Pause: nothing happens, so you can move your hand freely |

### Shortcut poses (hold still ~1 second)

Defined in [actions.json](actions.json). Defaults:

| Pose | Action |
|---|---|
| 🖐 Open palm (all five fingers) | Mission Control |
| 👍 Thumbs up | Open Chrome |
| 🤘 Rock (index + pinky) | Open Slack |
| 3 fingers (index, middle, ring) | Open YouTube |
| 🤙 Call me (thumb + pinky) | Spotlight (⌘ Space) |
| Pinky only | Open VS Code |

Action types you can use in `actions.json`:

```json
{ "label": "Notes",     "type": "open_app", "app": "Notes" }
{ "label": "Gmail",     "type": "open_url", "url": "https://mail.google.com" }
{ "label": "New tab",   "type": "hotkey",   "keys": ["command", "t"] }
{ "label": "Lock",      "type": "shell",    "cmd": "pmset displaysleepnow" }
```

Remove a pose from the file to disable it. Restart after editing.

## Keys (with the preview window focused)

- `q`: quit
- `p`: pause / resume
- `h`: hide / show the on-screen help

## Tuning

Constants at the top of [hand_control.py](hand_control.py):

- `ACTIVE_BOX`: the part of the camera frame that maps to the screen. Shrink it so you reach the screen edges with less arm movement.
- `SMOOTHING`: lower is steadier but laggier.
- `PINCH_ON` / `PINCH_OFF`: how close fingers must be to click and to release.
- `SCROLL_SPEED`, `SCROLL_DEADZONE`: scroll feel.
- `SHORTCUT_HOLD_S`, `SHORTCUT_COOLDOWN_S`: how long to hold a shortcut pose and the wait before the next one.

Options: `--camera 1` picks another camera, `--no-preview` runs without the window, `--check` tests the camera and exits.

## Tips

- Good, even lighting matters more than anything else.
- Keep your hand 40–70 cm from the camera with your palm facing it.
- Make a fist before moving your hand somewhere you don't want the cursor to follow.
