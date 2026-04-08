# water-tracker

A small KDE Plasma hydration tracker for tracking daily water intake.

This repository contains a lightweight shell wrapper and Python popup/tray UI for logging water in ounces, showing progress toward a 101 oz daily target, and optionally running as a persistent KDE system tray app.

The current pacing model aims for steady hydration across the active day rather than front-loading most of the target into the morning.

## Contents

- `water` — CLI wrapper script for logging intake, showing status, and controlling the tray app
- `water-popup.py` — Python popup/tray UI and reminder logic
- `water-tray.desktop` — KDE desktop entry for the tray app
- `water-icon.svg` — Icon used by the tray app

## Installation

1. Ensure `~/bin` is on your `PATH`.
2. Make the scripts executable:
   ```bash
   chmod +x water water-popup.py
   ```
3. Copy the files to a suitable location, for example:
   ```bash
   cp water water-popup.py ~/.local/bin/
   cp water-icon.svg ~/bin/
   ```
4. Optional: install the desktop file if you want a launcher or autostart entry.

## Usage

Run the `water` command from a terminal:

- `water` — show terminal status
- `water gui` — open clickable popup window
- `water tray` — start the persistent system tray app
- `water stop` — stop the tray app
- `water restart` — restart the tray app
- `water tray-status` — show whether the tray app is running
- `water <oz>` — log a drinking event in ounces
- `water undo` — remove the last entry
- `water reset` — clear today’s log
- `water remind` — show a reminder popup immediately
- `water cron` — install legacy cron reminders
- `water uncron` — remove legacy cron reminders
- `water log` — show today’s full log
- `water schedule` — show the steady hourly pacing plan

## Settings

The popup includes a gear button to open the settings dialog. Settings are persisted in `~/.local/share/water/config.json` and include:

- daily target in ounces
- day start and end time with separate hour/minute selectors
- 12-hour or 24-hour time entry mode
- overnight schedules by setting end time at or before start time
- custom quick-add buttons: show/hide individual buttons and adjust their oz amounts
- reminder "aim for" parity: limit suggestions to odd, even, or both ounce values

## Data storage

Logs are stored in `~/.local/share/water/` as daily log files named `YYYY-MM-DD.log`.

## License

This project does not include a license file. Add one if you want an explicit open-source license.
