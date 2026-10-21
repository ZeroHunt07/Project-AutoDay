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
```

**Arch:**
```bash
sudo pacman -S ffmpeg v4l-utils brightnessctl
```

**Fedora:**
```bash
sudo dnf install ffmpeg v4l-utils brightnessctl
```

---

## Installation

1. **Download the script:**
   ```bash
   sudo install -m 755 auto-brightness.py /usr/local/bin/auto-brightness
   ```

2. **Run once to generate a default config:**
   ```bash
   sudo auto-brightness --once
   ```
   This creates `/etc/auto-brightness/config.json`.

3. **Edit the config** (see below) and calibrate.

4. **Install the systemd service** (see [Running as a service](#running-as-a-service)).

---

## Usage

```bash
# Normal daemon loop
sudo auto-brightness

# One check cycle then exit (for cron / testing)
sudo auto-brightness --once

# Calibration: prints raw luminance and computed % (Ctrl-C to quit)
sudo auto-brightness --calibrate

# Verbose logging
sudo auto-brightness -v
```

> **Tip:** Run `--calibrate` in a dark room, then a bright room, and note the `raw=` values. Use those to tune `min_raw` / `max_raw`.

---

## Configuration

Config lives at `/etc/auto-brightness/config.json` (auto-created on first run).

| Key | Default | Description |
|-----|---------|-------------|
| `camera` | `/dev/video0` | V4L2 device to sample |
| `capture_width` | `64` | Frame downscale width (raw gray) |
| `capture_height` | `48` | Frame downscale height |
| `check_interval_s` | `300` | Seconds between sampling cycles |
| `samples_per_check` | `3` | Frames averaged per cycle |
| `sample_gap_s` | `1.0` | Seconds between frames within a cycle |
| `exposure_auto` | `false` | `true` = leave camera auto-exposure alone |
| `exposure_absolute` | `300` | Fixed exposure (100 µs units → 30 ms) |
| `min_pct` | `5` | Backlight floor (%) |
| `max_pct` | `100` | Backlight ceiling (%) |
| `min_raw` | `3.0` | Mean gray value mapped to `min_pct` |
| `max_raw` | `180.0` | Mean gray value mapped to `max_pct` |
| `ema_alpha` | `0.3` | EMA smoothing factor across cycles (0 < α ≤ 1) |
| `hysteresis_pct` | `2.0` | Minimum target delta before applying |
| `fade_steps` | `10` | Number of fade increments |
| `fade_delay_ms` | `25` | Delay between fade steps (ms) |
| `manual_override_threshold` | `3.0` | % change that counts as a manual tweak |
| `backlight_cmd` | `brightnessctl` | Backlight tool (resolved to abs path) |
| `backlight_device` | `null` | Optional `-d` device, e.g. `intel_backlight` |

### Example config

```json
{
  "camera": "/dev/video0",
  "capture_width": 64,
  "capture_height": 48,
  "check_interval_s": 300,
  "samples_per_check": 3,
  "sample_gap_s": 1.0,
  "exposure_auto": false,
  "exposure_absolute": 300,
  "min_pct": 5,
  "max_pct": 100,
  "min_raw": 3.0,
  "max_raw": 180.0,
  "ema_alpha": 0.3,
  "hysteresis_pct": 2.0,
  "fade_steps": 10,
  "fade_delay_ms": 25,
  "manual_override_threshold": 3.0,
  "backlight_cmd": "brightnessctl",
  "backlight_device": null
}
```

Config is upgraded in-place on upgrade — new keys are added, removed keys are dropped, unknown keys ignored with a log message.

---

## Running as a service

Create `/etc/systemd/system/auto-brightness.service`:

```ini
[Unit]
Description=Auto brightness (camera-based adaptive backlight)
After=multi-user.target
Wants=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/auto-brightness
Restart=on-failure
RestartSec=10
# Hardening — adjust as needed for your distro
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=no
ReadWritePaths=/etc/auto-brightness
DeviceAllow=/dev/video0 rw
DeviceAllow=/sys/class/backlight/ rw

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now auto-brightness.service
sudo journalctl -u auto-brightness -f
```

> The service is designed to run **at system level**, not as a user unit — no session bus needed.

---

## How it works

Each cycle (default every 5 min):

```
3× ffmpeg frame  →  average  →  EMA across cycles  →  log curve
    →  manual-override correction  →  hysteresis gate  →  fade  →  brightnessctl
```

- **Raw value:** mean gray (0–255) of a downscaled frame.
- **Log curve:** `t = log(raw/min_raw) / log(max_raw/min_raw)` gives perceptual uniformity.
- **EMA:** new smoothed value blends 30% of the new sample with 70% history.
- **Manual override:** if the current backlight differs from what *we* last set by more than `manual_override_threshold`, we assume you changed it and add the difference to a persistent `baseline` shift.
- **Hysteresis:** changes smaller than `hysteresis_pct` are skipped.
- **Fade:** brightness is stepped toward the target over `fade_steps × fade_delay_ms`.

---

## Calibration walkthrough

1. Run in a dark room:
   ```bash
   sudo auto-brightness --calibrate
   ```
   Note the `raw=` value (e.g. `raw=4.2`).

2. Run with all lights on, aimed at a bright wall:
   ```bash
   sudo auto-brightness --calibrate
   ```
   Note the higher value (e.g. `raw=150.0`).

3. Set `min_raw` slightly below your dark reading, `max_raw` slightly above your bright one:
   ```json
   { "min_raw": 3.0, "max_raw": 170.0 }
   ```

4. Restart the service and watch the log:
   ```bash
   sudo journalctl -u auto-brightness -f
   ```

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `missing dependency: ffmpeg` | Install `ffmpeg`, ensure it's in `PATH`. |
| `backlight command not found` | Install `brightnessctl` or set absolute path in config. |
| `no readable frames` | Camera busy / permissions. Check `sudo ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 -f null -`. Add the service user to the `video` group. |
| Brightness never changes | Run `--once -v` to inspect `raw → curve`. Check `hysteresis_pct`. |
| Brightness keeps flipping | Reduce `ema_alpha`, raise `hysteresis_pct`, increase `check_interval_s`. |
| Camera produces black frames | Auto-exposure fighting you — check `v4l2-ctl -d /dev/video0 --list-ctrls` supports manual exposure, or set `exposure_auto: true`. |
| `brightnessctl` fails with permissions | Run as root, or add udev rule / `input` group membership. |

---

## Uninstall

```bash
sudo systemctl disable --now auto-brightness.service
sudo rm /etc/systemd/system/auto-brightness.service
sudo rm /usr/local/bin/auto-brightness
sudo rm -rf /etc/auto-brightness
sudo systemctl daemon-reload
```

---

## License

MIT — do what you want, no warranty.

---

## Notes

- The camera LED (if present) will briefly flash every cycle. To avoid this, use a camera without an indicator LED, or increase `check_interval_s`.
- If your laptop has a hardware ambient-light sensor, a dedicated `iio-sensor-proxy`-based tool will be more efficient than a webcam.
- The log curve assumes your raw values are stable — auto-exposure cameras produce inconsistent results and are best avoided (`exposure_auto: false`).
