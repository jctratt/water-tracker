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
DAY_END_HOUR = 21
DATA_DIR = Path.home() / ".local" / "share" / "water"
PID_FILE = DATA_DIR / "tray.pid"
TRAY_LOG_FILE = DATA_DIR / "tray.log"
ICON_FILE = Path.home() / "bin" / "water-icon.svg"
QUICK_ADD = [1, 2, 3, 4, 8, 12, 16, 20, 24]
LABELS = [
    "1 oz\nsip",
    "2 oz\nsip",
    "3 oz\nsip",
    "4 oz\nsip",
    "8 oz\n1/2 cup",
    "12 oz\nmug",
    "16 oz\nglass",
    "20 oz\nlarge",
    "24 oz\nbottle",
]


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
    if hour < DAY_START_HOUR:
        return 0
    if hour >= DAY_END_HOUR:
        return TARGET_OZ
    day_minutes = (DAY_END_HOUR - DAY_START_HOUR) * 60
    elapsed = (hour - DAY_START_HOUR) * 60 + minute
    return int(TARGET_OZ * elapsed / day_minutes)


def bell_weight(hour):
    # Peak at 10am, drops steeply after 3pm — front-loads for losartan, protects sleep
    center, sigma = 10.0, 3.0
    return math.exp(-((hour - center) ** 2) / (2 * sigma**2))


def hourly_expected_weighted():
    """Baseline expected oz per hour, bell-curve weighted, summing to TARGET_OZ."""
    hours = range(DAY_START_HOUR, DAY_END_HOUR)
    weights = {h: bell_weight(h + 0.5) for h in hours}
    total_w = sum(weights.values())
    return {h: TARGET_OZ * w / total_w for h, w in weights.items()}


def adjusted_future_expected(now, drunk):
    """Redistribute remaining oz over current+future hours, bell-curve weighted."""
    current_hour = now.hour
    remaining_oz = max(0.0, TARGET_OZ - drunk)
    future_hours = [h for h in range(max(current_hour, DAY_START_HOUR), DAY_END_HOUR)]
    if not future_hours:
        return {}

    remaining_hour_fraction = max(
        0.0,
        1.0 - ((now.minute * 60 + now.second) / 3600),
    )

    weights = {}
    for hour in future_hours:
        hour_weight = bell_weight(hour + 0.5)
        if hour == current_hour:
            hour_weight *= remaining_hour_fraction
        weights[hour] = hour_weight

    total_w = sum(weights.values()) or 1.0
    return {h: remaining_oz * w / total_w for h, w in weights.items()}


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
    if moment.hour < DAY_START_HOUR:
        return load_committed_targets(moment)

    committed = load_committed_targets(moment)
    last_hour = min(moment.hour, DAY_END_HOUR - 1)
    changed = False

    for hour in range(DAY_START_HOUR, last_hour + 1):
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
        key=lambda key: (values[key] - math.floor(values[key]), -key),
        reverse=True,
    )

    for key in ranked_keys[:remainder]:
        rounded[key] += 1

    return rounded


def suggested_next_oz(now, drunk, interval_minutes=None):
    """How many oz to aim for in the next reminder interval.

    PKD requires consistent frequent sips, not large boluses.  We cap the
    suggestion at 8 oz — urgency is expressed via shorter intervals, not bigger
    drinks.  The snap ladder only goes up to 8 oz for the same reason.
    """
    if not within_active_hours(now) or drunk >= TARGET_OZ:
        return 0
    remaining_oz = max(0.0, TARGET_OZ - drunk)
    day_end = now.replace(hour=DAY_END_HOUR, minute=0, second=0, microsecond=0)
    remaining_minutes = max(1.0, (day_end - now).total_seconds() / 60)
    if interval_minutes is None:
        interval_minutes = reminder_interval_minutes(now, drunk=drunk, expected=expected_oz(now))
    raw = remaining_oz * interval_minutes / remaining_minutes
    PKD_MAX_SIP = 8
    # Snap to meaningful sip sizes, never exceeding the PKD max
    for snap in (4, 6, 8):
        if raw <= snap:
            return snap
    return PKD_MAX_SIP


def reminder_interval_minutes(now=None, drunk=None, expected=None):
    moment = now or datetime.now()
    fractional_hour = moment.hour + (moment.minute / 60)
    weight = bell_weight(fractional_hour)
    base_interval = int(90 - (weight * 70))

    current_drunk = total_oz(moment) if drunk is None else drunk
    current_expected = expected_oz(moment) if expected is None else expected
    deficit = max(0, current_expected - current_drunk)

    if deficit <= 0:
        return base_interval

    if deficit >= 20:
        reduction = 0.45
    elif deficit >= 10:
        reduction = 0.30
    else:
        reduction = 0.15

    if moment.hour >= 20:
        reduction *= 0.35
        minimum_interval = 30
    elif moment.hour >= 18:
        reduction *= 0.60
        minimum_interval = 20
    else:
        minimum_interval = 8

    adjusted_interval = int(base_interval * (1 - reduction))
    return max(minimum_interval, adjusted_interval)


def within_active_hours(now=None):
    moment = now or datetime.now()
    return DAY_START_HOUR <= moment.hour < DAY_END_HOUR


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
    expected = expected_oz(moment)
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
            lines.append(f"Next reminder in ~{reminder_interval_minutes(moment, drunk=drunk, expected=expected)} min")
    elif drunk >= TARGET_OZ:
        lines.append("Reminders paused: daily goal reached")
    else:
        lines.append("Reminders pause outside 7 AM - 9 PM")
    return lines


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
    from PyQt5.QtCore import QRect, Qt, QTimer
    from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap
    from PyQt5.QtWidgets import (
        QAction,
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMenu,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QLineEdit,
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

            hours = list(range(DAY_START_HOUR, DAY_END_HOUR))
            total_hours = DAY_END_HOUR - DAY_START_HOUR
            current_hour = self.snapshot.hour
            drunk = sum(self.hourly.values())

            hist_exp = hourly_expected_weighted()
            committed = load_committed_targets(self.snapshot)
            remaining_oz = max(0.0, TARGET_OZ - drunk)

            # Past + current hour: show the committed target for reference.
            past_cur_exp = {
                h: committed.get(h, hist_exp.get(h, 0))
                for h in range(DAY_START_HOUR, current_hour + 1)
            }
            # Fully future hours only: redistribute ALL remaining oz with bell
            # weighting, starting from current_hour+1. This guarantees future
            # bars sum to remaining_oz, matching the "X oz remaining" counter.
            future_hs = list(range(current_hour + 1, DAY_END_HOUR))
            if future_hs:
                fw = {hh: bell_weight(hh + 0.5) for hh in future_hs}
                fw_total = sum(fw.values()) or 1.0
                future_exp = {hh: remaining_oz * fw[hh] / fw_total for hh in future_hs}
            else:
                future_exp = {}

            combined_exp = {**past_cur_exp, **future_exp}
            past_labels = rounded_distribution(past_cur_exp)
            future_labels = rounded_distribution(future_exp, target_total=int(round(remaining_oz)))
            expected_labels = {**past_labels, **future_labels}

            all_exp = list(combined_exp.values())
            max_exp = max(all_exp) if all_exp else 8.0
            max_actual = max(self.hourly.values()) if self.hourly else 0.0
            max_oz = max(max_exp, max_actual or 8.0) * 2.2

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
                is_current = hour == current_hour

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
                    if DAY_START_HOUR <= frac_hour < DAY_END_HOUR:
                        x_px = left_m + (frac_hour - DAY_START_HOUR) / total_hours * chart_w
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
        def __init__(self, remind_mode=False, on_log=None, on_snooze=None, next_reminder_at=None):
            super().__init__()
            self.remind_mode = remind_mode
            self.on_log = on_log
            self.on_snooze = on_snooze
            self.next_reminder_at = next_reminder_at
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
            expected = expected_oz(snapshot_time)
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
            title_row.addWidget(title)
            title_row.addWidget(clock_label)
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

            if snapshot_time.hour >= 20:
                evening_label = QLabel("Evening mode - small sips to protect sleep")
                evening_label.setObjectName("evening")
                root.addWidget(evening_label)

            if within_active_hours(snapshot_time) and drunk < TARGET_OZ:
                if self.next_reminder_at and self.next_reminder_at > snapshot_time:
                    next_minutes = max(1, math.ceil((self.next_reminder_at - snapshot_time).total_seconds() / 60))
                else:
                    next_minutes = reminder_interval_minutes(snapshot_time, drunk=drunk, expected=expected)
                sip = suggested_next_oz(snapshot_time, drunk, interval_minutes=next_minutes)
                next_label = QLabel(f"   Next reminder: ~{next_minutes} min  →  aim for {sip} oz")
            elif drunk >= TARGET_OZ:
                next_label = QLabel("   Reminders paused: goal reached")
                sip = 0
            else:
                next_label = QLabel("   Reminders pause outside 7 AM - 9 PM")
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

            row_one = QHBoxLayout()
            row_one.setSpacing(6)
            row_two = QHBoxLayout()
            row_two.setSpacing(6)
            row_three = QHBoxLayout()
            row_three.setSpacing(6)

            for index, (oz, label) in enumerate(zip(QUICK_ADD, LABELS)):
                button = QPushButton(label)
                button.setObjectName("add_btn")
                button.amount = oz
                self.quick_buttons.append(button)
                if oz == sip:
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
                if index < 3:
                    row_one.addWidget(button)
                elif index < 6:
                    row_two.addWidget(button)
                else:
                    row_three.addWidget(button)

            root.addLayout(row_one)
            root.addLayout(row_two)
            root.addLayout(row_three)

            divider_two = QFrame()
            divider_two.setObjectName("divider")
            divider_two.setFrameShape(QFrame.HLine)
            root.addWidget(divider_two)

            manual_label = QLabel("Custom amount:")
            manual_label.setObjectName("status")
            root.addWidget(manual_label)

            manual_row = QHBoxLayout()
            manual_row.setSpacing(6)
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
            manual_row.addWidget(self.manual_input)
            manual_row.addWidget(manual_button)
            root.addLayout(manual_row)

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

            dismiss = QPushButton("Dismiss")
            dismiss.setObjectName("dismiss_btn")
            dismiss.clicked.connect(self.close)
            root.addWidget(dismiss, alignment=Qt.AlignRight)

            self.adjustSize()

        def showEvent(self, event):
            super().showEvent(event)
            self._refresh_dynamic_display()

        def _refresh_dynamic_display(self):
            now = datetime.now()
            if within_active_hours(now):
                commit_missing_hourly_targets(now)
            drunk = total_oz(now)
            expected = expected_oz(now)
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
                if self.next_reminder_at and self.next_reminder_at > now:
                    next_minutes = max(1, math.ceil((self.next_reminder_at - now).total_seconds() / 60))
                else:
                    next_minutes = reminder_interval_minutes(now, drunk=drunk, expected=expected)
                sip = suggested_next_oz(now, drunk, interval_minutes=next_minutes)
                self.next_label.setText(f"   Next reminder: ~{next_minutes} min  →  aim for {sip} oz")
            elif drunk >= TARGET_OZ:
                self.next_label.setText("   Reminders paused: goal reached")
                sip = 0
            else:
                self.next_label.setText("   Reminders pause outside 7 AM - 9 PM")
                sip = 0

            self._update_button_highlight(sip)
            self.chart.snapshot = now
            self.chart.hourly = hourly_oz(now)
            self.chart.update()

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
                expected = expected_oz(now)
                return reminder_interval_minutes(now, drunk=drunk, expected=expected)
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

            quit_action = QAction("Quit tray", self.menu)
            quit_action.triggered.connect(self.app.quit)
            self.menu.addAction(quit_action)

        def refresh_state(self, reschedule=False):
            now = datetime.now()
            if within_active_hours(now):
                commit_missing_hourly_targets(now)
            drunk = total_oz(now)
            expected = expected_oz(now)
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
            expected = expected_oz(moment)
            interval = reminder_interval_minutes(moment, drunk=drunk, expected=expected)
            last_time = last_entry_time(moment)
            if last_time is not None:
                candidate = last_time + timedelta(minutes=interval)
                self.next_reminder_at = max(moment, candidate)
            else:
                self.next_reminder_at = moment + timedelta(minutes=interval)
            logging.info(
                "Scheduled next reminder in %s minutes (drunk=%s expected=%s last=%s)",
                interval,
                format_oz(drunk),
                expected,
                last_time.strftime("%H:%M:%S") if last_time else "none",
            )

        def _next_reminder_text(self, now, drunk):
            if drunk >= TARGET_OZ:
                return "Reminders paused: goal reached"
            if not within_active_hours(now):
                return "Reminders pause outside 7 AM - 9 PM"
            if not self.next_reminder_at:
                expected = expected_oz(now)
                return f"Next reminder: ~{reminder_interval_minutes(now, drunk=drunk, expected=expected)} min"

            remaining_minutes = self._countdown_minutes(now, drunk)
            return f"Next reminder in ~{remaining_minutes} min"

        def _tick(self):
            now = datetime.now()
            today = today_string(now)
            if today != self.current_day:
                self.current_day = today
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
            popup = WaterPopup(remind_mode=False, on_log=self._after_log, next_reminder_at=self.next_reminder_at)
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
                next_reminder_at=self.next_reminder_at,
            )
            popup.show()
            popup.raise_()
            if not is_wayland_session():
                popup.activateWindow()
            logging.info("Displayed reminder popup")
            self._remember_popup(popup)
            self.tray.showMessage(
                "Water reminder",
                "Time for another drink. Use the popup or the tray menu to log it.",
                QSystemTrayIcon.Information,
                5000,
            )

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


def run_qt_popup(remind_mode=False):
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("water")
    signal_timer = QTimer()
    signal_timer.start(250)
    signal_timer.timeout.connect(lambda: None)
    popup = WaterPopup(remind_mode=remind_mode)
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
    expected = expected_oz()
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
