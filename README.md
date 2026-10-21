# auto-brightness

Camera-based adaptive display backlight for Linux, designed to run as a **system-level service** under systemd (or standalone). It reads ambient light from a webcam, converts frame brightness into a perceptual backlight percentage, and adjusts your screen accordingly — while respecting manual tweaks you make yourself.

No D-Bus session bus, no `XDG_RUNTIME_DIR`, no logind session required.

---

## Features

- **Low-overhead sampling** — 3 captures every 5 minutes (~6 wakeups/hour)
- **Perceptual log curve** — maps raw luminance to backlight % smoothly
- **EMA smoothing** — avoids flicker between cycles
- **Hysteresis** — ignores tiny deltas to prevent jitter
- **Fade transitions** — animates brightness changes instead of snapping
- **Manual override learning** — if *you* change brightness, the script shifts its baseline instead of fighting you
- **Atomic config writes** — safe against power loss
- **Calibration mode** — observe raw values to tune your curve
- **One-shot mode** — perfect for cron or testing

---

## Requirements

| Dependency | Purpose |
|------------|---------|
| `python3` | Runtime |
| `ffmpeg` | Frame capture from `/dev/video*` |
| `v4l2-ctl` | Disable auto-exposure (recommended) |
| `brightnessctl` | Backlight control |
| Camera access | A webcam at `/dev/video0` (configurable) |
| Root / `video` + `input` groups | Access to camera + backlight |

### Install dependencies

**Debian / Ubuntu:**
```bash
sudo apt install ffmpeg v4l-utils brightnessctl
