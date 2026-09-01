# BATC Pauser - agent guide

Single-file Windows app that freezes MSFS 2024 (via SimConnect) when BeyondATC clears you
for arrival/descent, or at a chosen SimBrief waypoint. All logic is in `batc_pauser.py`,
split into the numbered sections listed in its module docstring. `README.md` is the
user-facing reference for behavior, BeyondATC log parsing, and every config key - read it
before touching trigger logic.

## Run / test

- Run: `python batc_pauser.py` (or `Start BATC Pauser.bat`). Windows-only (tkinter + Win32).
- No test suite, no `requirements.txt`. It imports the `SimConnect` package already installed
  under the user's Roaming Python site-packages (the bundled `SimConnect.dll` lives inside
  that package). `requests` is NOT installed - use stdlib `urllib`.
- Real testing is live against MSFS + BeyondATC, which only the user can observe. For a
  SimConnect question, write a tiny single-event probe in the scratchpad and have the user
  report what the sim does (the `pause_probe.py` / `hold_probe.py` pattern from development).
  For UI, launch the app and screenshot the window via PowerShell `CopyFromScreen`.
- `config.json` is auto-generated on first run from `DEFAULT_CONFIG`, is gitignored, and
  holds the user's personal `simbrief_id`. Never commit it.

## MSFS 2024 SimConnect facts (hard-won, non-obvious)

- **No SimConnect pause stops the world clock.** `PAUSE_ON` and `PAUSE_SET` both give an
  *active* pause (aircraft frozen, time of day keeps running); `PAUSE_TOGGLE` does nothing.
  The clock-stopping menu pause is unreachable via SimConnect. Confirmed live and on Asobo
  dev forums.
- **The clock freeze is faked** by re-setting Zulu time ~2x/sec with `ZULU_HOURS_SET` /
  `ZULU_MINUTES_SET` (minute granularity only) while paused - see `SimLink._hold_clock_tick`.
- **Reading `ZULU TIME`**: it is read-only AND not in `AircraftRequests`' registered set, so
  `AircraftRequests.get("ZULU_TIME")` returns `None`. Read it with a direct
  `SimConnect.RequestList.Request((b"ZULU TIME", b"Seconds"), sm)` (see `SimLink._connect`).
- **Sim rate** (`SIMULATION RATE`) is read-only; change it only via `SIM_RATE_INCR` /
  `SIM_RATE_DECR`, each of which doubles/halves - see `SimLink._set_rate`.
- **BeyondATC has no API.** It is frozen by suspending its process via `NtSuspendProcess`
  (`BatcController`). Always thaw on exit (both `App.quit` and an `atexit` hook) so it can
  never be left frozen.
- ctypes gotcha: set `restype`/`argtypes` or Win32 HANDLEs get truncated on 64-bit (see
  `BatcController.__init__`). And `SimConnect()` can hand back a not-really-connected object,
  so `SimLink._connect` proves the link by mapping events before trusting it.

## Conventions

- Match the existing style in `batc_pauser.py`: comments explain *why*, not *what*; daemon
  threads + `queue` for cross-thread work; `self.px()` DPI scaling on every tkinter size.
- The UI is the "instrument-mono" design (IBM Plex Mono, Consolas fallback; warm off-white +
  ink + amber/green). Source mockup: `design/Main.dc.html`.
- Distribution target is flightsim.to (Utilities category): must be a .zip, and any bundled
  `SimConnect.dll` needs its license included with redistribution rights stated.
