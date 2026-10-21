#!/usr/bin/env python3
"""
auto-brightness: camera-based adaptive display backlight for Linux (system-level).

Runs as root under systemd, or standalone via run-standalone.sh.
No D-Bus session bus, no XDG_RUNTIME_DIR, no logind session required.

Sampling model:
  Every `check_interval_s` (default 300 s = 5 min), take
  `samples_per_check` captures (default 3) spaced by `sample_gap_s`
  (default 1 s), average them, then apply the brightness curve.
  This keeps idle CPU wakeups to ~6 per hour.

Pipeline per cycle:
  N x ffmpeg frame -> average -> EMA across cycles -> log curve -> fade -> brightnessctl
Respects manual user adjustments via persistent baseline shift.
"""

import argparse
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

APP = "auto-brightness"
CONFIG_DIR = Path("/etc/auto-brightness")
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG = logging.getLogger(APP)

DEFAULT_CONFIG = {
    "camera": "/dev/video0",
    "capture_width": 64,
    "capture_height": 48,

    # --- sampling cadence: 3 checks every 5 minutes ---
    "check_interval_s": 300,            # seconds between cycles
    "samples_per_check": 3,             # captures per cycle
    "sample_gap_s": 1.0,                # seconds between captures

    "exposure_auto": False,             # if False -> fixed exposure below
    "exposure_absolute": 300,           # 100us units -> 30 ms
    "min_pct": 5,                       # backlight floor %
    "max_pct": 100,                     # backlight ceiling %
    "min_raw": 3.0,                     # gray mean at min_pct
    "max_raw": 180.0,                   # gray mean at max_pct
    "ema_alpha": 0.3,                   # smoothing across cycles (0<alpha<=1)
    "hysteresis_pct": 2.0,              # min delta before applying
    "fade_steps": 10,
    "fade_delay_ms": 25,
    "manual_override_threshold": 3.0,   # % delta that means user changed it
    "backlight_cmd": "brightnessctl",   # resolved to abs path at startup
    "backlight_device": None,           # e.g. "intel_backlight"
}


# ---------------- config ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    file_existed = CONFIG_PATH.exists()
    needs_write = False

    if file_existed:
        try:
            with open(CONFIG_PATH) as f:
                on_disk = json.load(f)
        except Exception as e:
            LOG.warning("could not read %s: %s", CONFIG_PATH, e)
            on_disk = {}
            needs_write = True

        # merge in known keys (user values win)
        for k, v in on_disk.items():
            if k in DEFAULT_CONFIG:
                cfg[k] = v
            else:
                LOG.info("ignoring unknown config key: %s", k)

        # any new keys the file doesn't have? flag a rewrite
        for k in DEFAULT_CONFIG:
            if k not in on_disk:
                LOG.info("adding new config key %s=%r", k, DEFAULT_CONFIG[k])
                needs_write = True

        # detect keys we removed from DEFAULT_CONFIG
        for k in on_disk:
            if k not in DEFAULT_CONFIG:
                needs_write = True
    else:
        needs_write = True

    if needs_write:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            # write to a temp file then rename so we never leave a
            # half-written config on crash/power-loss
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
            LOG.info("%s config at %s",
                     "wrote" if not file_existed else "upgraded",
                     CONFIG_PATH)
        except Exception as e:
            LOG.warning("could not write %s: %s (using in-memory defaults)",
                        CONFIG_PATH, e)

    if not os.path.isabs(cfg["backlight_cmd"]):
        cfg["backlight_cmd"] = shutil.which(cfg["backlight_cmd"]) or cfg["backlight_cmd"]

    return cfg
# ---------------- hardware ----------------

def _bl_args(cfg, *args):
    cmd = [cfg["backlight_cmd"], *args]
    if cfg.get("backlight_device"):
        cmd += ["-d", cfg["backlight_device"]]
    return cmd


def get_brightness(cfg):
    """Return current backlight % (0..100) or None."""
    try:
        out = subprocess.run(_bl_args(cfg, "-m"), capture_output=True,
                             text=True, timeout=2, check=True)
        # brightnessctl -m format: device,class,current,percent,max
        parts = out.stdout.strip().split(",")
        if len(parts) >= 4:
            return float(parts[3].rstrip("%"))
    except Exception as e:
        LOG.debug("get_brightness: %s", e)
    return None


def set_brightness(cfg, pct):
    pct = int(round(pct))
    try:
        subprocess.run(_bl_args(cfg, "set", f"{pct}%"),
                       capture_output=True, timeout=2, check=True)
        return True
    except Exception as e:
        LOG.warning("set_brightness(%d%%): %s", pct, e)
        return False


def setup_v4l2(cfg):
    """Force manual exposure so frame brightness tracks ambient light."""
    if cfg["exposure_auto"]:
        return
    v4l2 = shutil.which("v4l2-ctl") or "/usr/bin/v4l2-ctl"
    pairs = [
        ("auto_exposure", "exposure_time_absolute"),
        ("exposure_auto", "exposure_absolute"),
    ]
    for auto, absname in pairs:
        r = subprocess.run(
            [v4l2, "-d", cfg["camera"], f"--set-ctrl={auto}=1"],
            capture_output=True, timeout=3)
        if r.returncode == 0:
            subprocess.run(
                [v4l2, "-d", cfg["camera"],
                 f"--set-ctrl={absname}={cfg['exposure_absolute']}"],
                capture_output=True, timeout=3)
            LOG.info("fixed exposure via %s/%s=%s",
                     auto, absname, cfg["exposure_absolute"])
            return
    LOG.warning("could not disable auto-exposure; accuracy may suffer")


def capture_raw(cfg):
    """Return mean gray value (0..255) of one frame, or None."""
    w, h = cfg["capture_width"], cfg["capture_height"]
    cmd = [
        "ffmpeg", "-loglevel", "error", "-nostdin",
        "-f", "v4l2", "-i", cfg["camera"],
        "-frames:v", "1",
        "-vf", f"scale={w}:{h}",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=5)
        data = out.stdout
        if len(data) < w * h:
            return None
        return sum(data[: w * h]) / (w * h)
    except Exception as e:
        LOG.debug("capture_raw: %s", e)
        return None


# ---------------- algorithm ----------------

def raw_to_pct(raw, cfg):
    lo = max(cfg["min_raw"], 0.1)
    hi = max(cfg["max_raw"], lo + 1.0)
    raw = max(lo, min(hi, raw))
    t = math.log(raw / lo) / math.log(hi / lo)     # perceptual log curve
    return cfg["min_pct"] + (cfg["max_pct"] - cfg["min_pct"]) * t


def fade_to(cfg, start, target):
    if abs(start - target) < 0.5:
        set_brightness(cfg, target)
        return target
    steps = max(1, int(cfg["fade_steps"]))
    delay = max(0, cfg["fade_delay_ms"]) / 1000.0
    for i in range(1, steps + 1):
        set_brightness(cfg, start + (target - start) * i / steps)
        if delay:
            time.sleep(delay)
    return target


def sleep_interruptible(seconds):
    """Sleep, but return early if SIGTERM/SIGINT sets _running=False."""
    end = time.monotonic() + seconds
    while _running:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def sample_cycle(cfg):
    """Take `samples_per_check` captures, return their mean, or None."""
    samples = []
    n = max(1, int(cfg["samples_per_check"]))
    for i in range(n):
        raw = capture_raw(cfg)
        if raw is not None:
            samples.append(raw)
        if i < n - 1:
            sleep_interruptible(cfg["sample_gap_s"])
        if not _running:
            break
    if not samples:
        return None
    return sum(samples) / len(samples)


# ---------------- main ----------------

_running = True


def _stop(*_):
    global _running
    _running = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true",
                    help="print raw values only, Ctrl-C to exit")
    ap.add_argument("--once", action="store_true",
                    help="run a single check cycle then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cfg = load_config()

    if not shutil.which("ffmpeg"):
        LOG.error("missing dependency: ffmpeg")
        return 2
    if not (shutil.which(cfg["backlight_cmd"]) or os.path.exists(cfg["backlight_cmd"])):
        LOG.error("backlight command not found: %s", cfg["backlight_cmd"])
        return 2

    setup_v4l2(cfg)

    if args.calibrate:
        LOG.info("calibrate mode - observe `raw` in dark vs bright rooms")
        while _running:
            raw = capture_raw(cfg)
            if raw is not None:
                print(f"raw={raw:6.2f}  -> pct={raw_to_pct(raw, cfg):5.1f}%")
            sleep_interruptible(1.0)
        return 0

    LOG.info("started: camera=%s backlight=%s cadence=%ds x%d samples/%ds gap",
             cfg["camera"], cfg["backlight_cmd"],
             cfg["check_interval_s"], cfg["samples_per_check"],
             cfg["sample_gap_s"])

    ema = None
    last_set = None         # last value WE set
    baseline = 0.0          # persistent shift from user tweaks
    camera_misses = 0

    while _running:
        raw = sample_cycle(cfg)

        if raw is None:
            camera_misses += 1
            LOG.warning("cycle %d: no readable frames (camera busy or gone?)",
                        camera_misses)
            if args.once:
                break
            sleep_interruptible(cfg["check_interval_s"])
            continue
        camera_misses = 0

        ema = raw if ema is None else cfg["ema_alpha"] * raw + (1 - cfg["ema_alpha"]) * ema
        curve = raw_to_pct(ema, cfg)

        cur = get_brightness(cfg)

        # ---- detect manual user adjustment ----
        if cur is not None and last_set is not None:
            if abs(cur - last_set) > cfg["manual_override_threshold"]:
                baseline += cur - last_set
                last_set = cur
                LOG.info("manual override detected: shift=%+.1f%%", baseline)

        target = max(cfg["min_pct"],
                     min(cfg["max_pct"], curve + baseline))

        if last_set is None or abs(target - last_set) >= cfg["hysteresis_pct"]:
            start = cur if cur is not None else (
                last_set if last_set is not None else target)
            LOG.info("raw=%5.1f ema=%5.1f curve=%5.1f%% shift=%+.1f target=%.1f%%",
                     raw, ema, curve, baseline, target)
            fade_to(cfg, start, target)
            last_set = target
        else:
            LOG.debug("no change: raw=%5.1f ema=%5.1f target=%.1f%% (last=%s)",
                      raw, ema, target, last_set)

        if args.once:
            break
        sleep_interruptible(cfg["check_interval_s"])

    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
