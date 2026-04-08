#!/usr/bin/env python3
"""
water-popup - popup and tray UI for the PKD hydration tracker on KDE Plasma.

Modes:
  gui     Show the manual popup.
  remind  Show the reminder popup.
  tray    Start the persistent system tray app.
"""

import atexit
import json
import logging
import math
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

TARGET_OZ = 101
DAY_START_HOUR = 7
DAY_START_MINUTE = 0
DAY_END_HOUR = 21
DAY_END_MINUTE = 0
USE_24H = False
DATA_DIR = Path.home() / ".local" / "share" / "water"
PID_FILE = DATA_DIR / "tray.pid"
TRAY_LOG_FILE = DATA_DIR / "tray.log"
ICON_FILE = Path.home() / "bin" / "water-icon.svg"
_DEFAULT_QUICK_ADD = [1, 2, 3, 4, 8, 12, 16, 20, 24]
_QUICK_ADD_ALL_VALUES = list(_DEFAULT_QUICK_ADD)
_QUICK_ADD_ALL_ENABLED = [True] * len(_DEFAULT_QUICK_ADD)
QUICK_ADD = list(_DEFAULT_QUICK_ADD)
AIM_FOR_ODD = True
AIM_FOR_EVEN = True
REMINDER_INPUT_GUARD_MS = 1400
MIN_FUTURE_HOUR_OZ = 1.0
LABELS = [
    "1 oz\n\u215b cup",
    "2 oz\n\u00bc cup",
    "3 oz\n\u215c cup",
    "4 oz\n\u00bd cup",
    "8 oz\n1 cup",
    "12 oz\n1\u00bd cups",
    "16 oz\n2 cups",
    "20 oz\n2\u00bd cups",
    "24 oz\n3 cups",
]

CONFIG_FILE = DATA_DIR / "config.json"


def _apply_config():
    """Load saved settings into module-level constants."""
    global TARGET_OZ, DAY_START_HOUR, DAY_START_MINUTE, DAY_END_HOUR, DAY_END_MINUTE, USE_24H
    global QUICK_ADD, _QUICK_ADD_ALL_VALUES, _QUICK_ADD_ALL_ENABLED, AIM_FOR_ODD, AIM_FOR_EVEN
    if not CONFIG_FILE.exists():
        return
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if "target_oz" in data:
            TARGET_OZ = max(1, int(data["target_oz"]))
        if "day_start_hour" in data:
            DAY_START_HOUR = max(0, min(23, int(data["day_start_hour"])))
        if "day_start_minute" in data:
            DAY_START_MINUTE = max(0, min(59, int(data["day_start_minute"])))
        if "day_end_hour" in data:
            DAY_END_HOUR = max(0, min(23, int(data["day_end_hour"])))
        if "day_end_minute" in data:
            DAY_END_MINUTE = max(0, min(59, int(data["day_end_minute"])))
        if "use_24h" in data:
            USE_24H = bool(data["use_24h"])
        # Load all-values; migrate from legacy 'quick_add' key if needed
        values_raw = data.get("quick_add_values") or data.get("quick_add")
        if isinstance(values_raw, list) and len(values_raw) == len(_DEFAULT_QUICK_ADD):
            validated = []
            for v in values_raw:
                try:
                    fv = float(v)
                    validated.append(fv if fv > 0 else _DEFAULT_QUICK_ADD[len(validated)])
                except (TypeError, ValueError):
                    validated.append(_DEFAULT_QUICK_ADD[len(validated)])
            _QUICK_ADD_ALL_VALUES = validated
        enabled_raw = data.get("quick_add_enabled")
        if isinstance(enabled_raw, list) and len(enabled_raw) == len(_DEFAULT_QUICK_ADD):
            _QUICK_ADD_ALL_ENABLED = [bool(e) for e in enabled_raw]
        QUICK_ADD = [
            v for v, en in zip(_QUICK_ADD_ALL_VALUES, _QUICK_ADD_ALL_ENABLED) if en
        ] or list(_DEFAULT_QUICK_ADD)
        if "aim_for_odd" in data:
            AIM_FOR_ODD = bool(data["aim_for_odd"])
        if "aim_for_even" in data:
            AIM_FOR_EVEN = bool(data["aim_for_even"])
    except (OSError, ValueError, KeyError):
        pass


def save_config(target_oz, day_start_hour, day_start_minute, day_end_hour, day_end_minute, use_24h,
                quick_add_values=None, quick_add_enabled=None, aim_for_odd=True, aim_for_even=True):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({
            "target_oz": target_oz,
            "day_start_hour": day_start_hour,
            "day_start_minute": day_start_minute,
            "day_end_hour": day_end_hour,
            "day_end_minute": day_end_minute,
            "use_24h": use_24h,
            "quick_add_values": quick_add_values if quick_add_values is not None else list(_QUICK_ADD_ALL_VALUES),
            "quick_add_enabled": quick_add_enabled if quick_add_enabled is not None else list(_QUICK_ADD_ALL_ENABLED),
            "aim_for_odd": aim_for_odd,
            "aim_for_even": aim_for_even,
        }),
        encoding="utf-8",
    )


_apply_config()


def today_string(now=None):
    moment = now or datetime.now()
    return moment.strftime("%Y-%m-%d")


def current_log_file(now=None):
    return DATA_DIR / f"{today_string(now)}.log"


def format_oz(oz):
    if float(oz).is_integer():
        return str(int(oz))
    return f"{oz:.1f}".rstrip("0").rstrip(".")


def read_entries(now=None):
    log_file = current_log_file(now)
    if not log_file.exists():
        return []

    entries = []
    for line in log_file.read_text().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            entries.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    return entries


def last_entry_time(now=None):
    entries = read_entries(now)
    if not entries:
        return None

    timestamp = entries[-1][0]
    try:
        last_time = datetime.strptime(timestamp, "%H:%M:%S")
    except ValueError:
        return None
    moment = now or datetime.now()
    return moment.replace(
        hour=last_time.hour,
        minute=last_time.minute,
        second=last_time.second,
        microsecond=0,
    )


def total_oz(now=None):
    return sum(amount for _, amount in read_entries(now))


def hourly_oz(now=None):
    """Returns {hour: total_oz} for today's log entries."""
    by_hour = {}
    for time_str, oz in read_entries(now):
        try:
            hour = int(time_str.split(":")[0])
            by_hour[hour] = by_hour.get(hour, 0) + oz
        except (ValueError, IndexError):
            pass
    return by_hour


def total_oz_before_hour(hour, now=None):
    total = 0.0
    for time_str, oz in read_entries(now):
        try:
            entry_hour = int(time_str.split(":")[0])
        except (ValueError, IndexError):
            continue
        if entry_hour < hour:
            total += oz
    return total


def log_oz(oz, now=None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now()
    if within_active_hours(moment):
        commit_missing_hourly_targets(moment)
    with current_log_file(moment).open("a", encoding="utf-8") as handle:
        handle.write(f"{moment.strftime('%H:%M:%S')} {format_oz(oz)}\n")


def expected_oz(now=None):
    moment = now or datetime.now()
    hour = moment.hour
    minute = moment.minute
    hours = active_hours_list()
    if not hours:
        return 0
    if not within_active_hours(moment):
        # Before window starts → 0; after it ends → full target
        # Determine by checking if we're "past" the end in the day order
        if DAY_START_HOUR < DAY_END_HOUR:
            return 0 if hour < DAY_START_HOUR else TARGET_OZ
        # Overnight: hours between end and start are the "off" period
        return TARGET_OZ if DAY_END_HOUR <= hour < DAY_START_HOUR else 0
    day_minutes = len(hours) * 60
    # Position within the ordered active hours list
    if hour in hours:
        idx = hours.index(hour)
    else:
        idx = 0
    elapsed = idx * 60 + minute
    return int(TARGET_OZ * elapsed / day_minutes)


def distribute_hourly_targets(hours, total_oz, current_hour=None, current_hour_fraction=1.0):
    """Spread remaining ounces steadily across the available hours.

    Future full hours keep a small floor whenever enough ounces remain so the
    late-day plan does not collapse into zeros after an early push.
    """
    if not hours or total_oz <= 0:
        return {}

    slot_weights = {}
    floor_targets = {}
    for hour in hours:
        slot_fraction = current_hour_fraction if hour == current_hour else 1.0
        slot_weights[hour] = max(0.0, slot_fraction)
        floor_targets[hour] = 0.0 if hour == current_hour else MIN_FUTURE_HOUR_OZ

    floor_total = sum(floor_targets.values())
    if floor_total > total_oz and floor_total > 0:
        scale = total_oz / floor_total
        floor_targets = {hour: value * scale for hour, value in floor_targets.items()}
        floor_total = total_oz

    remaining_oz = max(0.0, total_oz - floor_total)
    total_weight = sum(slot_weights.values()) or 1.0
    return {
        hour: floor_targets[hour] + remaining_oz * slot_weights[hour] / total_weight
        for hour in hours
    }


def hourly_expected_steady():
    """Baseline steady oz targets per hour, summing to TARGET_OZ."""
    return distribute_hourly_targets(active_hours_list(), TARGET_OZ)


def adjusted_future_expected(now, drunk):
    """Redistribute remaining oz over current+future hours with a steady pace."""
    current_hour = now.hour
    remaining_oz = max(0.0, TARGET_OZ - drunk)
    hours = active_hours_list()
    if current_hour in hours:
        future_hours = hours[hours.index(current_hour):]
    else:
        future_hours = []
    if not future_hours:
        return {}

    remaining_hour_fraction = max(
        0.0,
        1.0 - ((now.minute * 60 + now.second) / 3600),
    )
    return distribute_hourly_targets(
        future_hours,
        remaining_oz,
        current_hour=current_hour,
        current_hour_fraction=remaining_hour_fraction,
    )


def _committed_targets_path(now=None):
    return DATA_DIR / f"{today_string(now)}.targets.json"


def load_committed_targets(now=None):
    """Return {hour: oz} that were saved when each hour started."""
    path = _committed_targets_path(now)
    if not path.exists():
        return {}
    try:
        return {int(k): float(v) for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError, KeyError):
        return {}


def commit_hourly_targets(now=None, drunk=None):
    """Backward-compatible wrapper for committing hour targets."""
    return commit_missing_hourly_targets(now)


def commit_missing_hourly_targets(now=None):
    """Persist hour-start targets for any missing hours up to the current hour."""
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return load_committed_targets(moment)

    committed = load_committed_targets(moment)
    hours = active_hours_list()
    current_hour = moment.hour
    if current_hour in hours:
        hours_to_commit = hours[: hours.index(current_hour) + 1]
    else:
        hours_to_commit = []
    changed = False

    for hour in hours_to_commit:
        if hour in committed:
            continue
        hour_start = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
        drunk_before_hour = total_oz_before_hour(hour, moment)
        adj = adjusted_future_expected(hour_start, drunk_before_hour)
        committed[hour] = round(adj.get(hour, 0.0), 2)
        changed = True

    if changed:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _committed_targets_path(moment).write_text(
            json.dumps({str(k): v for k, v in sorted(committed.items())})
        )

    return committed


def rounded_distribution(values, target_total=None):
    """Round values to integers while preserving the rounded total."""
    if not values:
        return {}

    if target_total is None:
        target_total = int(round(sum(values.values())))

    rounded = {key: int(math.floor(value)) for key, value in values.items()}
    remainder = max(0, target_total - sum(rounded.values()))

    ranked_keys = sorted(
        values,
        key=lambda key: (values[key] - math.floor(values[key]), key),
        reverse=True,
    )

    for key in ranked_keys[:remainder]:
        rounded[key] += 1

    return rounded


def round_oz_int(oz):
    return int(math.floor(oz + 0.5))


def current_future_base_targets(now=None):
    """Return baseline targets for the current and future hours only.

    Past targets stay frozen. The current and future targets come from the plan
    that was in effect at the start of the current hour.
    """
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return {}

    hour_start = moment.replace(minute=0, second=0, microsecond=0)
    drunk_before_hour = total_oz_before_hour(moment.hour, moment)
    return adjusted_future_expected(hour_start, drunk_before_hour)


def adjusted_current_future_targets(now=None):
    """Return current/future targets adjusted only for current-hour overage.

    Past hour targets remain frozen. If the current hour has already exceeded
    its target, that overage is removed from future hours proportionally so the
    future bars still sum to the day's remaining ounces.
    """
    moment = now or datetime.now()
    base_targets = current_future_base_targets(moment)
    if not base_targets:
        return {}, {}

    current_target = max(0.0, base_targets.get(moment.hour, 0.0))
    consumed_this_hour = hourly_oz(moment).get(moment.hour, 0.0)
    overage = max(0.0, consumed_this_hour - current_target)

    future_base = {hour: value for hour, value in base_targets.items() if hour > moment.hour}
    future_total = max(0.0, sum(future_base.values()) - overage)

    if future_base and future_total > 0:
        total_future_base = sum(future_base.values()) or 1.0
        adjusted_future = {
            hour: future_total * future_base[hour] / total_future_base
            for hour in future_base
        }
    else:
        adjusted_future = {}

    adjusted_targets = {moment.hour: current_target, **adjusted_future}
    target_total = int(round(sum(adjusted_targets.values())))
    return adjusted_targets, rounded_distribution(adjusted_targets, target_total=target_total)


def live_hour_target(now=None):
    """Current hour target after redistributing carried deficit forward."""
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return 0
    _, labels = adjusted_current_future_targets(moment)
    return max(0, labels.get(moment.hour, 0))


def committed_expected_oz(now=None):
    moment = now or datetime.now()
    if not within_active_hours(moment):
        if DAY_START_HOUR < DAY_END_HOUR:
            return 0 if moment.hour < DAY_START_HOUR else TARGET_OZ
        return TARGET_OZ if DAY_END_HOUR <= moment.hour < DAY_START_HOUR else 0

    committed = commit_missing_hourly_targets(moment)
    baseline = hourly_expected_steady()
    hours = active_hours_list()
    current_hour = moment.hour
    if current_hour in hours:
        hours_so_far = hours[: hours.index(current_hour) + 1]
    else:
        hours_so_far = []
    return sum(
        max(0, round_oz_int(committed.get(hour, baseline.get(hour, 0))))
        for hour in hours_so_far
    )


def current_hour_remaining_target(now=None):
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return 0

    consumed_this_hour = hourly_oz(moment).get(moment.hour, 0)
    remaining = live_hour_target(moment) - consumed_this_hour
    return max(0, round_oz_int(remaining))


def current_hour_chunks_remaining(now=None):
    remaining_target = current_hour_remaining_target(now)
    if remaining_target <= 0:
        return 0
    pkd_max_sip = 8
    return math.ceil(remaining_target / pkd_max_sip)


def current_hour_reminder_interval(now=None):
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return 0

    chunks_remaining = current_hour_chunks_remaining(moment)
    if chunks_remaining <= 0:
        return 0

    hour_end = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    remaining_minutes = max(1.0, (hour_end - moment).total_seconds() / 60)
    return max(20, math.ceil(remaining_minutes / (chunks_remaining + 1)))


def fallback_next_reminder_minutes(now=None):
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return 0

    interval = current_hour_reminder_interval(moment)
    if interval > 0:
        return interval
    if moment.hour + 1 >= DAY_END_HOUR:
        return 0

    next_hour = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1, math.ceil((next_hour - moment).total_seconds() / 60))


def _apply_aim_parity(oz):
    """Round oz to the nearest allowed parity (odd / even / both)."""
    if (AIM_FOR_ODD and AIM_FOR_EVEN) or (not AIM_FOR_ODD and not AIM_FOR_EVEN):
        return oz  # no filtering
    v = max(1, int(round(oz)))
    if AIM_FOR_ODD and not AIM_FOR_EVEN:
        if v % 2 == 1:
            return v
        # v is even; prefer v-1 if ≥ 1, else v+1
        return v - 1 if v - 1 >= 1 else v + 1
    # AIM_FOR_EVEN and not AIM_FOR_ODD
    if v % 2 == 0:
        return max(2, v)
    # v is odd; prefer v-1 if ≥ 2, else v+1
    return v - 1 if v - 1 >= 2 else v + 1


def suggested_next_oz(now, drunk, interval_minutes=None):
    """How many oz to aim for in the next reminder interval.

    Based on the adjusted current hour target after carrying deficit forward.
    Uses the actual remaining amount for the current adjusted hour target.
    """
    if not within_active_hours(now) or drunk >= TARGET_OZ:
        return 0

    remaining_oz = max(0.0, TARGET_OZ - drunk)
    hour_remaining = current_hour_remaining_target(now)
    if hour_remaining <= 0:
        return 0
    raw = max(1, min(hour_remaining, int(math.ceil(remaining_oz))))
    return _apply_aim_parity(raw)


def reminder_interval_minutes(now=None, drunk=None, expected=None):
    moment = now or datetime.now()
    if not within_active_hours(moment):
        return 0

    current_drunk = total_oz(moment) if drunk is None else drunk
    current_expected = committed_expected_oz(moment) if expected is None else expected
    deficit = max(0, current_expected - current_drunk)

    if deficit >= 20:
        interval = 20
    elif deficit >= 10:
        interval = 30
    elif deficit > 0:
        interval = 40
    else:
        interval = 50

    # Slow reminders down near the end of the active window.
    hours = active_hours_list()
    if hours and moment.hour in hours:
        idx = hours.index(moment.hour)
        remaining_hours = len(hours) - idx
        if remaining_hours <= 1:
            return max(45, interval)
        if remaining_hours <= 3:
            return max(30, interval)
    return interval


def active_hours_list():
    """Ordered list of calendar hours in the active window (handles overnight spans)."""
    if DAY_START_HOUR < DAY_END_HOUR:
        return list(range(DAY_START_HOUR, DAY_END_HOUR))
    # Overnight: e.g. start=21, end=8 → [21,22,23,0,1,2,3,4,5,6,7]
    return list(range(DAY_START_HOUR, 24)) + list(range(0, DAY_END_HOUR))


def active_window_label():
    """Human-readable string like '7 AM - 9 PM' or '9 PM - 8 AM'."""
    def fmt(h):
        suffix = "AM" if h < 12 else "PM"
        display = h % 12 or 12
        return f"{display} {suffix}"
    return f"{fmt(DAY_START_HOUR)} - {fmt(DAY_END_HOUR)}"


def within_active_hours(now=None):
    moment = now or datetime.now()
    t = moment.hour * 60 + moment.minute
    s = DAY_START_HOUR * 60 + DAY_START_MINUTE
    e = DAY_END_HOUR * 60 + DAY_END_MINUTE
    if s < e:
        return s <= t < e
    # Overnight span
    return t >= s or t < e


def pace_summary(drunk, expected):
    pace_diff = int(drunk) - expected
    if int(drunk) >= TARGET_OZ:
        return "pace_good", "Goal reached! Great work."
    if pace_diff >= 0:
        return "pace_good", f"On pace (+{pace_diff} oz ahead)"
    if pace_diff >= -10:
        return "pace_warn", f"Slightly behind ({abs(pace_diff)} oz) - sip soon"
    return "pace_bad", f"Behind pace by {abs(pace_diff)} oz - drink now"


def build_status_lines(now=None, next_reminder_at=None):
    moment = now or datetime.now()
    drunk = total_oz(moment)
    expected = committed_expected_oz(moment)
    remaining = max(0, TARGET_OZ - drunk)
    _, pace_text = pace_summary(drunk, expected)
    lines = [
        f"{format_oz(drunk)} / {TARGET_OZ} oz",
        pace_text,
        f"Remaining: {format_oz(remaining)} oz",
    ]
    if within_active_hours(moment) and drunk < TARGET_OZ:
        if next_reminder_at and next_reminder_at > moment:
            minutes = max(1, math.ceil((next_reminder_at - moment).total_seconds() / 60))
            lines.append(f"Next reminder in ~{minutes} min")
        else:
            minutes = fallback_next_reminder_minutes(moment)
            if minutes > 0:
                lines.append(f"Next reminder in ~{minutes} min")
    elif drunk >= TARGET_OZ:
        lines.append("Reminders paused: daily goal reached")
    else:
        lines.append(f"Reminders pause outside {active_window_label()}")
    return lines


def next_reminder_label_text(now, drunk, next_reminder_at=None):
    if next_reminder_at and next_reminder_at > now:
        next_minutes = max(1, math.ceil((next_reminder_at - now).total_seconds() / 60))
    else:
        next_minutes = fallback_next_reminder_minutes(now)

    sip = suggested_next_oz(now, drunk, interval_minutes=next_minutes)
    if sip > 0:
        return f"   Next reminder: ~{next_minutes} min  →  aim for {sip} oz", sip

    return f"   Next reminder: ~{next_minutes} min  →  current hour target met", 0


def format_cup_equivalent(oz):
    cups = oz / 8
    total_eighths = int(round(cups * 8))
    if math.isclose(cups, total_eighths / 8, abs_tol=1e-9):
        whole_cups, remaining_eighths = divmod(total_eighths, 8)
        fractions = {
            1: "⅛",
            2: "¼",
            3: "⅜",
            4: "½",
            5: "⅝",
            6: "¾",
            7: "⅞",
        }
        if remaining_eighths and whole_cups:
            amount = f"{whole_cups}{fractions[remaining_eighths]}"
        elif remaining_eighths:
            amount = fractions[remaining_eighths]
        else:
            amount = str(whole_cups)
        unit = "cup" if total_eighths <= 8 else "cups"
        return f"{amount} {unit}"

    unit = "cup" if math.isclose(cups, 1.0, abs_tol=1e-9) else "cups"
    return f"{format_oz(cups)} {unit}"


def suggested_button_label(oz):
    return f"{format_oz(oz)} oz\n{format_cup_equivalent(oz)}"


def read_pid():
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_is_running(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_pid():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid():
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    logging.basicConfig(
        filename=str(TRAY_LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def is_wayland_session():
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    return session_type.lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


try:
    from PyQt5.QtCore import QEvent, QRect, Qt, QTimer
    from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap
    from PyQt5.QtWidgets import (
        QAction,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMenu,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QLineEdit,
        QScrollArea,
        QSpinBox,
        QStyle,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )

    HAS_QT = True
except ImportError:
    HAS_QT = False


STYLE = """
QWidget#root {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 12px;
}
QLabel#title {
    color: #58a6ff;
    font-size: 15px;
    font-weight: bold;
    font-family: 'JetBrains Mono', 'Noto Mono', monospace;
}
QLabel#status {
    color: #8b949e;
    font-size: 11px;
    font-family: 'JetBrains Mono', 'Noto Mono', monospace;
}
QLabel#pace_good  { color: #3fb950; font-size: 12px; font-family: monospace; }
QLabel#pace_warn  { color: #d29922; font-size: 12px; font-family: monospace; }
QLabel#pace_bad   { color: #f85149; font-size: 12px; font-family: monospace; }
QLabel#evening    { color: #d29922; font-size: 11px; font-family: monospace; }
QProgressBar {
    border: 1px solid #30363d;
    border-radius: 6px;
    background: #161b22;
    height: 14px;
    text-align: center;
    color: #c9d1d9;
    font-size: 10px;
    font-family: monospace;
}
QProgressBar::chunk {
    border-radius: 5px;
}
QPushButton#add_btn {
    background: #161b22;
    color: #58a6ff;
    border: 1px solid #30363d;
    border-radius: 8px;
    font-family: 'JetBrains Mono', 'Noto Mono', monospace;
    font-size: 11px;
    padding: 8px 4px;
    min-width: 58px;
}
QPushButton#add_btn:hover {
    background: #1f6feb;
    color: #ffffff;
    border-color: #58a6ff;
}
QPushButton#add_btn:pressed {
    background: #388bfd;
}
QPushButton#dismiss_btn {
    background: transparent;
    color: #8b949e;
    border: 1px solid #30363d;
    border-radius: 6px;
    font-size: 11px;
    padding: 5px 14px;
}
QPushButton#dismiss_btn:hover {
    color: #c9d1d9;
    border-color: #8b949e;
}
QFrame#divider {
    color: #30363d;
}
"""


if HAS_QT:

    class AmountButton(QPushButton):
        _COLOR_NORMAL = "#58a6ff"
        _COLOR_HOVER  = "#ffffff"

        def __init__(self, label, parent=None):
            super().__init__("", parent)
            self.setObjectName("add_btn")
            self.setMinimumHeight(62)

            primary_text, secondary_text = (label.split("\n", 1) + [""])[:2]

            layout = QVBoxLayout(self)
            layout.setContentsMargins(4, 5, 4, 5)
            layout.setSpacing(0)

            self.primary_label = QLabel(primary_text, self)
            self.primary_label.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
            self.primary_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            primary_font = QFont("JetBrains Mono")
            primary_font.setPointSize(13)
            self.primary_label.setFont(primary_font)
            layout.addWidget(self.primary_label)

            self.secondary_label = QLabel(secondary_text, self)
            self.secondary_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.secondary_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            secondary_font = QFont("JetBrains Mono")
            secondary_font.setPointSize(13)
            self.secondary_label.setFont(secondary_font)
            layout.addWidget(self.secondary_label)

            if not secondary_text:
                self.secondary_label.hide()

            self._apply_label_color(self._COLOR_NORMAL)

        def _apply_label_color(self, color):
            style = f"color: {color}; background: transparent;"
            self.primary_label.setStyleSheet(style)
            self.secondary_label.setStyleSheet(style)

        def enterEvent(self, event):
            super().enterEvent(event)
            self._apply_label_color(self._COLOR_HOVER)

        def leaveEvent(self, event):
            super().leaveEvent(event)
            self._apply_label_color(self._COLOR_NORMAL)

    class DailyBarChart(QWidget):
        """Bar chart showing hourly expected vs consumed over the active day."""

        def __init__(self, now=None, parent=None):
            super().__init__(parent)
            self.snapshot = now or datetime.now()
            self.hourly = hourly_oz(self.snapshot)
            self.setFixedHeight(150)
            self.setMinimumWidth(200)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)

            w = self.width()
            h = self.height()
            left_m, right_m, top_m, bottom_m = 4, 4, 18, 24
            chart_w = w - left_m - right_m
            chart_h = h - top_m - bottom_m

            hours = active_hours_list()
            total_hours = len(hours)
            current_hour = self.snapshot.hour
            drunk = sum(self.hourly.values())

            hist_exp = hourly_expected_steady()
            committed = load_committed_targets(self.snapshot)

            past_exp = {
                h: committed.get(h, hist_exp.get(h, 0))
                for h in (hours[:hours.index(current_hour)] if current_hour in hours else hours)
            }
            current_future_exp, current_future_labels = adjusted_current_future_targets(self.snapshot)

            combined_exp = {**past_exp, **current_future_exp}
            past_labels = rounded_distribution(past_exp, target_total=int(round(sum(past_exp.values()))))
            expected_labels = {**past_labels, **current_future_labels}

            all_exp = list(combined_exp.values())
            max_exp = max(all_exp) if all_exp else 8.0
            max_oz = max(max_exp, 8.0) * 2.2

            slot_w = chart_w / len(hours)
            bar_w = max(3, slot_w * 0.78)
            half_w = max(2, int(bar_w * 0.45))

            # Background + baseline
            painter.fillRect(0, 0, w, h, QColor("#0d1117"))
            base_y = top_m + chart_h
            painter.setPen(QPen(QColor("#30363d"), 1))
            painter.drawLine(left_m, base_y, w - right_m, base_y)

            # Hour-boundary tick marks on the x-axis.
            tick_pen = QPen(QColor("#4b5563"), 1)
            painter.setPen(tick_pen)
            tick_top = base_y - 4
            tick_bottom = base_y + 4
            for i in range(1, len(hours)):
                tick_x = int(left_m + i * slot_w)
                painter.drawLine(tick_x, tick_top, tick_x, tick_bottom)

            label_font = QFont("Hack")
            label_font.setPixelSize(9)

            # --- Expected bars and consumed totals ---
            for i, hour in enumerate(hours):
                slot_x = left_m + i * slot_w
                bar_x = slot_x + (slot_w - bar_w) / 2
                is_future = hour > current_hour
                if current_hour in hours:
                    idx_cur = hours.index(current_hour)
                    idx_h = hours.index(hour) if hour in hours else -1
                    is_future = idx_h > idx_cur
                    is_current = idx_h == idx_cur
                else:
                    is_current = False

                exp_oz = combined_exp.get(hour, 0)
                if is_future:
                    exp_color = QColor("#0d2640")
                elif is_current:
                    exp_color = QColor("#1a4060")
                else:
                    exp_color = QColor("#1a3a5c")

                actual_h = self.hourly.get(hour, 0)
                exp_px = max(2, int(chart_h * min(exp_oz, max_oz) / max_oz)) if exp_oz > 0 else 0
                act_px = max(2, int(chart_h * min(actual_h, max_oz) / max_oz)) if actual_h > 0 else 0

                if exp_px > 0:
                    painter.fillRect(int(bar_x), base_y - exp_px, half_w, exp_px, exp_color)
                if act_px > 0:
                    painter.fillRect(int(bar_x + half_w + 1), base_y - act_px, half_w, act_px, QColor("#56d364"))

                if exp_px > 0:
                    label_value = str(expected_labels.get(hour, round(exp_oz)))
                    legend_font = QFont("Hack")
                    legend_font.setPixelSize(8)
                    painter.setFont(legend_font)
                    painter.setPen(QColor("#8b949e"))
                    fm2 = painter.fontMetrics()
                    label_x = int(bar_x + (half_w - fm2.horizontalAdvance(label_value)) / 2)
                    label_y = base_y - exp_px - 6
                    if label_y < top_m + fm2.height():
                        label_y = top_m + fm2.height()
                    painter.drawText(label_x, label_y, label_value)

                if act_px > 0:
                    actual_label = str(round(actual_h))
                    legend_font = QFont("Hack")
                    legend_font.setPixelSize(8)
                    painter.setFont(legend_font)
                    painter.setPen(QColor("#56d364"))
                    fm3 = painter.fontMetrics()
                    actual_x = int(bar_x + half_w + 1 + (half_w - fm3.horizontalAdvance(actual_label)) / 2)
                    actual_y = base_y - act_px - 6
                    if actual_y < top_m + fm3.height():
                        actual_y = top_m + fm3.height()
                    painter.drawText(actual_x, actual_y, actual_label)

                # Current hour marker
                if is_current:
                    painter.setPen(QPen(QColor("#58a6ff"), 1))
                    cx = int(slot_x + slot_w / 2)
                    painter.drawLine(cx, top_m, cx, base_y)

                # Hour labels every 2 hours
                painter.setFont(label_font)
                painter.setPen(QColor("#6e7681"))
                fm = painter.fontMetrics()
                if (hour - DAY_START_HOUR) % 2 == 0:
                    suffix = "a" if hour < 12 else "p"
                    lbl = f"{hour % 12 or 12}{suffix}"
                    lx = int(slot_x + slot_w / 2 - fm.horizontalAdvance(lbl) / 2)
                    painter.drawText(lx, h - 6, lbl)

            # --- Individual drink line + dots ---
            entries = read_entries(self.snapshot)
            pts = []
            for time_str, oz in entries:
                try:
                    parts = time_str.split(":")
                    frac_hour = int(parts[0]) + int(parts[1]) / 60
                    if within_active_hours(self.snapshot.replace(hour=int(frac_hour), minute=int((frac_hour % 1) * 60))):
                        slot_idx = hours.index(int(frac_hour)) if int(frac_hour) in hours else 0
                        x_px = left_m + (slot_idx + frac_hour % 1) / total_hours * chart_w
                        y_px = base_y - max(3, int(chart_h * min(oz, max_oz) / max_oz))
                        pts.append((int(x_px), y_px, oz))
                except (ValueError, IndexError):
                    pass

            if len(pts) >= 2:
                pen = QPen(QColor("#56d364"), 1.5)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                for j in range(len(pts) - 1):
                    painter.drawLine(pts[j][0], pts[j][1], pts[j + 1][0], pts[j + 1][1])

            dot_r = 4
            for x_px, y_px, oz in pts:
                painter.setBrush(QBrush(QColor("#56d364")))
                painter.setPen(QPen(QColor("#0d1117"), 1))
                painter.drawEllipse(x_px - dot_r, y_px - dot_r, dot_r * 2, dot_r * 2)

            # Legend
            legend_font = QFont("Hack")
            legend_font.setPixelSize(9)
            painter.setFont(legend_font)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#1a3a5c")))
            painter.drawRect(left_m, 2, 8, 8)
            painter.setPen(QColor("#6e7681"))
            painter.drawText(left_m + 11, 10, "target")
            painter.setBrush(QBrush(QColor("#56d364")))
            painter.setPen(QPen(QColor("#0d1117"), 1))
            painter.drawEllipse(left_m + 55, 3, 7, 7)
            painter.setPen(QColor("#6e7681"))
            painter.drawText(left_m + 65, 10, "consumed")

            painter.end()

    class WaterPopup(QWidget):
        def __init__(
            self,
            remind_mode=False,
            on_log=None,
            on_snooze=None,
            on_skip=None,
            on_configure=None,
            next_reminder_at=None,
        ):
            super().__init__()
            self.remind_mode = remind_mode
            self.on_log = on_log
            self.on_snooze = on_snooze
            self.on_skip = on_skip
            self.on_configure = on_configure
            self.next_reminder_at = next_reminder_at
            self.input_guard_active = False
            self._build_ui()
            self._position_window()

        def _build_ui(self):
            self.setObjectName("root")
            self.setWindowTitle("Water")
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground, False)
            self.setStyleSheet(STYLE)
            self.setFixedWidth(420)

            snapshot_time = datetime.now()
            if within_active_hours(snapshot_time):
                commit_missing_hourly_targets(snapshot_time)
            drunk = total_oz(snapshot_time)
            expected = committed_expected_oz(snapshot_time)
            remaining = max(0, TARGET_OZ - drunk)

            root = QVBoxLayout(self)
            root.setContentsMargins(18, 16, 18, 16)
            root.setSpacing(10)

            title_row = QHBoxLayout()
            title = QLabel("Water")
            title.setObjectName("title")
            clock_label = QLabel(snapshot_time.strftime("%-I:%M %p"))
            clock_label.setObjectName("status")
            clock_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            gear_button = QPushButton()
            _gear_icon = QIcon.fromTheme("configure", QIcon.fromTheme("preferences-system"))
            if not _gear_icon.isNull():
                gear_button.setIcon(_gear_icon)
            else:
                gear_button.setText("…")
            gear_button.setObjectName("dismiss_btn")
            gear_button.setFixedSize(28, 28)
            gear_button.setToolTip("Settings")
            gear_button.clicked.connect(self._open_configure)
            title_row.addWidget(title)
            title_row.addWidget(clock_label)
            title_row.addWidget(gear_button)
            root.addLayout(title_row)

            percent = min(100, int(drunk * 100 / TARGET_OZ))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(percent)
            bar.setFormat(f"{format_oz(drunk)} / {TARGET_OZ} oz  ({percent}%)")
            bar.setFixedHeight(20)
            if percent >= 75:
                chunk_color = "#3fb950"
            elif percent >= 40:
                chunk_color = "#d29922"
            else:
                chunk_color = "#f85149"
            bar.setStyleSheet(bar.styleSheet() + f"QProgressBar::chunk {{ background: {chunk_color}; }}")
            self.bar = bar
            root.addWidget(bar)

            chart_label = QLabel("Hourly: expected vs consumed")
            chart_label.setObjectName("status")
            root.addWidget(chart_label)
            self.chart = DailyBarChart(now=snapshot_time)
            root.addWidget(self.chart)

            pace_class, pace_text = pace_summary(drunk, expected)
            pace_label = QLabel(pace_text)
            pace_label.setObjectName(pace_class)
            self.pace_label = pace_label
            root.addWidget(pace_label)

            remaining_label = QLabel(f"   {format_oz(remaining)} oz remaining to goal")
            remaining_label.setObjectName("status")
            self.remaining_label = remaining_label
            root.addWidget(remaining_label)

            hours = active_hours_list()
            near_end = hours[-2:] if len(hours) >= 2 else hours
            if snapshot_time.hour in near_end:
                evening_label = QLabel("Near end of active window - small sips to protect sleep")
                evening_label.setObjectName("evening")
                root.addWidget(evening_label)

            if within_active_hours(snapshot_time) and drunk < TARGET_OZ:
                next_text, sip = next_reminder_label_text(
                    snapshot_time,
                    drunk,
                    next_reminder_at=self.next_reminder_at,
                )
                next_label = QLabel(next_text)
            elif drunk >= TARGET_OZ:
                next_label = QLabel("   Reminders paused: goal reached")
                sip = 0
            else:
                next_label = QLabel(f"   Reminders pause outside {active_window_label()}")
                sip = 0
            next_label.setObjectName("status")
            self.next_label = next_label
            root.addWidget(next_label)

            self.quick_buttons = []
            divider = QFrame()
            divider.setObjectName("divider")
            divider.setFrameShape(QFrame.HLine)
            root.addWidget(divider)

            button_label = QLabel("Log intake:")
            button_label.setObjectName("status")
            root.addWidget(button_label)

            self._btn_row_1 = QHBoxLayout()
            self._btn_row_1.setSpacing(6)
            self._btn_row_2 = QHBoxLayout()
            self._btn_row_2.setSpacing(6)
            self._btn_row_3 = QHBoxLayout()
            self._btn_row_3.setSpacing(6)
            self._rendered_quick_add = list(QUICK_ADD)

            for index, oz in enumerate(QUICK_ADD):
                button = self._make_add_button(oz, highlighted=(oz == sip))
                self.quick_buttons.append(button)
                if index < 3:
                    self._btn_row_1.addWidget(button)
                elif index < 6:
                    self._btn_row_2.addWidget(button)
                else:
                    self._btn_row_3.addWidget(button)

            root.addLayout(self._btn_row_1)
            root.addLayout(self._btn_row_2)
            root.addLayout(self._btn_row_3)

            divider_two = QFrame()
            divider_two.setObjectName("divider")
            divider_two.setFrameShape(QFrame.HLine)
            root.addWidget(divider_two)

            manual_label = QLabel("Custom amount:")
            manual_label.setObjectName("status")
            root.addWidget(manual_label)

            self._manual_row = QHBoxLayout()
            self._manual_row.setSpacing(6)
            if sip > 0 and sip not in QUICK_ADD:
                suggested_button = self._make_add_button(sip, highlighted=True)
                self.quick_buttons.append(suggested_button)
                self._manual_row.addWidget(suggested_button)
            self.manual_input = QLineEdit()
            self.manual_input.setPlaceholderText("Enter oz")
            self.manual_input.setFixedWidth(86)
            self.manual_input.setStyleSheet(
                "QLineEdit {"
                "  background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;"
                "  border-radius: 8px; padding: 8px; font-family: 'JetBrains Mono','Noto Mono',monospace;"
                "}"
            )
            self.manual_input.setAlignment(Qt.AlignCenter)
            self.manual_input.returnPressed.connect(self._add_manual_amount)
            manual_button = QPushButton("Add")
            manual_button.setObjectName("add_btn")
            manual_button.clicked.connect(self._add_manual_amount)
            self._manual_row.addWidget(self.manual_input)
            self._manual_row.addWidget(manual_button)
            root.addLayout(self._manual_row)

            if self.remind_mode:
                snooze_label = QLabel("Snooze reminder:")
                snooze_label.setObjectName("status")
                root.addWidget(snooze_label)

                snooze_row = QHBoxLayout()
                snooze_row.setSpacing(6)
                for minutes in (5, 10, 15):
                    snooze_button = QPushButton(f"{minutes} min")
                    snooze_button.setObjectName("dismiss_btn")
                    snooze_button.clicked.connect(
                        lambda checked=False, delay=minutes: self._snooze_and_close(delay)
                    )
                    snooze_row.addWidget(snooze_button)
                root.addLayout(snooze_row)

            action_row = QHBoxLayout()
            action_row.setSpacing(6)

            if self.remind_mode:
                skip_button = QPushButton("Skip this drink")
                skip_button.setObjectName("dismiss_btn")
                skip_button.clicked.connect(self._skip_and_close)
                action_row.addWidget(skip_button)

            action_row.addStretch(1)

            dismiss = QPushButton("Dismiss")
            dismiss.setObjectName("dismiss_btn")
            dismiss.clicked.connect(self.close)
            action_row.addWidget(dismiss)
            root.addLayout(action_row)

            self.adjustSize()

        def showEvent(self, event):
            super().showEvent(event)
            if self.remind_mode:
                self._arm_input_guard()
            self._refresh_dynamic_display()

        def closeEvent(self, event):
            self._disarm_input_guard()
            super().closeEvent(event)

        def eventFilter(self, obj, event):
            if self.input_guard_active and self._is_guarded_input_event(event):
                if obj is self or self.isAncestorOf(obj):
                    return True
            return super().eventFilter(obj, event)

        def _arm_input_guard(self):
            if self.input_guard_active:
                return
            app = QApplication.instance()
            if app is None:
                return
            self.input_guard_active = True
            app.installEventFilter(self)
            QTimer.singleShot(REMINDER_INPUT_GUARD_MS, self._disarm_input_guard)
            logging.info("Guarding reminder popup keyboard input for %s ms", REMINDER_INPUT_GUARD_MS)

        def _disarm_input_guard(self):
            if not self.input_guard_active:
                return
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self.input_guard_active = False

        def _is_guarded_input_event(self, event):
            return event.type() in {
                QEvent.KeyPress,
                QEvent.KeyRelease,
                QEvent.ShortcutOverride,
                QEvent.InputMethod,
            }

        def _refresh_dynamic_display(self):
            now = datetime.now()
            if within_active_hours(now):
                commit_missing_hourly_targets(now)
            drunk = total_oz(now)
            expected = committed_expected_oz(now)
            remaining = max(0, TARGET_OZ - drunk)
            percent = min(100, int(drunk * 100 / TARGET_OZ))

            self.bar.setValue(percent)
            self.bar.setFormat(f"{format_oz(drunk)} / {TARGET_OZ} oz  ({percent}%)")
            if percent >= 75:
                chunk_color = "#3fb950"
            elif percent >= 40:
                chunk_color = "#d29922"
            else:
                chunk_color = "#f85149"
            self.bar.setStyleSheet(
                "QProgressBar {"
                "  border: 1px solid #30363d; border-radius: 6px; background: #161b22;"
                "  height: 14px; text-align: center; color: #c9d1d9; font-size: 10px; font-family: monospace;"
                "}"
                f"QProgressBar::chunk {{ background: {chunk_color}; border-radius: 5px; }}"
            )

            pace_class, pace_text = pace_summary(drunk, expected)
            self.pace_label.setText(pace_text)
            self.pace_label.setObjectName(pace_class)
            self.pace_label.style().unpolish(self.pace_label)
            self.pace_label.style().polish(self.pace_label)
            self.pace_label.update()

            self.remaining_label.setText(f"   {format_oz(remaining)} oz remaining to goal")

            if within_active_hours(now) and drunk < TARGET_OZ:
                next_text, sip = next_reminder_label_text(
                    now,
                    drunk,
                    next_reminder_at=self.next_reminder_at,
                )
                self.next_label.setText(next_text)
            elif drunk >= TARGET_OZ:
                self.next_label.setText("   Reminders paused: goal reached")
                sip = 0
            else:
                self.next_label.setText(f"   Reminders pause outside {active_window_label()}")
                sip = 0

            if list(QUICK_ADD) != self._rendered_quick_add:
                self._rebuild_quick_buttons(sip)
            else:
                self._update_button_highlight(sip)
            self.chart.snapshot = now
            self.chart.hourly = hourly_oz(now)
            self.chart.update()

        def _make_add_button(self, oz, highlighted=False):
            button = AmountButton(suggested_button_label(oz))
            button.amount = oz
            if highlighted:
                button.setStyleSheet(
                    "QPushButton#add_btn {"
                    "  background: #161b22; color: #58a6ff;"
                    "  border: 2px solid #3fb950;"
                    "  border-radius: 8px;"
                    "  font-family: 'JetBrains Mono','Noto Mono',monospace;"
                    "  font-size: 11px; padding: 8px 4px; min-width: 58px;"
                    "}"
                    "QPushButton#add_btn:hover { background: #1a3a1f; color: #ffffff; border-color: #56d364; }"
                )
            button.clicked.connect(lambda checked=False, amount=oz: self._log_and_close(amount))
            return button

        def _rebuild_quick_buttons(self, sip):
            for row in (self._btn_row_1, self._btn_row_2, self._btn_row_3):
                while row.count():
                    item = row.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
            # Remove any suggested button that precedes manual_input in _manual_row
            while self._manual_row.count() > 0:
                item = self._manual_row.itemAt(0)
                if item and item.widget() is self.manual_input:
                    break
                item = self._manual_row.takeAt(0)
                if item and item.widget():
                    item.widget().deleteLater()
            self.quick_buttons = []
            for index, oz in enumerate(QUICK_ADD):
                btn = self._make_add_button(oz, highlighted=(oz == sip))
                self.quick_buttons.append(btn)
                if index < 3:
                    self._btn_row_1.addWidget(btn)
                elif index < 6:
                    self._btn_row_2.addWidget(btn)
                else:
                    self._btn_row_3.addWidget(btn)
            if sip > 0 and sip not in QUICK_ADD:
                suggested = self._make_add_button(sip, highlighted=True)
                self.quick_buttons.append(suggested)
                self._manual_row.insertWidget(0, suggested)
            self._rendered_quick_add = list(QUICK_ADD)
            self.adjustSize()

        def _update_button_highlight(self, sip):
            for button in self.quick_buttons:
                if button.amount == sip and sip > 0:
                    button.setStyleSheet(
                        "QPushButton#add_btn {"
                        "  background: #161b22; color: #58a6ff;"
                        "  border: 2px solid #3fb950;"
                        "  border-radius: 8px;"
                        "  font-family: 'JetBrains Mono','Noto Mono',monospace;"
                        "  font-size: 11px; padding: 8px 4px; min-width: 58px;"
                        "}"
                        "QPushButton#add_btn:hover { background: #1a3a1f; color: #ffffff; border-color: #56d364; }"
                    )
                else:
                    button.setStyleSheet("")

        def _position_window(self):
            try:
                screen = QApplication.primaryScreen().availableGeometry()
                self.adjustSize()
                x_pos = screen.right() - self.width() - 80
                y_pos = screen.center().y() - self.height() // 2
                self.move(x_pos, y_pos)
            except Exception:
                pass

        def _log_and_close(self, oz):
            log_oz(oz)
            logging.info("Logged %s oz via popup", format_oz(oz))
            if callable(self.on_log):
                self.on_log(oz)
            self.close()

        def _add_manual_amount(self):
            text = self.manual_input.text().strip()
            if not text:
                return
            try:
                amount = float(text)
            except ValueError:
                QMessageBox.information(self, "Invalid amount", "Enter a number of ounces, e.g. 3 or 8.5.")
                return
            if amount <= 0:
                QMessageBox.information(self, "Invalid amount", "Enter a positive number of ounces.")
                return
            self._log_and_close(amount)

        def _snooze_and_close(self, minutes):
            logging.info("Snoozed reminder for %s minutes", minutes)
            if callable(self.on_snooze):
                self.on_snooze(minutes)
            self.close()

        def _skip_and_close(self):
            logging.info("Skipped current reminder drink")
            if callable(self.on_skip):
                self.on_skip()
            self.close()

        def _open_configure(self):
            if callable(self.on_configure):
                self.on_configure()
                self._refresh_dynamic_display()


    class ConfigDialog(QDialog):
        _COMBO_STYLE = (
            "QComboBox { background: #161b22; color: #c9d1d9; border: 1px solid #30363d;"
            "            border-radius: 6px; padding: 3px 6px;"
            "            font-family: 'JetBrains Mono','Noto Mono',monospace; font-size: 12px; }"
            "QComboBox::drop-down { border: none; width: 18px; }"
            "QComboBox QAbstractItemView { background: #161b22; color: #c9d1d9;"
            "                              selection-background-color: #1f6feb; border: 1px solid #30363d; }"
        )

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Water Tracker — Settings")
            self.setStyleSheet(
                "QDialog { background: #0d1117; color: #c9d1d9; }"
                "QLabel { color: #c9d1d9; font-family: 'JetBrains Mono','Noto Mono',monospace; font-size: 12px; }"
                "QSpinBox { background: #161b22; color: #c9d1d9; border: 1px solid #30363d;"
                "           border-radius: 6px; padding: 4px 8px;"
                "           font-family: 'JetBrains Mono','Noto Mono',monospace; }"
                "QLineEdit { background: #161b22; color: #c9d1d9; border: 1px solid #30363d;"
                "            border-radius: 6px; padding: 4px 6px;"
                "            font-family: 'JetBrains Mono','Noto Mono',monospace; font-size: 12px; }"
                "QCheckBox { color: #c9d1d9; font-family: 'JetBrains Mono','Noto Mono',monospace; font-size: 12px; }"
                "QPushButton { background: #161b22; color: #58a6ff; border: 1px solid #30363d;"
                "              border-radius: 6px; padding: 5px 14px; }"
                "QPushButton:hover { background: #1f6feb; color: #ffffff; }"
                + self._COMBO_STYLE
            )

            layout = QVBoxLayout(self)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(10)

            form = QFormLayout()
            form.setSpacing(8)

            self.target_spin = QSpinBox()
            self.target_spin.setRange(1, 500)
            self.target_spin.setValue(TARGET_OZ)
            self.target_spin.setSuffix(" oz")
            form.addRow("Daily target:", self.target_spin)

            self.use_24h = QCheckBox("24-hour clock (0–23)")
            self.use_24h.setChecked(USE_24H)
            form.addRow("", self.use_24h)

            self.start_h, self.start_m, self.start_ampm, start_widget = self._make_time_row(
                DAY_START_HOUR, DAY_START_MINUTE
            )
            form.addRow("Day start:", start_widget)

            self.end_h, self.end_m, self.end_ampm, end_widget = self._make_time_row(
                DAY_END_HOUR, DAY_END_MINUTE
            )
            form.addRow("Day end:", end_widget)

            hint = QLabel("Tip: set end ≤ start for overnight schedules (e.g. start 9 PM, end 8 AM).")
            hint.setObjectName("status")
            hint.setWordWrap(True)
            form.addRow("", hint)

            layout.addLayout(form)

            # --- Quick add button amounts ---
            div1 = QFrame()
            div1.setFrameShape(QFrame.HLine)
            div1.setStyleSheet("color: #30363d;")
            layout.addWidget(div1)

            quick_lbl = QLabel("Quick add buttons — check to show, edit oz value:")
            quick_lbl.setStyleSheet(
                "color: #c9d1d9; font-weight: bold;"
                "font-family: 'JetBrains Mono','Noto Mono',monospace; font-size: 12px;"
            )
            layout.addWidget(quick_lbl)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(220)
            scroll.setStyleSheet(
                "QScrollArea { border: 1px solid #30363d; border-radius: 6px; background: #0d1117; }"
                "QScrollBar:vertical { background: #161b22; width: 8px; }"
                "QScrollBar::handle:vertical { background: #30363d; border-radius: 4px; }"
            )
            inner = QWidget()
            inner.setStyleSheet("background: #0d1117;")
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(6, 6, 6, 6)
            inner_layout.setSpacing(4)

            self.quick_checks = []
            self.quick_edits = []
            for i, (current_oz, default_oz, enabled) in enumerate(
                zip(_QUICK_ADD_ALL_VALUES, _DEFAULT_QUICK_ADD, _QUICK_ADD_ALL_ENABLED)
            ):
                row_w = QWidget()
                row_l = QHBoxLayout(row_w)
                row_l.setContentsMargins(2, 2, 2, 2)
                row_l.setSpacing(8)

                cb = QCheckBox()
                cb.setToolTip("Show this button on the main panel")
                cb.setChecked(enabled)
                edit = QLineEdit()
                edit.setText(format_oz(current_oz))
                edit.setFixedWidth(70)
                edit.setPlaceholderText("oz")
                btn_lbl = QLabel(f"oz  (default: {format_oz(default_oz)})")

                row_l.addWidget(cb)
                row_l.addWidget(edit)
                row_l.addWidget(btn_lbl)
                row_l.addStretch()

                self.quick_checks.append(cb)
                self.quick_edits.append(edit)
                inner_layout.addWidget(row_w)

            inner_layout.addStretch()
            scroll.setWidget(inner)
            layout.addWidget(scroll)

            # --- Aim for oz parity ---
            div2 = QFrame()
            div2.setFrameShape(QFrame.HLine)
            div2.setStyleSheet("color: #30363d;")
            layout.addWidget(div2)

            parity_lbl = QLabel("'Aim for' oz parity:")
            parity_lbl.setStyleSheet(
                "color: #c9d1d9; font-weight: bold;"
                "font-family: 'JetBrains Mono','Noto Mono',monospace; font-size: 12px;"
            )
            layout.addWidget(parity_lbl)

            parity_hint = QLabel(
                "Which parity the reminder's 'aim for' suggestion may use. Both checked = no restriction."
            )
            parity_hint.setWordWrap(True)
            parity_hint.setStyleSheet(
                "color: #8b949e; font-size: 11px;"
                "font-family: 'JetBrains Mono','Noto Mono',monospace;"
            )
            layout.addWidget(parity_hint)

            parity_row = QHBoxLayout()
            parity_row.setSpacing(16)
            self.cb_odd = QCheckBox("Odd")
            self.cb_odd.setChecked(AIM_FOR_ODD)
            self.cb_even = QCheckBox("Even")
            self.cb_even.setChecked(AIM_FOR_EVEN)
            parity_row.addWidget(self.cb_odd)
            parity_row.addWidget(self.cb_even)
            parity_row.addStretch()
            layout.addLayout(parity_row)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

            self.use_24h.toggled.connect(self._on_toggle_24h)
            self._on_toggle_24h(USE_24H)

        @staticmethod
        def _hour_items_12():
            return [str(h) for h in range(1, 13)]

        @staticmethod
        def _hour_items_24():
            return [f"{h:02d}" for h in range(24)]

        @staticmethod
        def _minute_items():
            return [f"{m:02d}" for m in range(0, 60, 5)]

        @staticmethod
        def _hour_to_12h(h24):
            return h24 % 12 or 12, "AM" if h24 < 12 else "PM"

        @staticmethod
        def _hour_from_12h(h12_str, period):
            h = int(h12_str) % 12
            return h + (12 if period == "PM" else 0)

        def _make_time_row(self, hour_24, minute):
            h_combo = QComboBox()
            h_combo.addItems(self._hour_items_12())
            h12, period = self._hour_to_12h(hour_24)
            h_combo.setCurrentText(str(h12))

            sep = QLabel(":")
            sep.setObjectName("status")

            m_combo = QComboBox()
            m_combo.addItems(self._minute_items())
            snapped = (minute // 5) * 5
            m_combo.setCurrentText(f"{snapped:02d}")

            ampm_combo = QComboBox()
            ampm_combo.addItems(["AM", "PM"])
            ampm_combo.setCurrentText(period)

            container = QWidget()
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(h_combo)
            row.addWidget(sep)
            row.addWidget(m_combo)
            row.addWidget(ampm_combo)
            row.addStretch()

            return h_combo, m_combo, ampm_combo, container

        def _on_toggle_24h(self, checked):
            for h_combo, ampm_combo in [
                (self.start_h, self.start_ampm),
                (self.end_h, self.end_ampm),
            ]:
                cur = h_combo.currentText()
                if checked:
                    h24 = self._hour_from_12h(cur, ampm_combo.currentText())
                    h_combo.clear()
                    h_combo.addItems(self._hour_items_24())
                    h_combo.setCurrentText(f"{h24:02d}")
                    ampm_combo.setVisible(False)
                else:
                    h24 = int(cur)
                    h12, period = self._hour_to_12h(h24)
                    h_combo.clear()
                    h_combo.addItems(self._hour_items_12())
                    h_combo.setCurrentText(str(h12))
                    ampm_combo.setCurrentText(period)
                    ampm_combo.setVisible(True)

        def _read_time(self, h_combo, m_combo, ampm_combo):
            if self.use_24h.isChecked():
                return int(h_combo.currentText()), int(m_combo.currentText())
            return self._hour_from_12h(h_combo.currentText(), ampm_combo.currentText()), int(m_combo.currentText())

        def _accept(self):
            sh, sm = self._read_time(self.start_h, self.start_m, self.start_ampm)
            eh, em = self._read_time(self.end_h, self.end_m, self.end_ampm)
            if sh == eh and sm == em:
                QMessageBox.warning(self, "Invalid range", "Start and end times must differ.")
                return
            self.accept()

        def values(self):
            sh, sm = self._read_time(self.start_h, self.start_m, self.start_ampm)
            eh, em = self._read_time(self.end_h, self.end_m, self.end_ampm)
            quick_add_values = []
            quick_add_enabled = []
            for i, (cb, edit) in enumerate(zip(self.quick_checks, self.quick_edits)):
                try:
                    v = float(edit.text().strip())
                    quick_add_values.append(max(0.1, v))
                except ValueError:
                    quick_add_values.append(_DEFAULT_QUICK_ADD[i])
                quick_add_enabled.append(cb.isChecked())
            aim_for_odd = self.cb_odd.isChecked()
            aim_for_even = self.cb_even.isChecked()
            return (
                self.target_spin.value(), sh, sm, eh, em, self.use_24h.isChecked(),
                quick_add_values, quick_add_enabled, aim_for_odd, aim_for_even,
            )

    class WaterTray:
        def __init__(self, app):
            self.app = app
            self.menu = QMenu()
            self.icon = self._build_icon()
            self.tray = QSystemTrayIcon(self.icon, self.app)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self._handle_activation)
            self.popups = []
            self.next_reminder_at = None
            self.current_day = today_string()
            self.current_hour = datetime.now().hour
            self._build_menu()
            if within_active_hours(datetime.now()):
                commit_missing_hourly_targets()
            self.refresh_state(reschedule=True)

            self.timer = QTimer(self.app)
            self.timer.timeout.connect(self._tick)
            self.timer.start(60 * 1000)

            self.tray.show()
            self.tray.showMessage(
                "Water tracker",
                "Tray reminders are active. Use the tray menu to log water any time.",
                QSystemTrayIcon.Information,
                3500,
            )

        def _build_icon(self):
            return self._render_tray_icon(None)

        def _base_icon_pixmap(self, size=64):
            if ICON_FILE.exists():
                pixmap = QPixmap(str(ICON_FILE))
                if not pixmap.isNull():
                    return pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                icon = QIcon(str(ICON_FILE))
                if not icon.isNull():
                    return icon.pixmap(size, size)
            icon = QIcon.fromTheme("drink-water")
            if icon.isNull():
                icon = QIcon.fromTheme("waterdrop")
            if icon.isNull():
                icon = self.app.style().standardIcon(QStyle.SP_DriveDVDIcon)
            return icon.pixmap(size, size)

        def _render_tray_icon(self, minutes):
            size = 64
            drop = self._base_icon_pixmap(size)

            if minutes is None:
                return QIcon(drop)

            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            # Drop at full size and full opacity
            painter.drawPixmap(0, 0, drop)

            text = str(min(99, max(1, minutes)))
            is_single = len(text) == 1

            # Badge anchored to bottom-right corner so the drop is fully visible
            pill_w = 30 if is_single else 46
            pill_h = 34
            margin = 1
            pill_x = size - pill_w - margin
            pill_y = size - pill_h - margin

            # Dark pill, no border
            painter.setBrush(QBrush(QColor(5, 15, 35, 230)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(pill_x, pill_y, pill_w, pill_h, 7, 7)

            # White bold number
            font = QFont("Hack")
            font.setBold(True)
            font.setPixelSize(30 if is_single else 24)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(QRect(pill_x, pill_y, pill_w, pill_h), Qt.AlignCenter, text)

            painter.end()
            return QIcon(pixmap)

        def _countdown_minutes(self, now, drunk):
            if drunk >= TARGET_OZ or not within_active_hours(now):
                return None
            if not self.next_reminder_at:
                minutes = fallback_next_reminder_minutes(now)
                return minutes or None
            remaining_seconds = max(0, int((self.next_reminder_at - now).total_seconds()))
            return max(1, math.ceil(remaining_seconds / 60))

        def _build_menu(self):
            self.status_action = QAction("", self.menu)
            self.status_action.setEnabled(False)
            self.pace_action = QAction("", self.menu)
            self.pace_action.setEnabled(False)
            self.next_action = QAction("", self.menu)
            self.next_action.setEnabled(False)
            self.menu.addAction(self.status_action)
            self.menu.addAction(self.pace_action)
            self.menu.addAction(self.next_action)
            self.menu.addSeparator()

            open_popup_action = QAction("Open popup", self.menu)
            open_popup_action.triggered.connect(self.show_manual_popup)
            self.menu.addAction(open_popup_action)

            for oz in QUICK_ADD:
                action = QAction(f"Add {oz} oz", self.menu)
                action.triggered.connect(lambda checked=False, amount=oz: self.add_amount(amount))
                self.menu.addAction(action)

            self.menu.addSeparator()

            log_action = QAction("Show today's log", self.menu)
            log_action.triggered.connect(self.show_log_summary)
            self.menu.addAction(log_action)

            error_log_action = QAction("Show error log", self.menu)
            error_log_action.triggered.connect(self.show_error_log)
            self.menu.addAction(error_log_action)

            self.menu.addSeparator()

            configure_action = QAction("Configure...", self.menu)
            configure_action.triggered.connect(self.show_config)
            self.menu.addAction(configure_action)

            quit_action = QAction("Quit tray", self.menu)
            quit_action.triggered.connect(self.app.quit)
            self.menu.addAction(quit_action)

        def refresh_state(self, reschedule=False):
            now = datetime.now()
            if within_active_hours(now):
                commit_missing_hourly_targets(now)
            drunk = total_oz(now)
            expected = committed_expected_oz(now)
            _, pace_text = pace_summary(drunk, expected)

            self.status_action.setText(f"Today: {format_oz(drunk)} / {TARGET_OZ} oz")
            self.pace_action.setText(pace_text)

            if reschedule:
                self.schedule_next_reminder(now)

            next_text = self._next_reminder_text(now, drunk)
            self.next_action.setText(next_text)
            tooltip = "\n".join(build_status_lines(now, next_reminder_at=self.next_reminder_at))
            self.tray.setToolTip(tooltip)
            self.tray.setIcon(self._render_tray_icon(self._countdown_minutes(now, drunk)))

        def schedule_next_reminder(self, now=None):
            moment = now or datetime.now()
            drunk = total_oz(moment)
            if drunk >= TARGET_OZ or not within_active_hours(moment):
                self.next_reminder_at = None
                return

            hour_interval = current_hour_reminder_interval(moment)
            if hour_interval > 0:
                interval = hour_interval
            elif moment.hour + 1 < DAY_END_HOUR:
                next_hour = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                self.next_reminder_at = next_hour
                logging.info(
                    "Scheduled next reminder for next hour boundary at %s",
                    next_hour.strftime("%H:%M:%S"),
                )
                return
            else:
                self.next_reminder_at = None
                logging.info("No more reminders scheduled for today")
                return

            last_time = last_entry_time(moment)
            if last_time is not None:
                candidate = last_time + timedelta(minutes=interval)
                if candidate <= moment:
                    candidate = moment + timedelta(minutes=interval)
                self.next_reminder_at = candidate
            else:
                self.next_reminder_at = moment + timedelta(minutes=interval)
            logging.info(
                "Scheduled next reminder in %s minutes (drunk=%s hour_target=%s hour_remaining=%s last=%s)",
                interval,
                format_oz(drunk),
                live_hour_target(moment),
                current_hour_remaining_target(moment),
                last_time.strftime("%H:%M:%S") if last_time else "none",
            )

        def _next_reminder_text(self, now, drunk):
            if drunk >= TARGET_OZ:
                return "Reminders paused: goal reached"
            if not within_active_hours(now):
                return f"Reminders pause outside {active_window_label()}"
            if not self.next_reminder_at:
                minutes = fallback_next_reminder_minutes(now)
                return f"Next reminder: ~{minutes} min"

            remaining_minutes = self._countdown_minutes(now, drunk)
            return f"Next reminder in ~{remaining_minutes} min"

        def _tick(self):
            now = datetime.now()
            today = today_string(now)
            if today != self.current_day:
                self.current_day = today
                self.current_hour = now.hour
                self.refresh_state(reschedule=True)
                return

            if now.hour != self.current_hour:
                self.current_hour = now.hour
                self.refresh_state(reschedule=True)
                return

            # Lock in this hour's target before any drinking happens in it.
            if within_active_hours(now):
                commit_missing_hourly_targets(now)

            if self.next_reminder_at is None and within_active_hours(now) and total_oz(now) < TARGET_OZ:
                self.schedule_next_reminder(now)

            if self.next_reminder_at and now >= self.next_reminder_at:
                if not any(popup.isVisible() for popup in self.popups):
                    self.show_reminder_popup()
                self.schedule_next_reminder(now)

            self.refresh_state(reschedule=False)

        def _handle_activation(self, reason):
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
                self.show_manual_popup()

        def _remember_popup(self, popup):
            self.popups.append(popup)
            self.popups = [item for item in self.popups if item.isVisible()]

        def show_manual_popup(self):
            popup = WaterPopup(
                remind_mode=False,
                on_log=self._after_log,
                on_skip=self.skip_current_drink,
                on_configure=self.show_config,
                next_reminder_at=self.next_reminder_at,
            )
            popup.show()
            popup.raise_()
            if not is_wayland_session():
                popup.activateWindow()
            logging.info("Opened manual popup")
            self._remember_popup(popup)

        def show_reminder_popup(self):
            popup = WaterPopup(
                remind_mode=True,
                on_log=self._after_log,
                on_snooze=self.snooze_reminder,
                on_skip=self.skip_current_drink,
                on_configure=self.show_config,
                next_reminder_at=self.next_reminder_at,
            )
            popup.show()
            popup.raise_()
            if not is_wayland_session():
                popup.activateWindow()
            logging.info("Displayed reminder popup")
            self._remember_popup(popup)

        def add_amount(self, oz):
            log_oz(oz)
            logging.info("Logged %s oz via tray menu", format_oz(oz))
            self._after_log(oz)

        def snooze_reminder(self, minutes):
            self.next_reminder_at = datetime.now() + timedelta(minutes=minutes)
            self.refresh_state(reschedule=False)
            self.tray.showMessage(
                "Water reminder snoozed",
                f"Next reminder in about {minutes} minutes.",
                QSystemTrayIcon.Information,
                2500,
            )

        def skip_current_drink(self):
            now = datetime.now()
            self.schedule_next_reminder(now)
            if self.next_reminder_at is None:
                message = "No more reminders scheduled for today."
            else:
                minutes = max(1, math.ceil((self.next_reminder_at - now).total_seconds() / 60))
                message = f"Next reminder in about {minutes} minutes."
            self.refresh_state(reschedule=False)
            self.tray.showMessage(
                "Water reminder skipped",
                message,
                QSystemTrayIcon.Information,
                2500,
            )

        def _after_log(self, oz):
            self.refresh_state(reschedule=True)
            self.tray.showMessage(
                "Water logged",
                f"Added {format_oz(oz)} oz. Total now {build_status_lines()[0]}.",
                QSystemTrayIcon.Information,
                2500,
            )

        def show_log_summary(self):
            entries = read_entries()
            if not entries:
                text = "No entries yet for today."
            else:
                lines = [f"{ts}  +{format_oz(amount)} oz" for ts, amount in entries[-10:]]
                if len(entries) > 10:
                    lines.insert(0, f"Showing last 10 of {len(entries)} entries")
                text = "\n".join(lines)

            QMessageBox.information(None, "Water log", text)

        def show_error_log(self):
            if not TRAY_LOG_FILE.exists():
                text = "No app log has been created yet."
            else:
                lines = TRAY_LOG_FILE.read_text(encoding="utf-8").splitlines()
                problem_lines = [line for line in lines if " ERROR " in line or " WARNING " in line or " CRITICAL " in line]
                if problem_lines:
                    text = "\n".join(problem_lines[-20:])
                elif lines:
                    recent = lines[-20:]
                    text = "No warnings or errors logged. Recent app activity:\n\n" + "\n".join(recent)
                else:
                    text = "The app log is empty."

            QMessageBox.information(None, "Water app log", text)

        def show_config(self):
            dialog = ConfigDialog()
            if dialog.exec_() == QDialog.Accepted:
                target_oz, sh, sm, eh, em, use_24h, quick_add_values, quick_add_enabled, aim_for_odd, aim_for_even = dialog.values()
                save_config(target_oz, sh, sm, eh, em, use_24h, quick_add_values, quick_add_enabled, aim_for_odd, aim_for_even)
                _apply_config()
                logging.info(
                    "Settings updated: target=%s oz  start=%02d:%02d  end=%02d:%02d",
                    target_oz, sh, sm, eh, em,
                )
                self.refresh_state(reschedule=True)


def run_qt_popup(remind_mode=False):
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("water")
    signal_timer = QTimer()
    signal_timer.start(250)
    signal_timer.timeout.connect(lambda: None)
    popup = WaterPopup(
        remind_mode=remind_mode,
        on_skip=(lambda: None) if remind_mode else None,
    )
    popup.show()
    popup.raise_()
    if not is_wayland_session():
        popup.activateWindow()
    logging.info("Started standalone popup mode=%s", "remind" if remind_mode else "gui")
    sys.exit(app.exec_())


def handle_signal(signum, frame):
    logging.info("Received signal %s, shutting down", signum)
    app = QApplication.instance()
    if app is not None:
        app.quit()


def run_tray():
    setup_logging()
    existing_pid = read_pid()
    if existing_pid and pid_is_running(existing_pid):
        logging.info("Tray launch skipped; already running with pid %s", existing_pid)
        print(f"Water tray already running (pid {existing_pid}).")
        return 0

    remove_pid()
    app = QApplication(sys.argv)
    app.setApplicationName("water-tray")
    app.setQuitOnLastWindowClosed(False)
    signal_timer = QTimer()
    signal_timer.start(250)
    signal_timer.timeout.connect(lambda: None)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        logging.error("System tray is not available in this session")
        print("System tray is not available in this session.", file=sys.stderr)
        return 1

    write_pid()
    atexit.register(remove_pid)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    app.aboutToQuit.connect(remove_pid)
    app.aboutToQuit.connect(lambda: logging.info("Tray app exiting"))

    WaterTray(app)
    logging.info("Tray app started")
    return app.exec_()


def run_kdialog():
    drunk = total_oz()
    remaining = max(0, TARGET_OZ - drunk)
    percent = min(100, int(drunk * 100 / TARGET_OZ))
    expected = committed_expected_oz()
    _, pace_text = pace_summary(drunk, expected)

    message = (
        f"Progress: {format_oz(drunk)} / {TARGET_OZ} oz ({percent}%)\n"
        f"{pace_text}\n"
        f"Remaining: {format_oz(remaining)} oz\n\n"
        "How much did you drink?"
    )

    button_map = {
        "4 oz (sip)": 4,
        "8 oz (1/2 cup)": 8,
        "12 oz (mug)": 12,
        "16 oz (glass)": 16,
        "20 oz (large)": 20,
        "24 oz (bottle)": 24,
        "Skip this drink": "skip-drink",
        "Nothing / dismiss": None,
    }

    buttons = list(button_map.keys())
    args = ["kdialog", "--menu", message]
    for index, label in enumerate(buttons):
        args += [str(index), label]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        return

    selected_index = int(result.stdout.strip())
    selected_label = buttons[selected_index]
    ounces = button_map[selected_label]
    if ounces == "skip-drink":
        return
    if ounces:
        log_oz(ounces)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "gui"

    if mode == "tray":
        if not HAS_QT:
            print("PyQt5 is required for tray mode.", file=sys.stderr)
            sys.exit(1)
        sys.exit(run_tray())

    if HAS_QT:
        run_qt_popup(remind_mode=(mode == "remind"))
    else:
        print("PyQt5 not found, falling back to kdialog...")
        run_kdialog()
