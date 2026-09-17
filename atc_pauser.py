#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 jshef747
# ATC Pauser is free software under the GNU AGPL-3.0; see LICENSE and NOTICE.
"""ATC Pauser - pause Microsoft Flight Simulator 2024 at a waypoint of your
choosing, or when your ATC add-on clears you for the arrival or issues a descent.

Two ways to arm:

  * Pick a waypoint from your SimBrief flight plan and the sim pauses a few
    miles before you reach it.  This works whichever ATC add-on you fly with.
  * Pick nothing and it falls back to the ATC add-on: the moment it clears you
    for the STAR or issues a descent, the sim pauses.

Two ATC providers are supported, chosen by the `provider` config key / the
Settings selector:

  * BeyondATC has no public API, but it is a Unity app that writes a running
    transcript of every ATC transmission to Player.log (a [ControllerScript] +
    [Instruction] pair, or a [CPDLC] uplink on datalink).  LogWatcher tails it.
  * SayIntentions.AI keeps no local transcript; its transcript lives in the
    cloud and is read with the user's API key via the getCommsHistory endpoint.
    SayIntentionsWatcher polls it.

Both watchers emit Trigger events onto one queue, so the pause path is identical:
the sim is frozen with SimConnect PAUSE_ON.  The waypoint arm reads the
aircraft's own position over SimConnect and is provider-independent.

Sections below, in order:
    1. Configuration
    2.  LogWatcher            - tails BeyondATC's Player.log and emits Trigger events
    2b. SayIntentionsWatcher  - polls SayIntentions' cloud API and emits Trigger events
    3. SimBrief        - fetches a flight plan and its waypoints
    4. SimLink         - keeps a SimConnect connection alive, pauses, reads position
    5. AtcProcessController - suspends/resumes the active ATC add-on's process
    6. Notifier        - optional Telegram push alerts on pause / resume
    7. GlobalHotkey    - listens for an optional system-wide pause shortcut
    8. UI              - a small always-on-top tkinter window
"""

from __future__ import annotations

import atexit
import ctypes
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from ctypes.wintypes import DWORD
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont

APP_NAME = "ATC Pauser"
APP_VERSION = "1.1"
if getattr(sys, "frozen", False):
    # PyInstaller: keep config.json beside the .exe, not in the temp unpack dir.
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "player_log": "auto",
    "simconnect_dll": "auto",

    # Your SimBrief Pilot ID (the number in SimBrief > Account Settings) or your
    # SimBrief username.  Leave "" to run without the waypoint feature - the
    # STAR / descent trigger below still works.
    "simbrief_id": "",
    # Pause this many nautical miles before the selected waypoint.
    "waypoint_arm_nm": 5.0,

    # Matched against whole log lines.  [ControllerScript] appears only on
    # ATC-initiated transmissions, so these can never fire on your own readback.
    "trigger_patterns": [
        r"^\[ControllerScript\] .*\bExpectStarInstructionScript$",
        r"^\[ControllerScript\] .*\bInitialDescentInstructionScript$",
        r"^\[ControllerScript\] .*\bDescentInstructionScript$",
    ],

    # Matched against the Content= field of CPDLC uplinks only - messages
    # addressed to you.  Deliberately broad; tighten once you have captured a
    # real descent uplink.
    "cpdlc_enabled": True,
    "cpdlc_uplink_content_patterns": [
        r"\bDESCEND\b",
        r"\bEXPECT\b.*\bARRIVAL\b",
        r"\bCLEARED\b.*\bARRIVAL\b",
    ],

    # MSFS 2024 has no SimConnect pause that stops the world clock, so a pause
    # freezes the aircraft but time of day keeps advancing.  With this on, the
    # app holds the clock still while paused by re-setting the Zulu time each
    # second - a complete freeze.  Set false to let time run during a pause.
    "hold_clock": True,

    # When to arm again after a pause fires:
    #   "resume" - re-arm as soon as you unpause (no countdown)
    #   "timer"  - rearm_seconds after the pause, resumed or not
    #   "manual" - never; it disarms and waits for you to press Arm
    "rearm_mode": "resume",
    "rearm_seconds": 30,        # only used by "timer" mode

    # ATC provider the clearance trigger listens to:
    #   "beyondatc"     - tail BeyondATC's local Player.log
    #   "sayintentions" - poll SayIntentions.AI's cloud getCommsHistory API
    #                     (needs your API key from the SayIntentions pilot portal)
    # The SimBrief waypoint arm below works the same with either provider.
    "provider": "beyondatc",
    "sayintentions_api_key": "",
    "sayintentions_process": "SayIntentions.exe",
    # Matched case-insensitively against what ATC said, to arm on arrival/descent.
    # Deliberately broad; tighten once you have seen SayIntentions' real wording.
    "si_trigger_patterns": [
        r"\bDESCEND\b",
        r"\bDESCENT\b",
        r"\bEXPECT\b.*\b(APPROACH|ARRIVAL|STAR)\b",
        r"\bCLEARED\b.*\b(APPROACH|ARRIVAL|ILS|RNAV|VISUAL)\b",
    ],
    "si_poll_interval_s": 3.0,

    # Also freeze the ATC add-on while paused, by suspending its process - it
    # stops talking and holds its state, then resumes where it left off.  The
    # process suspended is the active provider's (beyondatc_process, or
    # sayintentions_process for SayIntentions).  Key name kept for back-compat.
    "pause_beyondatc": True,
    "beyondatc_process": "BeyondATC.exe",

    # An optional Windows-wide shortcut which toggles MSFS pause even while its
    # window has focus.  Leave blank to disable it.  Set it in Settings by
    # clicking the field and pressing a combination, e.g. Ctrl+Alt+P.
    "pause_hotkey": "",

    # Optional Telegram push alerts, so a pause reaches your phone while you are
    # away from the PC.  Create a bot with @BotFather for the token, get your
    # numeric chat ID from @userinfobot, and paste both into the app (or here).
    # Left blank, notifications are simply off.
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "notify_on_pause": True,
    "notify_on_resume": True,

    "poll_interval_ms": 250,
    "instruction_timeout_s": 10.0,
    # BeyondATC writes to Player.log constantly while it runs, so a stale file
    # means it is closed - the file itself sticks around between sessions.
    "log_idle_seconds": 180,
    "start_armed": True,
    "always_on_top": True,
}


def load_config() -> dict:
    """Read config.json, writing the defaults out on first run."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            # utf-8-sig, not utf-8: Notepad and PowerShell both write a BOM, and
            # json.loads rejects one - which would silently discard the config.
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as exc:
            print(f"{APP_NAME}: ignoring unreadable config.json ({exc})", file=sys.stderr)
    else:
        try:
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"{APP_NAME}: could not write config.json ({exc})", file=sys.stderr)
    return cfg


def save_config(**updates) -> None:
    """Persist the given keys back to config.json, leaving every other key the
    user has set untouched."""
    data: dict = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            pass
    data.update(updates)
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"{APP_NAME}: could not save config ({exc})", file=sys.stderr)


def default_player_log() -> Path:
    root = os.environ.get("USERPROFILE") or str(Path.home())
    return (Path(root) / "AppData" / "LocalLow" / "Skirmish Mode Games, Inc"
            / "BeyondATC" / "Player.log")


@dataclass(frozen=True)
class Trigger:
    label: str      # STAR, DESCENT, CPDLC, WP FOOBAR ...
    text: str       # the clearance as BeyondATC worded it, or the waypoint
    at: float       # time.time()


class Status:
    """Attribute bag shared across threads.  Single attribute reads and writes
    are atomic under the GIL, and nothing here needs a consistent snapshot."""

    def __init__(self) -> None:
        self.sim_connected = False
        self.sim_paused = False
        self.sim_detail = "waiting for MSFS"
        self.log_ok = False
        self.log_detail = "starting…"      # provider watcher overwrites this quickly
        self.callsign = ""
        # Aircraft position, None until SimConnect reports it.
        self.plane_lat: float | None = None
        self.plane_lon: float | None = None
        self.sim_rate = 1.0     # simulation rate, 1.0 = real time


# ---------------------------------------------------------------------------
# 2. LogWatcher
# ---------------------------------------------------------------------------

RE_CONTROLLER_SCRIPT = re.compile(r"^\[ControllerScript\] (?:.*\.)?(\w+)\s*$")
RE_INSTRUCTION = re.compile(r"^\[Instruction\] (.*)$")
RE_CPDLC_QUEUED = re.compile(
    r"^\[CPDLC\] Message queued: ID=(\S+) Type=(\S+) From=(\S+) To=(\S+)(.*?) Content=(.*)$"
)
RE_CPDLC_DOWNLINK = re.compile(r"^\[CPDLC\] CPDLC_Relay: Inbound CPDLC from=(\S+) to=")
RE_STATION = re.compile(r"^[A-Z]{4}$")

_SCRIPT_LABELS = (
    ("ExpectStar", "STAR"),
    ("LeavingStar", "STAR"),
    ("InitialDescent", "DESCENT"),
    ("Descent", "DESCENT"),
    ("Vector", "VECTOR"),
)


def format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


def label_for_script(name: str) -> str:
    """Short display tag for a BeyondATC script class name."""
    for needle, label in _SCRIPT_LABELS:
        if needle in name:
            return label
    trimmed = re.sub(r"(Instruction)?Script$", "", name)
    return (trimmed or "ATC").upper()[:12]


class LogWatcher(threading.Thread):
    """Tails BeyondATC's Player.log and puts Trigger objects on a queue."""

    def __init__(self, cfg: dict, status: Status, out: "queue.Queue[Trigger]") -> None:
        super().__init__(name="LogWatcher", daemon=True)
        self.cfg = cfg
        self.status = status
        self.out = out
        self._stop = threading.Event()
        self._triggers = [re.compile(p) for p in cfg["trigger_patterns"]]
        self._cpdlc = [re.compile(p) for p in cfg["cpdlc_uplink_content_patterns"]]
        raw = cfg["player_log"]
        self.path = default_player_log() if raw == "auto" else Path(raw)
        self._key = None        # (st_dev, st_ino) of the file we are following
        self._pos = 0           # byte offset we have consumed up to
        self._buf = b""         # bytes of an incomplete trailing line
        self._pending = None    # (label, monotonic) awaiting its [Instruction] line

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        interval = max(0.05, self.cfg["poll_interval_ms"] / 1000.0)
        while not self._stop.is_set():
            try:
                self._tick()
            except OSError as exc:
                self._reset()
                self.status.log_ok = False
                self.status.log_detail = f"read error: {exc.strerror or exc}"
            self._stop.wait(interval)

    def _tick(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            self._reset()
            self.status.log_ok = False
            self.status.log_detail = "waiting for BeyondATC"
            return

        key = (st.st_dev, st.st_ino)
        if self._key is None:
            # First sight of the log: start at the end, so the megabytes of
            # history already in the file can never fire a pause.
            self._reset(key=key, pos=st.st_size)
        elif key != self._key or st.st_size < self._pos:
            # BeyondATC restarted - Unity truncates or replaces Player.log.
            # Read the new session from the top.
            self._reset(key=key, pos=0)
            self.status.callsign = ""

        # Keep tailing regardless, but only call the link healthy while the file
        # is actually being written to - BeyondATC's log outlives BeyondATC.
        age = max(0.0, time.time() - st.st_mtime)
        if age <= self.cfg["log_idle_seconds"]:
            self.status.log_ok = True
            self.status.log_detail = "log active"
        else:
            self.status.log_ok = False
            self.status.log_detail = f"idle for {format_age(age)}"

        if st.st_size > self._pos:
            # Open only to take the delta, then close again immediately.  Holding
            # the log open would make Windows refuse some writers the file, and
            # BeyondATC opens its own log after we may already be running.
            with self.path.open("rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
            self._pos += len(chunk)
            self._buf += chunk
            lines = self._buf.split(b"\n")
            self._buf = lines.pop()         # keep any partial trailing line
            for raw in lines:
                self._feed(raw.decode("utf-8", "replace").rstrip("\r"))

        # Fire an armed voice trigger even if its [Instruction] line never
        # lands: pausing is the job, the text is decoration.
        if self._pending is not None:
            label, started = self._pending
            if time.monotonic() - started > self.cfg["instruction_timeout_s"]:
                self._pending = None
                self._emit(label, "")

    def _reset(self, key=None, pos: int = 0) -> None:
        self._key = key
        self._pos = pos
        self._buf = b""
        self._pending = None

    def _feed(self, line: str) -> None:
        if self.cfg["cpdlc_enabled"] and line.startswith("[CPDLC] "):
            self._feed_cpdlc(line)
            return

        match = RE_CONTROLLER_SCRIPT.match(line)
        if match:
            # Any ATC-initiated call supersedes a still-pending one.
            if any(r.search(line) for r in self._triggers):
                self._pending = (label_for_script(match.group(1)), time.monotonic())
            else:
                self._pending = None
            return

        if self._pending is not None:
            match = RE_INSTRUCTION.match(line)
            if match:
                label, _ = self._pending
                self._pending = None
                self._emit(label, match.group(1).strip())

    def _feed_cpdlc(self, line: str) -> None:
        match = RE_CPDLC_DOWNLINK.match(line)
        if match:
            # The relay only ever reports traffic inbound from us, so the
            # from= field on this line is our own callsign.
            self.status.callsign = match.group(1)
            return

        match = RE_CPDLC_QUEUED.match(line)
        if not match:
            return
        sender, recipient, content = match.group(3), match.group(4), match.group(6).strip()
        if not self._is_uplink(sender, recipient):
            return
        if any(r.search(content) for r in self._cpdlc):
            self._emit("CPDLC", content)

    def _is_uplink(self, sender: str, recipient: str) -> bool:
        """True when ATC sent this to us, rather than us sending it to ATC."""
        callsign = self.status.callsign
        if callsign:
            return recipient == callsign
        return bool(RE_STATION.match(sender))

    def _emit(self, label: str, text: str) -> None:
        self.out.put(Trigger(label, text, time.time()))


# ---------------------------------------------------------------------------
# 2b. SayIntentionsWatcher
# ---------------------------------------------------------------------------

SI_COMMS_URL = "https://apipri.sayintentions.ai/sapi/getCommsHistory"


def si_label(text: str) -> str:
    """Short display tag for a SayIntentions ATC line, mirroring the BeyondATC
    STAR / DESCENT tags."""
    upper = text.upper()
    if "DESCEND" in upper or "DESCENT" in upper:
        return "DESCENT"
    if any(w in upper for w in ("APPROACH", "ARRIVAL", "STAR", "ILS", "RNAV", "VISUAL")):
        return "STAR"
    return "ATC"


class SayIntentionsWatcher(threading.Thread):
    """Polls SayIntentions.AI's cloud getCommsHistory API and puts Trigger
    objects on the same queue LogWatcher uses, so everything downstream is
    unchanged.

    SayIntentions keeps no local ATC transcript (its local files are sim
    telemetry); the transcript lives in the cloud and is read with the user's
    API key.  Each poll asks only for entries newer than the last id seen, so
    this tails the flight the way LogWatcher tails Player.log - and, like it, it
    baselines to the current end on first sight (and whenever a new flight
    starts) so the server-side backlog can never fire a pause.  Every network /
    JSON error is swallowed and reported in the status line; the thread never
    dies, and the key only ever goes to SayIntentions' own API."""

    RETRY_SECONDS = 10.0        # back off to this after an error

    def __init__(self, cfg: dict, status: Status, out: "queue.Queue[Trigger]") -> None:
        super().__init__(name="SayIntentionsWatcher", daemon=True)
        self.cfg = cfg
        self.status = status
        self.out = out
        self._stop = threading.Event()
        self._patterns = [re.compile(p, re.IGNORECASE) for p in cfg["si_trigger_patterns"]]
        self._api_key = str(cfg.get("sayintentions_api_key") or "").strip()
        self._interval = max(1.0, float(cfg.get("si_poll_interval_s", 3.0)))
        self._since_id: int | None = None   # None until baselined to the flight's end
        self._flight_id = None              # a change means a new flight -> re-baseline

    def stop(self) -> None:
        self._stop.set()

    def update_key(self, key: str) -> None:
        """Adopt a new API key mid-run and re-baseline on the next poll."""
        self._api_key = (key or "").strip()
        self._since_id = None
        self._flight_id = None

    def run(self) -> None:
        while not self._stop.is_set():
            if not self._api_key:
                self.status.log_ok = False
                self.status.log_detail = "no API key"
                self._stop.wait(self._interval)
                continue
            wait = self._interval
            try:
                self._poll()
            except Exception as exc:        # never let the polling thread die
                self._note_error(exc)
                wait = self.RETRY_SECONDS
            self._stop.wait(wait)

    def _poll(self) -> None:
        params = {"api_key": self._api_key}
        if self._since_id is not None:
            params["since_id"] = self._since_id
        url = SI_COMMS_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))

        entries = data.get("comm_history")
        flight = data.get("flight_id")
        if not isinstance(entries, list) or not flight:
            # Key is valid, but no active flight session to read yet.
            self.status.log_ok = False
            self.status.log_detail = "waiting for a SayIntentions flight"
            self._since_id = None
            self._flight_id = None
            return

        # First sight of a flight (or a new flight) baselines to the current end
        # so history already on the server can never fire.
        if flight != self._flight_id or self._since_id is None:
            self._flight_id = flight
            self._since_id = self._max_id(entries, 0)
            self.status.log_ok = True
            self.status.log_detail = "listening"
            return

        self.status.log_ok = True
        self.status.log_detail = "listening"
        # Fire on new ATC messages, oldest first, then advance the cursor.
        for entry in sorted(entries, key=self._entry_id):
            eid = self._entry_id(entry)
            if eid <= self._since_id:
                continue
            self._since_id = eid
            atc = str(entry.get("outgoing_message_english")
                      or entry.get("outgoing_message") or "").strip()
            if atc and any(p.search(atc) for p in self._patterns):
                self._emit(si_label(atc), atc)

    @staticmethod
    def _entry_id(entry) -> int:
        try:
            return int(entry.get("id"))
        except (TypeError, ValueError, AttributeError):
            return 0

    def _max_id(self, entries, floor: int) -> int:
        return max([floor] + [self._entry_id(e) for e in entries])

    def _note_error(self, exc: Exception) -> None:
        self.status.log_ok = False
        if isinstance(exc, urllib.error.HTTPError):
            self.status.log_detail = ("auth failed - check API key"
                                      if exc.code in (401, 403) else f"HTTP {exc.code}")
        elif isinstance(exc, (urllib.error.URLError, OSError)):
            self.status.log_detail = "no connection"
        else:
            self.status.log_detail = "bad response"

    def _emit(self, label: str, text: str) -> None:
        self.out.put(Trigger(label, text, time.time()))


def si_check_key(key: str) -> tuple[bool, str]:
    """One-shot validation of a SayIntentions API key for the Test button."""
    url = SI_COMMS_URL + "?" + urllib.parse.urlencode({"api_key": key.strip()})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return False, ("key rejected - check it" if exc.code in (401, 403)
                       else f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return False, f"no connection ({getattr(exc, 'reason', exc)})"
    except ValueError:
        return False, "bad response"
    if isinstance(data, dict) and data.get("error"):
        return False, str(data.get("error"))[:60]
    if isinstance(data, dict) and data.get("flight_id"):
        return True, "key works - flight active"
    return True, "key accepted - start a flight to arm"


# ---------------------------------------------------------------------------
# 3. SimBrief flight plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Waypoint:
    ident: str
    lat: float
    lon: float
    stage: str      # CLB, CRZ, DES ...
    kind: str       # wpt, apt, vor ...


@dataclass(frozen=True)
class Plan:
    summary: str            # "EGLL -> LKPR  ·  42 waypoints"
    waypoints: list[Waypoint]


def nm_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    r = 3440.065  # mean earth radius in nm
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def fetch_simbrief_plan(ident: str) -> Plan:
    """Fetch the latest OFP for a SimBrief Pilot ID or username.

    Raises RuntimeError with a short, user-facing message on any failure.
    """
    ident = ident.strip()
    # A Pilot ID is all digits; anything else is treated as a username.
    key = "userid" if ident.isdigit() else "username"
    url = ("https://www.simbrief.com/api/xml.fetcher.php?"
           + urllib.parse.urlencode({key: ident, "json": "1"}))
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"no connection ({exc.reason})") from exc
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"bad response ({exc})") from exc

    fetch = data.get("fetch") or {}
    status = str(fetch.get("status", "")).strip()
    if status and status.lower() != "success" and "navlog" not in data:
        # SimBrief reports e.g. "Error: Unknown UserID" here.
        raise RuntimeError(status)

    navlog = data.get("navlog") or {}
    fixes = navlog.get("fix") or []
    if isinstance(fixes, dict):            # a one-fix plan decodes as a bare dict
        fixes = [fixes]

    waypoints: list[Waypoint] = []
    for fix in fixes:
        if not isinstance(fix, dict):
            continue
        try:
            lat = float(fix["pos_lat"])
            lon = float(fix["pos_long"])
        except (KeyError, TypeError, ValueError):
            continue
        waypoints.append(Waypoint(
            ident=str(fix.get("ident") or "?"),
            lat=lat, lon=lon,
            stage=str(fix.get("stage") or "").strip().upper(),
            kind=str(fix.get("type") or "").strip().lower(),
        ))

    if not waypoints:
        raise RuntimeError("no waypoints in plan")

    origin = (data.get("origin") or {}).get("icao_code") or "????"
    dest = (data.get("destination") or {}).get("icao_code") or "????"
    summary = f"{origin} → {dest}   ·   {len(waypoints)} waypoints"
    return Plan(summary=summary, waypoints=waypoints)


# ---------------------------------------------------------------------------
# 4. SimLink
# ---------------------------------------------------------------------------

class SimLink(threading.Thread):
    """Keeps a SimConnect connection to MSFS, sends pause events, and reports
    the aircraft's position so the waypoint arm can measure distance."""

    RETRY_SECONDS = 5.0

    def __init__(self, cfg: dict, status: Status) -> None:
        super().__init__(name="SimLink", daemon=True)
        self.cfg = cfg
        self.status = status
        self.cmds: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._sm = None
        self._events = {}
        self._air = None
        self._zulu = None
        self._commanded_pause = False
        # MSFS 2024 has no SimConnect pause that stops the world clock (verified:
        # PAUSE_ON / PAUSE_SET both give an active pause, the clock keeps running).
        # So for a complete freeze we active-pause the aircraft AND hold the time
        # of day still by re-setting the Zulu clock while paused.
        self._hold_clock = bool(cfg.get("hold_clock", True))
        self._frozen_hm: tuple[int, int] | None = None
        self._last_hold = 0.0       # monotonic; throttles the re-set to ~1 Hz

    def pause(self) -> None:
        self.cmds.put("PAUSE_ON")

    def resume(self) -> None:
        self.cmds.put("PAUSE_OFF")

    def set_rate(self, target: float) -> None:
        self.cmds.put(f"RATE:{target}")

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._sm is None:
                if not self._connect():
                    self._stop.wait(self.RETRY_SECONDS)
                    continue
            try:
                name = self.cmds.get(timeout=0.25)
            except queue.Empty:
                self._poll()
                continue
            self._send(name)
        self._disconnect()

    def _connect(self) -> bool:
        try:
            import logging

            from SimConnect import AircraftRequests, SimConnect
        except ImportError as exc:
            self.status.sim_detail = f"SimConnect package missing ({exc})"
            return False

        # The library logs failed calls at error level; with no sim running that
        # is once every retry, which is noise rather than news.
        logging.getLogger("SimConnect").setLevel(logging.CRITICAL)

        kwargs = {}
        dll = self.cfg["simconnect_dll"]
        if dll and dll != "auto":
            kwargs["library_path"] = dll

        try:
            sm = SimConnect(**kwargs)
        except Exception:
            self.status.sim_connected = False
            self.status.sim_detail = "waiting for MSFS"
            return False

        # SimConnect() does not reliably raise when the sim is absent - it can
        # hand back an object whose handle was never opened, which would make us
        # claim to be connected and then silently drop every pause.  Prove the
        # link by mapping the events we need before trusting it.
        events = {}
        healthy = bool(getattr(sm, "ok", False))
        if healthy:
            try:
                for name in (b"PAUSE_ON", b"PAUSE_OFF"):
                    event = sm.map_to_sim_event(name)
                    if event is None:
                        healthy = False
                        break
                    events[name] = event
                # Extras: SIM_RATE_* drive time acceleration, ZULU_*_SET hold the
                # clock still while paused.  All best-effort - a failure to map
                # any of them just disables that feature, it does not sink the
                # connection.
                if healthy:
                    for extra in (b"SIM_RATE_INCR", b"SIM_RATE_DECR",
                                  b"ZULU_HOURS_SET", b"ZULU_MINUTES_SET"):
                        evt = sm.map_to_sim_event(extra)
                        if evt is not None:
                            events[extra] = evt
            except Exception:
                healthy = False

        if not healthy:
            self._shutdown(sm)
            self.status.sim_connected = False
            self.status.sim_detail = "waiting for MSFS"
            return False

        # Position is a convenience, not a requirement - a failure here must not
        # break pausing, so it is guarded separately and left as None on error.
        try:
            air = AircraftRequests(sm, _time=1000)
        except Exception:
            air = None

        # ZULU TIME is not in AircraftRequests' registered set, so read it with a
        # direct Request - this is the value we pin the clock to while paused.
        try:
            from SimConnect.RequestList import Request
            zulu = Request((b"ZULU TIME", b"Seconds"), sm, _time=200)
        except Exception:
            zulu = None

        self._sm = sm
        self._events = events
        self._air = air
        self._zulu = zulu
        self._commanded_pause = False
        self.status.sim_connected = True
        self.status.sim_detail = "connected"
        return True

    @staticmethod
    def _shutdown(sm) -> None:
        try:
            sm.exit()
        except Exception:
            # exit() joins a dispatch thread that a failed connect never started
            pass

    def _poll(self) -> None:
        sm = self._sm
        if sm is None:
            return
        if getattr(sm, "quit", 0) == 1:
            self._disconnect()
            self.status.sim_detail = "MSFS closed"
            return
        # python-simconnect's own Paused/Unpaused subscription is unreliable, so
        # fall back to what we last asked the sim to do.
        self.status.sim_paused = bool(getattr(sm, "paused", False)) or self._commanded_pause
        self._read_position()
        self._hold_clock_tick()

    def _read_position(self) -> None:
        if self._air is None:
            return
        try:
            lat = self._air.get("PLANE_LATITUDE")
            lon = self._air.get("PLANE_LONGITUDE")
            rate = self._air.get("SIMULATION_RATE")
        except Exception:
            return
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            self.status.plane_lat = float(lat)
            self.status.plane_lon = float(lon)
        if isinstance(rate, (int, float)) and rate > 0:
            self.status.sim_rate = float(rate)

    def _capture_zulu(self) -> tuple[int, int] | None:
        """Read the current Zulu time as (hours, minutes) to hold it at."""
        if not self._hold_clock or self._zulu is None:
            return None
        try:
            secs = self._zulu.value
        except Exception:
            return None
        if not isinstance(secs, (int, float)):
            return None
        total = int(secs) % 86400
        return total // 3600, (total % 3600) // 60

    def _hold_clock_tick(self) -> None:
        """While paused, re-set the Zulu clock to the frozen time about once a
        second.  MSFS 2024 has no clock-stopping pause, so pinning the time this
        way is what keeps the sun and time of day still during a pause."""
        if self._frozen_hm is None or not self.status.sim_paused:
            return
        now = time.monotonic()
        if now - self._last_hold < 0.5:
            return
        hset = self._events.get(b"ZULU_HOURS_SET")
        mset = self._events.get(b"ZULU_MINUTES_SET")
        if hset is None or mset is None:
            return
        hh, mm = self._frozen_hm
        try:
            self._sm.send_event(hset, DWORD(hh))
            self._sm.send_event(mset, DWORD(mm))
        except Exception:
            return
        self._last_hold = now

    def _send(self, name: str) -> None:
        sm = self._sm
        if sm is None:
            return
        if name.startswith("RATE:"):
            self._set_rate(float(name[5:]))
            return
        want_pause = name == "PAUSE_ON"
        try:
            sm.send_event(self._events[name.encode()])
        except Exception as exc:
            self.status.sim_detail = f"send failed: {exc}"
            self._disconnect()
            return
        self._commanded_pause = want_pause
        self.status.sim_paused = self._commanded_pause
        # Capture the moment to hold the clock at on pause; release it on resume.
        self._frozen_hm = self._capture_zulu() if want_pause else None

    def _set_rate(self, target: float) -> None:
        """Step the sim rate to a power-of-two target with INCR / DECR events.

        SIMULATION RATE is read-only, so the rate can only be nudged one notch
        (a doubling or halving) at a time.  We compute the number of notches
        from the current rate rather than re-reading between steps, which the
        1 s simvar cache would make stale anyway."""
        sm = self._sm
        if sm is None:
            return
        incr = self._events.get(b"SIM_RATE_INCR")
        decr = self._events.get(b"SIM_RATE_DECR")
        if incr is None or decr is None:
            return
        current = self.status.sim_rate if self.status.sim_rate > 0 else 1.0
        try:
            steps = int(round(math.log2(target / current)))
        except (ValueError, ZeroDivisionError):
            return
        steps = max(-6, min(6, steps))
        event = incr if steps > 0 else decr
        for _ in range(abs(steps)):
            try:
                sm.send_event(event)
            except Exception as exc:
                self.status.sim_detail = f"send failed: {exc}"
                self._disconnect()
                return
            time.sleep(0.05)
        # Optimistic; _poll corrects it from the real simvar within a second.
        self.status.sim_rate = target

    def _disconnect(self) -> None:
        sm, self._sm = self._sm, None
        self._events = {}
        self._air = None
        self._zulu = None
        self._commanded_pause = False
        self.status.sim_connected = False
        self.status.sim_paused = False
        self.status.plane_lat = None
        self.status.plane_lon = None
        self.status.sim_rate = 1.0
        self._frozen_hm = None
        self.status.sim_detail = "waiting for MSFS"
        if sm is not None:
            self._shutdown(sm)


# ---------------------------------------------------------------------------
# 5. BeyondATC process control
# ---------------------------------------------------------------------------

class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class AtcProcessController:
    """Suspends and resumes the BeyondATC process so it freezes with the sim.

    BeyondATC has no API, so the only reliable way to make it stop talking and
    hold its state during a pause is to freeze its threads at the OS level
    (NtSuspendProcess) and thaw them on resume.  Fully reversible; an atexit
    hook makes sure we never leave it frozen when the app closes normally."""

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_SUSPEND_RESUME = 0x0800
    _INVALID = ctypes.c_void_p(-1).value

    def __init__(self, process_name: str) -> None:
        self.name = process_name
        self._suspended = False
        self._armed_atexit = False
        try:
            self._k32 = ctypes.windll.kernel32
            self._ntdll = ctypes.windll.ntdll
            k = self._k32
            k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            k.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32)]
            k.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32)]
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
            self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            self._ok = True
        except (AttributeError, OSError):
            self._ok = False

    @property
    def suspended(self) -> bool:
        return self._suspended

    def suspend(self) -> None:
        if not self._ok or self._suspended:
            return
        if self._apply(self._ntdll.NtSuspendProcess):
            self._suspended = True
            if not self._armed_atexit:
                atexit.register(self.resume)    # never leave it frozen on exit
                self._armed_atexit = True

    def resume(self) -> None:
        if not self._ok or not self._suspended:
            return
        self._apply(self._ntdll.NtResumeProcess)
        self._suspended = False

    def _apply(self, fn) -> bool:
        applied = False
        for pid in self._find_pids():
            handle = self._k32.OpenProcess(self.PROCESS_SUSPEND_RESUME, False, pid)
            if not handle:
                continue
            try:
                fn(wintypes.HANDLE(handle))
                applied = True
            except OSError:
                pass
            finally:
                self._k32.CloseHandle(wintypes.HANDLE(handle))
        return applied

    def _find_pids(self) -> list[int]:
        snap = self._k32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snap or snap == self._INVALID:
            return []
        pids: list[int] = []
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        try:
            if not self._k32.Process32First(wintypes.HANDLE(snap), ctypes.byref(entry)):
                return []
            target = self.name.lower()
            while True:
                exe = entry.szExeFile.decode("latin-1", "ignore")
                if exe.lower() == target:
                    pids.append(entry.th32ProcessID)
                if not self._k32.Process32Next(wintypes.HANDLE(snap), ctypes.byref(entry)):
                    break
        finally:
            self._k32.CloseHandle(wintypes.HANDLE(snap))
        return pids


# ---------------------------------------------------------------------------
# 6. Notifier
# ---------------------------------------------------------------------------

class Notifier:
    """Sends a Telegram message when the sim pauses or resumes.

    Telegram's Bot API is a single HTTPS call, so this needs nothing beyond the
    stdlib.  Every alert goes out on a throwaway daemon thread with a short
    timeout and swallows all errors - a slow network or a bad token must never
    delay or break the pause itself.  Blank token/chat means simply disabled."""

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def update(self, token: str, chat_id: str) -> None:
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()

    def notify(self, text: str) -> None:
        """Fire-and-forget alert: never blocks the caller, never raises."""
        if not self.configured:
            return
        threading.Thread(target=self._send, args=(text,),
                         name="Notify", daemon=True).start()

    def send_sync(self, text: str) -> tuple[bool, str]:
        """Blocking send for the Test button - returns (ok, short_message)."""
        if not self.configured:
            return False, "enter a bot token and chat ID first"
        return self._send(text)

    def _send(self, text: str) -> tuple[bool, str]:
        url = self.API.format(token=self.token)
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
        }).encode()
        try:
            with urllib.request.urlopen(url, data=payload, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # Telegram still returns a JSON "description" on a 4xx (bad token,
            # wrong chat ID, bot never started); surface it - it is the fix.
            return False, self._http_detail(exc)
        except (urllib.error.URLError, OSError) as exc:
            return False, f"no connection ({getattr(exc, 'reason', exc)})"
        except ValueError as exc:
            return False, f"bad response ({exc})"
        if body.get("ok"):
            return True, "sent"
        return False, str(body.get("description") or "rejected by Telegram")

    @staticmethod
    def _http_detail(exc: "urllib.error.HTTPError") -> str:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            return str(body.get("description") or f"HTTP {exc.code}")
        except (ValueError, OSError):
            return f"HTTP {exc.code}"


# ---------------------------------------------------------------------------
# 7. Global pause hotkey
# ---------------------------------------------------------------------------

_HOTKEY_MODIFIERS = {
    "ALT": 0x0001,
    "CTRL": 0x0002,
    "SHIFT": 0x0004,
    "WIN": 0x0008,
}
_HOTKEY_MODIFIER_NAMES = {
    "CONTROL": "CTRL",
    "CTRL": "CTRL",
    "ALT": "ALT",
    "SHIFT": "SHIFT",
    "WINDOWS": "WIN",
    "WIN": "WIN",
}
_HOTKEY_KEYS = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "PAUSE": 0x13,
}


def parse_hotkey(value: str) -> tuple[str, int, int]:
    """Return a canonical display string plus RegisterHotKey flags and key.

    A normal letter or number must have a modifier: registering a bare letter
    would steal ordinary typing from every program.  Function keys and Pause
    are useful standalone exceptions for a cockpit button mapping.
    """
    parts = [part.strip().upper() for part in value.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty")
    key_name = parts.pop()
    modifiers: list[str] = []
    for part in parts:
        name = _HOTKEY_MODIFIER_NAMES.get(part)
        if name is None or name in modifiers:
            raise ValueError("use Ctrl, Alt, Shift, Win, and one key")
        modifiers.append(name)

    is_function_key = bool(re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", key_name))
    if len(key_name) == 1 and key_name.isalnum():
        key = ord(key_name)
    elif is_function_key:
        key = 0x70 + int(key_name[1:]) - 1
    else:
        key = _HOTKEY_KEYS.get(key_name)
        if key is None:
            raise ValueError("use a letter, number, F1–F24, or a named key")
    if not modifiers and key_name != "PAUSE" and not is_function_key:
        raise ValueError("add Ctrl, Alt, Shift, or Win")
    flags = 0
    for name in modifiers:
        flags |= _HOTKEY_MODIFIERS[name]
    return "+".join([*modifiers, key_name]), flags, key


class GlobalHotkey(threading.Thread):
    """Receives a RegisterHotKey message on its own Windows message queue.

    Tk owns the UI thread's message loop, so keeping the registration in this
    tiny daemon thread makes the shortcut work with MSFS in front and leaves
    all Tk work on the UI thread via ``events``.
    """

    HOTKEY_ID = 0x4250
    WM_HOTKEY = 0x0312
    PM_REMOVE = 0x0001
    MOD_NOREPEAT = 0x4000

    def __init__(self, value: str) -> None:
        super().__init__(name="GlobalHotkey", daemon=True)
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._requests: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._value = value

    def configure(self, value: str) -> None:
        self._requests.put(value)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                          wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL

        registered = False
        active_value: str | None = None
        try:
            while not self._stop.is_set():
                requested = self._value
                while True:
                    try:
                        requested = self._requests.get_nowait()
                    except queue.Empty:
                        break
                self._value = requested
                if requested != active_value:
                    if registered:
                        user32.UnregisterHotKey(None, self.HOTKEY_ID)
                        registered = False
                    active_value = requested
                    if not requested:
                        self.events.put(("disabled", "global pause hotkey is off"))
                    else:
                        try:
                            display, flags, key = parse_hotkey(requested)
                            registered = bool(user32.RegisterHotKey(
                                None, self.HOTKEY_ID, flags | self.MOD_NOREPEAT, key))
                            if registered:
                                self.events.put(("ready", f"global: {display}"))
                            else:
                                self.events.put(("error", "could not register - already in use?"))
                        except ValueError as exc:
                            self.events.put(("error", str(exc)))
                msg = wintypes.MSG()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, self.PM_REMOVE):
                    if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                        self.events.put(("pressed", ""))
                self._stop.wait(0.05)
        finally:
            if registered:
                user32.UnregisterHotKey(None, self.HOTKEY_ID)


# ---------------------------------------------------------------------------
# 8. UI
# ---------------------------------------------------------------------------

# Instrument-mono palette: warm off-white paper, ink lines, amber + green.
BG = "#f2f0eb"              # window interior (paper)
CARD_BG = "#ffffff"        # input / readout fields
INK = "#1c1a17"
FG = INK
MUTED = "#6a665d"
FAINT = "#8f897d"
LINE = "#c9c5bc"           # hairline panel borders
GREEN = "#2f9e52"
AMBER = "#d98a2b"
GREY = "#b6b1a7"           # inactive status dot
ARMED_BG = "#2f9e52"
TRIGGERED_BG = "#b23b34"
DISARMED_BG = "#6a665d"
BANNER_SUB_FG = "#dff1e5"
HEADER_BG = "#1c1a17"      # dark title band
HEADER_FG = "#f2efe9"
BTN_BG = "#f2f0eb"
BTN_LINE = INK
BTN_PRIMARY_BG = "#1c1a17"     # filled (Pause sim / Load)
BTN_PRIMARY_FG = "#f2efe9"
SPEED_ON_BG = "#d98a2b"        # selected time-acceleration notch
SPEED_ON_FG = "#1c1a17"

DOT = "■"                  # square marker, to suit the boxy panel look
MIDDOT = "·"

SPEEDS = (1.0, 2.0, 4.0)

# Dropdown entry that means "no waypoint - use the STAR / descent trigger".
NO_WP_LABEL = "— arrival / descent —"

# Window/taskbar icon: an amber airliner on an ink rounded square, embedded as a
# base64 PNG so the app stays a single file with no external asset to ship.
ICON_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAASM0lEQVR4nM1be4wd11n/nTMz9969d9dre7O21+v3M7GR4xpC4sShUZaShx1DUVThQqJEBVQiJKQCfxT+4SXSRyiIUlEkCDSkNTRyaWloKY4bFRw5bZMSB+IkbuJXNk5s14+s93XvnZmDvvOYOXPmzN1NBBJHXt+5M+fxvc73/b7vzGXIGweQAgzXX7/tlqvvXHkwTeOxJElXxnEcMIY5mukgetxnnuduXwEGpnuZvnON87cgCBLOg/EgCA4NtAYePfrSsWf0PJpXZNTJGzt37lxy7u3xRzrtzj6RijAVso8kqMCLS9v/0yaEIpIxBs553Gg09m9YvvI3//Xw4QuGZ2Yudu3atWX89MknO93O2jQVYAwJwLjNorECOa2lEKOfua1kLorfo0A1LYIBTBRpEUJ+TQERcMZRq0UnVo2u2v2dI0deId6JQezZ8/5rxs+c+man014rhOjqwYGaUmizLDJMfdx7WuDVxjqXFfuYV8vPqxHz8tPadZrMgD5SIbrtTmfdmTff+ObY2NgQjPaPvXTiM51Oe5UAugCLckpY9udq3Vzbu5M5REuBWH/ZHIqwAt9mjmyM/dBxAT55FIZVCy0SAt1Op7Pm5GvH/xSMpWzr1q03XLl04dkkSWmvSIuw184XVM7Jvlkwe/e7zZDDKGdAO6Ztpu6kAmiETHolZpuzy6FLFKtew+WjeE+kQRCwoeGhm/jM1MRHhRBEk+wjTdtj3iUr0P1sYkoWXFhVFJhfOsDx9/ta+OKH+7F8AcdsLKQ5ZnM78zhTedu8YoWcmIk0TdnUxNRDPI6TMektWb6+WaNgiaxigaoVM6LNTCqW0FIkhM/sbeL960LcujbEn/1sCyGHtATqRH1sreZ0V9A2Nyl5X7X9OPEcJ8ntPEniUcNiNpHtTIgg2/vr59mfvYCHEsW2+p9c7kRbYM+WGnatCXF+Ssi/nasD7N1Sx9W2QGCpvuQPXMY8gqpqxrItopDE8ShPUxFm2nGhh15A7kvvQs5Ny9nZQrEFS5q+Z0uEOCVLYCCE1U2AvVujzApKW8kW7DwYdrewj2LiOE4S0kmZiSyu24NsCRamysdL+tyVLYl2E4El/RzXLQ3RToCAK6ug681LAiwb4OgkRQkU5jOOyrOuCRb+px5r0fxwn6xs1gqhrceSyo3kXjzrp4mmZ6T14X6OBQ2GRIFM2YjnBX1cPosJfhWmENlWK9JQpLsamPptwQiEz9XVFkYuwXKMyPo4ixvB0H8q3NE2YKWtFjKBvlABdB8dtlX2Mu8inQ6VxqdZz7k9eXmcjyV/84skn6XgGKvipeNsCzfNvrbSkl6ouYRHjHU6mIH3mKO0BOvhCzKNzgPL5/rPJ50PM0aQNsCstAaPFbi3hBFAb8fKPHvHdlRlUfsIq9S6hV9tAG4/NOHQhGVb+/OMgiVK7TQ4a+7C1Y0pQFGyhqrYUB4v57A4sfduLsD8f5/2Kkhzmcm++6yUewdX+FIb6xcSD/96hSvbJIWvF9MCcfr4+LFDY0mrZQ1k3zN8YAE9bgbqvKR6T3mSkCrmfevbbW6QUvw0OCQbb1lfIZr0cCSluZhtAUw/dCYsTGABpExQFdi8+K34hMBPPlQ5DGHlCC6YMfI2fqDg3PTDTGAGi3jQYCE8umEQDmOuBlwzc1Pg7BmZlNRM2d2o0gsw2bH2o+3WAUx1VMboDWEmS2WqT+ZsLRoK2q0wQRfYcXltqdAOMTbusb9nkrSlz4A4EZjpContbSYI47e7kOhvz3UhOKXGelE1l5CC231dTX6n1NhYipmb5qS541T5CleZPn6rfIV9g40sHc4El21vBy0Z/1Ca3NoKROCiPoZr+hnemRE4PykQBZDZ3TuzKgd4+K4G7rw2wtWOZdZaUnS9oA5863gXH/+Xabx9VWBhH5MwmeDxcD/DYIPhR1MCl2cEonIW488F7DohPP5rZOmw4aXUoSqA2Qso7agCx2P7+jGygGO6A3zky5P4r7dizMbAzWtCPLKnifVDXBJPUFhq30AgLQ2yoIVNjlOXUvzW16dx+GSMvgjYuizEX3+ohf4aw7mrCe7fP4WzEynqVEVyiHR1ldFbUWHic3nkbKIKh0e3SfsjAxxrFwcy4Vk+yLF8gOPiVIqHdtbxxX0trBjUzJNJlCStRBEGHFdmhKwQPf7hFn79ljouTgs53+ggl3OvWRxKIXfpBMNj4q4T9yJGi4HQjWqZabrMZ1HAJ1+V0cm9D2C2K9BXAz77wSZ+5caGZLwba+atkJVljtampj4zsdLMH9zZh7VDAZ4bV5YkrS1VWaVLZxF4ecpXFqO2NYSKJQsDGshpbwHRC+LkPJCHJsKnOwK/O9aHoabasxQBbFPLI4CjQq0ukhPRRGN/YXsNd26OpAM0YbIq37DRYwadPXHdHs59rsMbQZxFbU/MPU6zWQMmqcTlriBUNCCLMTOk9J2OLqwNxvS8FBrJD5g5Ul0fKO3dcuTtjcbyMCis2K8reJbXLGxXzwQU4ohI0ppNFDknE/vtUBUFDAMNhlZNz86AVp1hoM6kc0wdTmjORLtpCp3Uh9ac7NB1kR47TNtRwEe/kT8v4vF8UCFRMSjMBkdMmeqFKYGfWhfij+9q6CqPEagNoygBZgh1/8MnYvznmzESQWcCDM+PJzh8KpEmT0wVY3s+DwGsJBV4+O4+3LYulHPZgi+s6GPcQpOGV24Ju2czm4M+yazJEb3TFvjVm+p4fF8L64cCGQ4ziKvFZZwmmTxp+okX2tj76ATu2z+JS9PAlekUD+y/ir1/M4F/fKGN/npeLstH5yR2U4E1iylK9OOjN9UlDUQL0eQiwSy7tP2Ak8Fyh8PytSMYaX5tgXoIfPbnWvijOxqSuemuqvJmWtOryKNuoWv+emvQWAlktB+hOeuhdsayrzotMkKwtyJZwQyhSgH84V1N/MUHm3I+KqlL67HK9b6IZvMmjA+Yh+qlmQWcybi8bSTAE/e1cO+2CJdnFGXKDHMLkM5OCPlHBDaj3PQyAoU8hc4rtrrQ0qxxNCJCgQoJZq7R0CJrigyXplPcu62OA/f3Y/vyABenU+V0Lczi0aGbDLFCuHNDnzF5IoRAygM31PEPv9SPjcOBjO9BkCuc+lI/Ipy0sbDBsLCP4/QVgaNvJQjpvNnWAHl06yDArPXC2QSnLqcY7GNY2OD6vICEqatR+o/2P4Gt9dcEkqYHb6hLGslPZFmn6wusLcKYxAGFs/RCRxlutJdvRgx/ck8Tv7ijJrcAwV1T3aX3CahFITAQqRXenEjxjVdiPHW8i2+92sVPb4owtimSxGVrOfsx1dby6LMzOPhaLOP/7RsjCaUJCdJitK48O9BWaaIQfX5ydxPbRwL83sFZTLUFmjUFlb14QH+GrknYX81B5ubhQGL5940qrZuUl+gIuJBhjU54zk2m+PZrXRx8tYtnTsUSr5OWOglkyTvfIr2aivsEpp481sHXXupIaLxzTYif2VTDjatDLG0pRzlN2aHOPAlLXJ4W2Lejji3LQnzsn6fw2sVUnTo79Qu7hYZhN4021xwMn7i7DztGA5yTYUrH7ohJjV+cAr59OsbB4138x4kuTl9O5YJkMbQFyIIuTeXeXMyVcBAzIIFCZn80F5n1gRc7+MqLHaxexLFrXYQPbIzwEytCDDVVMkZOmOa7MCmkoj69p4l7H5ssIFtfC21abCHQJ2l/5aIAqxcFuDQj5IkOSXSiDXz3jGL66de7eP1HicwDSHMEaKRGpC/QL6d4Flam7xy5W6etcqiew/gTukdp8mPPtbH/B225929bH+IDmyJcPxJiUZNoBi5NC6xaFGLlwgCnLieSZi//wvgAK7/PCDRHWS0mYzM9OnYuxVM/7OLQ8S5ePh9jtqtOegjVKcRWcbiZMWflkhnnxZcuihLJhyb6uhbkL1OcupTg8+cS/N33O9i8hGNsQ4SxjRG2LA1QZ0zWJ16/qLeIRZed+oeGW5+V0CLklCgD+8iXp/D8eBdTbaAWkraZDG00sfT+1lmfvxn7smGbp65eNdQi3qxFgjA0vHIuwdGzCT5/pI0dK0I8+qGWDKUFEFS+RFh4ociSQqql/cYVgU8+PYN/f72DwQaXZmaUmfhoreCnKFwVM5Wy89jbIxXL1Ga/u2DCLjVSSKumhPHMyS4+9fQs3riSSj8ltV8qCugtIAw48VSEagHDWxMpvvD9NhY1eZa1uVzZwMMtPLil6mwNu0LDPH6honkdGm097TPoMZXSvvDcrKwYEQ+FgyyHGG4zUgqHMsxB+gBiPiPal2T4rp2iqXpcdDRli6k4J/SluzaesB6TIIhmmR/YzFsO1jSezelZ1cBUYt5OL+36u82zq31/85R0C20OaO50MXjftTRDsy8rtE+JQ5umUjm/sO8I35sEx/FlToJRYFCnq5QoFQ8UKFzqdCSfKX+1tXB24DST7ZmzAluzdkpflpe1hq4HsNLyFo29ayFFghwfYIREZkhIcGJWeU66T8Iw6I2wvRJQjhIp26MQ69YGsvdWje+xU14PnZmPyjFW4W03IX1AD69dTCeLHX3HTIUeTKW8BEqosHnfjghdebABzMRCMmjSZKr3USGUnnW6wK/dXMP20UAWSOhswValu0Z2ZO74k0JVyKXb2sbcz3557/jBTa6RzPwyk4dk4JY1IQ7c18Kt60KpWSqPbxwKsHGYoSa9NLBpOMCGoUC/MEnnAAGeuL8fu6+LZNXHHLllinFxewX9voiR3TMZ4Yg+GKnMUtzkwJZICcsrQkmTlI398o11/M7tDXmfGKN6Au3vrqyACvQ3lI3LM0Fd74OO7fVACeeR78zizw/PyjhP1kBDbR68EakXLw6oYlIAhRnnP598bhcXJKNCIrTfv6NPlrRp71MElU7QGjPQAA7/kN7NFrh1Y00ep2VGR5UlbVoU0//pv7v4+DemZcJD+YYRQlESDtE+4XgYYsvpbHAuNOpZRN4S5SMyetfvcz/fwo+PqgRKlcV13UCXu6gynHYS7PmawpJf3xsgqAfy8ERuH12qJSGQNQ21uCySPHRgUqbYBtzMV9O9nvPKbeSau3Pf3QGUvk51gLuujXDzalOxVXk7FUFIQJQ0UYq7oM7w8L/N4MIdm3H+zs34xKFZmWnKcnldJ1apEhgJ8MJkihtXBth9bSSLMVno8nq5OYCaw09o4nvpTL7qdVMfYLI+iXCq2RORskIUqn1PW+EH4zHOTgLpVBdfGV6OJduHJTI88Mpy/OR33wZr1TDcBH5sWSBNnxpFCBIgzSlfIp3LWn3Pe4wJzVNXcJVe3wJArpelS8oUB1tcboe3r6YyQ3v2dIznx2NZoeFxgsVr+tH/wAp0L0zL7TFw2yp86rGLuHJyAtPgWL+Yy6LGztWRLHbS4ehgS9UissSpSkEVv1uwmTTbF2wOH+B9G86zgMkZrs4K3LO1hhtWhrIO+OqFRJovaY6Ib0QqzUxqHGEtgDC1vZCj207AO4n8lRJhBPmDCtD+Z7IAS7k+Hbc/+XJXFl1kGu5ahAWKMv9Y9dyEwRUjS+MkTcs/i/O9VFAWZiYA2U2/KEH1PEJxDR26spzCgAWR0r9sblN8VeFIAxStKXKCFFm6MdBXY6gHOutzI4BzbVuqjyfaemEYJmEQhmeTTmelvtf7lU2r2fm1XVUmhhc3ldfPmC44IUU9C6y5nMPVrOihj9/oxQhWN9B5DuY114UM0EjC5A3qyIkFQXiWh2FwSB40ULjOQPI8crIqT6zreKZSVD0wF1plwgOrNqizO1+CNG9a9TXxyjmnA5aDvN7X/5dMpWVWOmZ9vAuv6z2QfBe0zdms12XnhOn2w5KEBOOMpwODA3/Fj7388vfqtcaXOGecflJWYF4vOp8U/V1x8V5bj8TNS6cHERKPnHEeRbXHjx499j0OIfia9Rt+o16rn2FM/a4u21PMSUF7FjLm0cT/ojgselzLq6JV/igUiOr1+plt79v4sez1xUOHDl1cvW7D3fV6/QTnLNJjCKeKwluXNuJ5L80Hot7LXK4DdOZ3QjflWQn9xxiL6o36SeL1q189dLHix9NnP91pz+4TQkQiTXXMtKq45QX+b5rFZCHeO7HehNESOfK+xhmMg3Ee1+q1Ly1dNvrbR44cOW//eBq6ZT8p37p1683tqckH4yQeS9N0VTeO5YFYQQDz8TwFRFJkYN7CcxIu+54bPgt0Uck7CpMgCMfDIHhqwcDg3z5/9MXSz+f/B0gSoJJVzhaoAAAAAElFTkSuQmCC"


def enable_dpi_awareness() -> None:
    """Must run before the first Tk window, or text renders bitmap-scaled."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def is_sayintentions(cfg: dict) -> bool:
    return str(cfg.get("provider", "beyondatc")).lower() == "sayintentions"


def make_watcher(cfg: dict, status: Status, out: "queue.Queue[Trigger]"):
    """Build the clearance watcher for the configured provider.  Both emit
    Trigger objects onto `out`, so nothing downstream cares which one it is."""
    if is_sayintentions(cfg):
        return SayIntentionsWatcher(cfg, status, out)
    return LogWatcher(cfg, status, out)


def active_atc_process(cfg: dict) -> str:
    """Name of the process to suspend for the active provider."""
    if is_sayintentions(cfg):
        return str(cfg.get("sayintentions_process") or "SayIntentions.exe")
    return str(cfg.get("beyondatc_process") or "BeyondATC.exe")


def provider_row_label(cfg: dict) -> str:
    return "SAYINTENT." if is_sayintentions(cfg) else "BEYONDATC"


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.status = Status()
        self.events: "queue.Queue[Trigger]" = queue.Queue()
        self.plan_results: "queue.Queue[tuple]" = queue.Queue()
        self.provider = str(cfg.get("provider", "beyondatc")).lower()
        self.watcher = make_watcher(cfg, self.status, self.events)
        self.sim = SimLink(cfg, self.status)

        self.armed = bool(cfg["start_armed"])
        self.hold_until = 0.0       # monotonic; re-arm point during the grace hold
        self.rearm_mode = str(cfg.get("rearm_mode", "resume")).lower()
        self.await_resume = False   # "resume" mode: frozen, waiting for you to unpause
        self.saw_paused = False     # have we actually observed the pause take hold yet
        self.last = None            # (Trigger, fired: bool)

        # Flight-plan / waypoint state.
        self.waypoints: list[Waypoint] = []
        self.selected_wp: Waypoint | None = None
        self.wp_fired = False       # keep one crossing from pausing repeatedly
        self.loading_plan = False

        # Time acceleration.
        self.speed_target = 1.0
        self.speed_btns: dict[float, tk.Button] = {}

        # Freeze the active ATC add-on's process alongside the sim.  The controller
        # is always built (it does nothing until asked); pause_atc, bound to a
        # checkbox, decides whether pausing also suspends it.
        self.atc = AtcProcessController(active_atc_process(cfg))
        self.pause_atc = bool(cfg.get("pause_beyondatc", True))

        # A global hotkey is optional and deliberately separate from the Arm
        # state: it is an explicit manual pause, just like the Pause Sim button.
        raw_hotkey = str(cfg.get("pause_hotkey") or "").strip()
        self.hotkey_config_error = ""
        if raw_hotkey:
            try:
                self.pause_hotkey, _, _ = parse_hotkey(raw_hotkey)
            except ValueError as exc:
                self.pause_hotkey = ""
                self.hotkey_config_error = f"invalid saved hotkey: {exc}"
        else:
            self.pause_hotkey = ""
        self.hotkey = GlobalHotkey(self.pause_hotkey)

        # Optional Telegram push alerts.  The notifier no-ops until a token and
        # chat ID are set; _was_paused / _pause_notified drive the resume alert
        # off the paused->running edge so it only fires after a pause we announced.
        self.notifier = Notifier(str(cfg.get("telegram_bot_token") or ""),
                                 str(cfg.get("telegram_chat_id") or ""))
        self.telegram_results: "queue.Queue[tuple[bool, str]]" = queue.Queue()
        self.si_results: "queue.Queue[tuple[bool, str]]" = queue.Queue()
        self._was_paused = False
        self._pause_notified = False

        self.root = tk.Tk()
        dpi = self.root.winfo_fpixels("1i")
        self.scale = dpi / 96.0
        self.root.tk.call("tk", "scaling", dpi / 72.0)

        # The design is monospace throughout.  Prefer IBM Plex Mono if the user
        # has it installed; otherwise Consolas, which ships with Windows and
        # reads the same way.
        families = set(tkfont.families())
        mono = "IBM Plex Mono" if "IBM Plex Mono" in families else "Consolas"
        self.font_body = tkfont.Font(family=mono, size=9)
        self.font_small = tkfont.Font(family=mono, size=8)
        self.font_banner = tkfont.Font(family=mono, size=15, weight="bold")
        self.font_sub = tkfont.Font(family=mono, size=9)
        self.font_dot = tkfont.Font(family=mono, size=9)
        self.font_mono = tkfont.Font(family=mono, size=9)
        self.font_head = tkfont.Font(family=mono, size=10, weight="bold")

        self._build()

    def px(self, n: float) -> int:
        return max(1, int(round(n * self.scale)))

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        if self.cfg["always_on_top"]:
            root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._settings_open = False

        # Window / taskbar icon (kept on self so Tk does not garbage-collect it).
        try:
            self._icon = tk.PhotoImage(data=ICON_PNG_B64)
            root.iconphoto(True, self._icon)
        except Exception:
            pass

        # Dark title band, flush to the window edge under the OS chrome.
        header = tk.Frame(root, bg=HEADER_BG)
        header.pack(fill="x")
        hin = tk.Frame(header, bg=HEADER_BG, padx=self.px(13), pady=self.px(8))
        hin.pack(fill="x")
        tk.Frame(hin, bg=AMBER, width=self.px(8), height=self.px(8)).pack(side="left")
        tk.Label(hin, text="ATC PAUSER", bg=HEADER_BG, fg=HEADER_FG,
                 font=self.font_head).pack(side="left", padx=(self.px(9), 0))
        tk.Label(hin, text=f"v{APP_VERSION}", bg=HEADER_BG, fg=FAINT,
                 font=self.font_small).pack(side="right")
        # Gear opens the settings page; it becomes a close mark while there.
        self.btn_settings = tk.Label(hin, text="⚙", bg=HEADER_BG, fg=HEADER_FG,
                                     font=self.font_head, cursor="hand2")
        self.btn_settings.pack(side="right", padx=(0, self.px(12)))
        self.btn_settings.bind("<Button-1>",
                               lambda _e: self._show_page(not self._settings_open))

        outer = tk.Frame(root, bg=BG, padx=self.px(14), pady=self.px(14))
        outer.pack(fill="both", expand=True)

        # Pins the content width so the window does not jitter as text changes,
        # and so the main and settings pages line up at one width.
        tk.Frame(outer, bg=BG, height=1, width=self.px(352)).pack(fill="x")

        # Two pages share the outer area: the compact operating panel, and a
        # settings page reached by the header gear.  Only one is packed at a
        # time, so configuration never makes the main window taller.
        self.main_page = tk.Frame(outer, bg=BG)
        self.main_page.pack(fill="both", expand=True)
        self.settings_page = tk.Frame(outer, bg=BG)
        main = self.main_page

        # Status readout: two rows boxed by a hairline, split by a divider.
        box = tk.Frame(main, bg=LINE, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="x")
        self.row_sim = self._status_row(box, "MSFS 2024")
        tk.Frame(box, bg=LINE, height=1).pack(fill="x")
        # The second row tracks the active ATC provider; its label changes when
        # you switch provider in Settings.
        self.row_log = self._status_row(box, provider_row_label(self.cfg))
        self.row_log_name = self.row_log[2]

        self._build_plan_row(main)

        # Banner: a bold state word with a marker, over a line spelling out
        # exactly what Armed will do.  Boxed with an ink hairline.
        self.banner = tk.Frame(main, bg=ARMED_BG, highlightthickness=1,
                               highlightbackground=INK)
        self.banner.pack(fill="x", pady=(self.px(12), self.px(12)))
        self.banner_top = tk.Frame(self.banner, bg=ARMED_BG)
        top = self.banner_top
        top.pack(fill="x", padx=self.px(13), pady=(self.px(10), 0))
        self.banner_main = tk.Label(top, text="ARMED", bg=ARMED_BG, fg="#ffffff",
                                    font=self.font_banner)
        self.banner_main.pack(side="left")
        self.banner_mark = tk.Frame(top, bg="#ffffff", width=self.px(9), height=self.px(9))
        self.banner_mark.pack(side="right", pady=self.px(4))
        self.banner_sub = tk.Label(self.banner, text="", bg=ARMED_BG, fg=BANNER_SUB_FG,
                                   font=self.font_sub, anchor="w")
        self.banner_sub.pack(fill="x", padx=self.px(13), pady=(self.px(3), self.px(11)))

        head = tk.Frame(main, bg=BG)
        head.pack(fill="x", pady=(0, self.px(4)))
        self.trigger_label = tk.Label(head, text="LAST TRIGGER", bg=BG, fg=MUTED,
                                      font=self.font_small)
        self.trigger_label.pack(side="left")
        self.trigger_time = tk.Label(head, text="--:--:--", bg=BG, fg=FAINT,
                                     font=self.font_small)
        self.trigger_time.pack(side="right")

        self.quote = tk.Label(main, text="nothing yet", bg=CARD_BG, fg="#35322c",
                              font=self.font_mono, justify="left", anchor="w",
                              wraplength=self.px(326), padx=self.px(10), pady=self.px(8),
                              highlightthickness=1, highlightbackground=LINE)
        self.quote.pack(fill="x")

        self._build_speed_row(main)

        buttons = tk.Frame(main, bg=BG)
        buttons.pack(fill="x", pady=(self.px(10), 0))
        # One button that mirrors the sim: it only ever says "Resume sim" when
        # the sim is actually paused, so it can never imply a pause that is not
        # there.  It doubles as the old Test button when the sim is running.
        self.btn_pause = self._button(buttons, "PAUSE SIM", self.on_pause_toggle,
                                      last=False, primary=True)
        self.btn_arm = self._button(buttons, "DISARM", self.on_toggle_arm, last=True)

        self._build_settings_page()
        self._refresh()

    def _status_row(self, parent, name: str):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=BG, padx=self.px(11), pady=self.px(8))
        inner.pack(fill="x")
        mark = tk.Frame(inner, bg=GREY, width=self.px(7), height=self.px(7))
        mark.pack(side="left", pady=self.px(3))
        name_lbl = tk.Label(inner, text=name, bg=BG, fg=MUTED, font=self.font_body,
                            width=11, anchor="w")
        name_lbl.pack(side="left", padx=(self.px(9), 0))
        value = tk.Label(inner, text="", bg=BG, fg=INK, font=self.font_body, anchor="w")
        value.pack(side="left", fill="x", expand=True)
        return mark, value, name_lbl

    def _build_plan_row(self, parent) -> None:
        # SimBrief identity: type your Pilot ID or username here and press Load.
        # It is saved back to config.json, so this is a one-time entry.
        idrow = tk.Frame(parent, bg=BG)
        idrow.pack(fill="x", pady=(self.px(10), 0))
        tk.Label(idrow, text="SIMBRIEF", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        self.id_var = tk.StringVar(value=str(self.cfg.get("simbrief_id") or ""))
        self.id_entry = tk.Entry(idrow, textvariable=self.id_var, font=self.font_body,
                                 bg=CARD_BG, fg=INK, relief="solid", borderwidth=1,
                                 highlightthickness=0, insertbackground=INK,
                                 disabledbackground="#e8e6e1")
        self.id_entry.pack(side="left", fill="x", expand=True,
                           padx=(self.px(6), self.px(6)), ipady=self.px(3))
        self.id_entry.bind("<Return>", lambda _e: self.on_load_plan())
        self.btn_load = tk.Button(idrow, text="LOAD", command=self.on_load_plan,
                                  font=self.font_body, bg=BTN_PRIMARY_BG, fg=BTN_PRIMARY_FG,
                                  activebackground="#3a3630", activeforeground=HEADER_FG,
                                  relief="flat", borderwidth=0, highlightthickness=0,
                                  padx=self.px(12), pady=self.px(4))
        self.btn_load.pack(side="left")

        # Waypoint selector, populated once a plan is loaded.
        wprow = tk.Frame(parent, bg=BG)
        wprow.pack(fill="x", pady=(self.px(7), 0))
        tk.Label(wprow, text="PAUSE AT", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        self.wp_var = tk.StringVar(value=NO_WP_LABEL)
        self.wp_menu = tk.OptionMenu(wprow, self.wp_var, NO_WP_LABEL)
        self.wp_menu.configure(font=self.font_body, bg=CARD_BG, fg=INK,
                               activebackground="#efece6", relief="solid",
                               borderwidth=1, highlightthickness=0, anchor="w",
                               pady=self.px(3), indicatoron=True)
        self.wp_menu["menu"].configure(font=self.font_body, bg=CARD_BG, fg=INK)
        self.wp_menu.pack(side="left", fill="x", expand=True, padx=(self.px(6), 0))
        self._populate_wp_menu([])      # start with just the no-waypoint entry

        hint = ("press LOAD to fetch your latest plan"
                if str(self.cfg.get("simbrief_id") or "").strip()
                else "enter your SimBrief Pilot ID or username, then LOAD")
        self.plan_status = tk.Label(parent, text=hint, bg=BG, fg=FAINT,
                                    font=self.font_small, anchor="w")
        self.plan_status.pack(fill="x", pady=(self.px(3), 0))

    def _build_speed_row(self, parent) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(self.px(11), 0))
        tk.Label(row, text="SPEED", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        # Joined segmented control: one ink box, notches divided by hairlines.
        seg = tk.Frame(row, bg=INK, highlightthickness=1, highlightbackground=INK)
        seg.pack(side="left", fill="x", expand=True, padx=(self.px(6), 0))
        for i, rate in enumerate(SPEEDS):
            btn = tk.Button(seg, text=f"{int(rate)}×", font=self.font_body,
                            bg=CARD_BG, fg=MUTED, activebackground="#efece6",
                            relief="flat", borderwidth=0, highlightthickness=0,
                            pady=self.px(4), command=lambda r=rate: self.on_speed(r))
            btn.pack(side="left", fill="x", expand=True,
                     padx=(0, 1 if i < len(SPEEDS) - 1 else 0))
            self.speed_btns[rate] = btn

    def _build_settings_page(self) -> None:
        """The settings page keeps one-time setup off the operating panel."""
        page = self.settings_page

        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x")
        back = tk.Label(bar, text="‹ BACK", bg=BG, fg=MUTED, font=self.font_small,
                        cursor="hand2")
        back.pack(side="left")
        back.bind("<Button-1>", lambda _e: self._show_page(False))
        tk.Label(bar, text="SETTINGS", bg=BG, fg=FAINT,
                 font=self.font_small).pack(side="right")
        tk.Frame(page, bg=LINE, height=1).pack(fill="x", pady=(self.px(9), self.px(11)))

        # ATC provider: which add-on's clearances arm the pause.  The SayIntentions
        # API-key field lives inside pbox so it can be shown/hidden in place.
        pbox = tk.Frame(page, bg=BG)
        pbox.pack(fill="x")
        tk.Label(pbox, text="ATC PROVIDER", bg=BG, fg=MUTED, font=self.font_small,
                 anchor="w").pack(fill="x")
        seg = tk.Frame(pbox, bg=INK, highlightthickness=1, highlightbackground=INK)
        seg.pack(fill="x", pady=(self.px(7), 0))
        self.provider_btns: dict[str, tk.Button] = {}
        _opts = [("beyondatc", "BeyondATC"), ("sayintentions", "SayIntentions")]
        for i, (key, label) in enumerate(_opts):
            btn = tk.Button(seg, text=label, font=self.font_small, bg=CARD_BG, fg=MUTED,
                            activebackground="#efece6", relief="flat", borderwidth=0,
                            highlightthickness=0, pady=self.px(5),
                            command=lambda k=key: self.on_provider_change(k))
            btn.pack(side="left", fill="x", expand=True,
                     padx=(0, 1 if i < len(_opts) - 1 else 0))
            self.provider_btns[key] = btn

        self.si_key_frame = tk.Frame(pbox, bg=BG)
        krow = tk.Frame(self.si_key_frame, bg=BG)
        krow.pack(fill="x", pady=(self.px(8), 0))
        tk.Label(krow, text="API KEY", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        self.si_key_var = tk.StringVar(value=str(self.cfg.get("sayintentions_api_key") or ""))
        e_key = tk.Entry(krow, textvariable=self.si_key_var, font=self.font_body,
                         bg=CARD_BG, fg=INK, relief="solid", borderwidth=1,
                         highlightthickness=0, insertbackground=INK)
        e_key.pack(side="left", fill="x", expand=True, padx=(self.px(6), 0), ipady=self.px(3))
        e_key.bind("<FocusOut>", lambda _e: self._save_si_key())
        e_key.bind("<Return>", lambda _e: self._save_si_key())
        srow = tk.Frame(self.si_key_frame, bg=BG)
        srow.pack(fill="x", pady=(self.px(7), 0))
        self.si_status = tk.Label(srow, text="", bg=BG, fg=FAINT, font=self.font_small,
                                  anchor="w", wraplength=self.px(200), justify="left")
        self.si_status.pack(side="left", fill="x", expand=True)
        self.btn_si_test = tk.Button(srow, text="TEST", command=self.on_test_si,
                                     font=self.font_small, bg=BTN_BG, fg=INK,
                                     activebackground="#e6e3dd", relief="solid", borderwidth=1,
                                     highlightthickness=0, padx=self.px(10), pady=self.px(3))
        self.btn_si_test.pack(side="right")
        tk.Label(self.si_key_frame,
                 text="from the SayIntentions pilot portal (needs a subscription)",
                 bg=BG, fg=FAINT, font=self.font_small, anchor="w").pack(
                     fill="x", pady=(self.px(5), 0))

        tk.Frame(page, bg=LINE, height=1).pack(fill="x", pady=(self.px(13), self.px(10)))

        # Freeze the active ATC add-on's process alongside the sim (was on the
        # main panel; it is a setting).
        self.freeze_var = tk.BooleanVar(value=self.pause_atc)
        tk.Checkbutton(page, text="Also freeze the ATC app when paused",
                       variable=self.freeze_var, command=self.on_toggle_freeze,
                       font=self.font_small, bg=BG, fg=MUTED, activebackground=BG,
                       activeforeground=INK, selectcolor=CARD_BG, anchor="w",
                       bd=0, highlightthickness=0, padx=0, pady=0).pack(fill="x")
        self._refresh_provider_ui()

        # RegisterHotKey is system-wide, so this remains useful with MSFS in
        # front.  The entry captures a real key press instead of asking users to
        # spell a platform-specific shortcut syntax.
        tk.Frame(page, bg=LINE, height=1).pack(fill="x", pady=(self.px(13), self.px(10)))
        tk.Label(page, text="GLOBAL PAUSE HOTKEY", bg=BG, fg=MUTED, font=self.font_small,
                 anchor="w").pack(fill="x")
        tk.Label(page, text="click the field, then press a key combination",
                 bg=BG, fg=FAINT, font=self.font_small, anchor="w").pack(
                     fill="x", pady=(self.px(2), 0))
        hrow = tk.Frame(page, bg=BG)
        hrow.pack(fill="x", pady=(self.px(8), 0))
        tk.Label(hrow, text="PAUSE KEY", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        self.hotkey_var = tk.StringVar(value=self.pause_hotkey)
        self.hotkey_entry = tk.Entry(hrow, textvariable=self.hotkey_var, font=self.font_body,
                                     bg=CARD_BG, fg=INK, relief="solid", borderwidth=1,
                                     highlightthickness=0, insertbackground=INK)
        self.hotkey_entry.pack(side="left", fill="x", expand=True,
                               padx=(self.px(6), self.px(6)), ipady=self.px(3))
        self.hotkey_entry.bind("<FocusIn>", self._on_hotkey_focus)
        self.hotkey_entry.bind("<FocusOut>", lambda _e: self._save_hotkey())
        self.hotkey_entry.bind("<KeyPress>", self._capture_hotkey)
        tk.Button(hrow, text="CLEAR", command=self.on_clear_hotkey,
                  font=self.font_small, bg=BTN_BG, fg=INK, activebackground="#e6e3dd",
                  relief="solid", borderwidth=1, highlightthickness=0,
                  padx=self.px(10), pady=self.px(3)).pack(side="right")
        initial_hotkey_status = (self.hotkey_config_error or
                                 ("global pause hotkey is off" if not self.pause_hotkey
                                  else "starting global hotkey…"))
        self.hotkey_status = tk.Label(page, text=initial_hotkey_status, bg=BG, fg=FAINT,
                                      font=self.font_small, anchor="w")
        self.hotkey_status.pack(fill="x", pady=(self.px(4), 0))

        # Telegram alerts.  Both fields save on focus-out, so they are typed once
        # and remembered like the SimBrief ID.
        tk.Frame(page, bg=LINE, height=1).pack(fill="x", pady=(self.px(13), self.px(10)))
        tk.Label(page, text="TELEGRAM ALERTS", bg=BG, fg=MUTED, font=self.font_small,
                 anchor="w").pack(fill="x")
        tk.Label(page, text="a buzz on your phone the moment the sim pauses",
                 bg=BG, fg=FAINT, font=self.font_small, anchor="w").pack(
                     fill="x", pady=(self.px(2), 0))

        trow = tk.Frame(page, bg=BG)
        trow.pack(fill="x", pady=(self.px(9), 0))
        tk.Label(trow, text="BOT TOKEN", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        self.tg_token_var = tk.StringVar(value=str(self.cfg.get("telegram_bot_token") or ""))
        e_token = tk.Entry(trow, textvariable=self.tg_token_var, font=self.font_body,
                           bg=CARD_BG, fg=INK, relief="solid", borderwidth=1,
                           highlightthickness=0, insertbackground=INK)
        e_token.pack(side="left", fill="x", expand=True, padx=(self.px(6), 0), ipady=self.px(3))
        e_token.bind("<FocusOut>", lambda _e: self._save_telegram())
        e_token.bind("<Return>", lambda _e: self._save_telegram())

        crow = tk.Frame(page, bg=BG)
        crow.pack(fill="x", pady=(self.px(7), 0))
        tk.Label(crow, text="CHAT ID", bg=BG, fg=FAINT, font=self.font_small,
                 width=9, anchor="w").pack(side="left")
        self.tg_chat_var = tk.StringVar(value=str(self.cfg.get("telegram_chat_id") or ""))
        e_chat = tk.Entry(crow, textvariable=self.tg_chat_var, font=self.font_body,
                          bg=CARD_BG, fg=INK, relief="solid", borderwidth=1,
                          highlightthickness=0, insertbackground=INK)
        e_chat.pack(side="left", fill="x", expand=True, padx=(self.px(6), 0), ipady=self.px(3))
        e_chat.bind("<FocusOut>", lambda _e: self._save_telegram())
        e_chat.bind("<Return>", lambda _e: self._save_telegram())

        brow = tk.Frame(page, bg=BG)
        brow.pack(fill="x", pady=(self.px(9), 0))
        self.tg_status = tk.Label(brow, text="", bg=BG, fg=FAINT, font=self.font_small,
                                  anchor="w", wraplength=self.px(210), justify="left")
        self.tg_status.pack(side="left", fill="x", expand=True)
        self.btn_test = tk.Button(brow, text="SEND TEST", command=self.on_test_telegram,
                                  font=self.font_small, bg=BTN_BG, fg=INK,
                                  activebackground="#e6e3dd", relief="solid", borderwidth=1,
                                  highlightthickness=0, padx=self.px(10), pady=self.px(3))
        self.btn_test.pack(side="right")

        tk.Label(page, text="token from @BotFather · chat ID from @userinfobot",
                 bg=BG, fg=FAINT, font=self.font_small, anchor="w").pack(
                     fill="x", pady=(self.px(7), 0))

    def _show_page(self, settings: bool) -> None:
        """Swap the outer area between the operating panel and settings."""
        self._settings_open = settings
        if settings:
            self.main_page.pack_forget()
            self.settings_page.pack(fill="both", expand=True)
            self.btn_settings.configure(text="×")
        else:
            self._save_telegram()       # persist any un-blurred field edits
            self.settings_page.pack_forget()
            self.main_page.pack(fill="both", expand=True)
            self.btn_settings.configure(text="⚙")

    def _button(self, parent, text: str, command, last: bool,
                primary: bool = False) -> tk.Button:
        if primary:
            bg, fg, active = BTN_PRIMARY_BG, BTN_PRIMARY_FG, "#3a3630"
            btn = tk.Button(parent, text=text, command=command, font=self.font_body,
                            bg=bg, fg=fg, activebackground=active, activeforeground=fg,
                            relief="flat", borderwidth=0, highlightthickness=0,
                            pady=self.px(5))
        else:
            btn = tk.Button(parent, text=text, command=command, font=self.font_body,
                            bg=BTN_BG, fg=INK, activebackground="#e6e3dd",
                            relief="solid", borderwidth=1, highlightthickness=0,
                            pady=self.px(5))
        btn.pack(side="left", fill="x", expand=True,
                 padx=(0, 0 if last else self.px(8)))
        return btn

    def _populate_wp_menu(self, waypoints: list[Waypoint]) -> None:
        menu = self.wp_menu["menu"]
        menu.delete(0, "end")
        menu.add_command(label=NO_WP_LABEL, command=lambda: self._select_wp(None))
        for wp in waypoints:
            tag = wp.stage or wp.kind.upper()
            label = f"{wp.ident}   · {tag}" if tag else wp.ident
            menu.add_command(label=label,
                             command=lambda w=wp, l=label: self._select_wp(w, l))

    # -- behaviour ----------------------------------------------------------

    def run(self) -> None:
        self.watcher.start()
        self.sim.start()
        self.hotkey.start()
        self.root.after(200, self._tick)
        self.root.mainloop()

    def quit(self) -> None:
        self._save_telegram()           # persist any un-blurred field edits
        self.watcher.stop()
        self.sim.stop()
        self.hotkey.stop()
        self.atc.resume()              # never leave BeyondATC frozen on exit
        self.root.destroy()

    def _pause(self) -> None:
        self.sim.pause()
        if self.pause_atc:
            self.atc.suspend()

    def _resume(self) -> None:
        self.sim.resume()
        self.atc.resume()              # harmless if it was never suspended

    def on_toggle_freeze(self) -> None:
        self.pause_atc = self.freeze_var.get()
        save_config(pause_beyondatc=self.pause_atc)
        if self.pause_atc and self.status.sim_paused:
            self.atc.suspend()         # enabled mid-pause: freeze it now
        elif not self.pause_atc:
            self.atc.resume()          # disabled: thaw it right away

    def _on_hotkey_focus(self, _event) -> None:
        self.hotkey_status.configure(text="press a shortcut, or Esc to keep the current one")

    def _capture_hotkey(self, event) -> str:
        # Modifier-down events are incomplete combinations; wait for the key.
        if event.keysym in {"Control_L", "Control_R", "Alt_L", "Alt_R",
                            "Shift_L", "Shift_R", "Win_L", "Win_R"}:
            return "break"
        if event.keysym == "Escape":
            self.hotkey_entry.selection_clear()
            self.root.focus_set()
            self.hotkey_status.configure(
                text="global pause hotkey is off" if not self.pause_hotkey
                else f"global: {self.pause_hotkey}")
            return "break"
        names: list[str] = []
        if event.state & 0x0004:
            names.append("CTRL")
        # On Windows Tk uses 0x0008 for Num Lock (not Alt); Alt is bit 17.
        # Checking Mod1 here made every shortcut look like Alt on keyboards
        # with Num Lock enabled.
        if event.state & 0x20000:
            names.append("ALT")
        if event.state & 0x0001:
            names.append("SHIFT")
        key = event.keysym.upper()
        # Tk spells these a little differently than the Windows key names.
        key = {"RETURN": "ENTER", "ESCAPE": "ESC", "PRIOR": "PAGEUP",
               "NEXT": "PAGEDOWN", "SPACE": "SPACE"}.get(key, key)
        try:
            display, _, _ = parse_hotkey("+".join([*names, key]))
        except ValueError as exc:
            self.hotkey_status.configure(text=str(exc))
            return "break"
        self.hotkey_var.set(display)
        self._save_hotkey()
        self.root.focus_set()
        return "break"

    def _save_hotkey(self) -> None:
        value = self.hotkey_var.get().strip()
        if not value:
            display = ""
        else:
            try:
                display, _, _ = parse_hotkey(value)
            except ValueError as exc:
                self.hotkey_status.configure(text=str(exc))
                return
        if display == self.pause_hotkey:
            return
        self.pause_hotkey = display
        self.hotkey_var.set(display)
        self.cfg["pause_hotkey"] = display
        save_config(pause_hotkey=display)
        self.hotkey_status.configure(
            text="global pause hotkey is off" if not display else "registering global hotkey…")
        self.hotkey.configure(display)

    def on_clear_hotkey(self) -> None:
        self.hotkey_var.set("")
        self._save_hotkey()

    # -- ATC provider -------------------------------------------------------

    def on_provider_change(self, provider: str) -> None:
        provider = provider.lower()
        if provider == self.provider:
            return
        self.provider = provider
        self.cfg["provider"] = provider
        save_config(provider=provider)

        # Swap the clearance watcher (threads cannot restart, so build a fresh one).
        self.watcher.stop()
        self.watcher = make_watcher(self.cfg, self.status, self.events)
        self.watcher.start()

        # Re-point the freeze controller at the new provider's process.
        was_suspended = self.atc.suspended
        self.atc.resume()
        self.atc = AtcProcessController(active_atc_process(self.cfg))
        if was_suspended and self.pause_atc and self.status.sim_paused:
            self.atc.suspend()

        self.status.log_ok = False
        self.status.log_detail = "switching…"
        self._refresh_provider_ui()

    def _refresh_provider_ui(self) -> None:
        for key, btn in self.provider_btns.items():
            on = (key == self.provider)
            btn.configure(bg=SPEED_ON_BG if on else CARD_BG,
                          fg=SPEED_ON_FG if on else MUTED,
                          activebackground=SPEED_ON_BG if on else "#efece6")
        if self.provider == "sayintentions":
            self.si_key_frame.pack(fill="x")
        else:
            self.si_key_frame.pack_forget()
        self.row_log_name.configure(text=provider_row_label(self.cfg))

    def _save_si_key(self) -> None:
        key = self.si_key_var.get().strip()
        if key == str(self.cfg.get("sayintentions_api_key") or ""):
            return
        self.cfg["sayintentions_api_key"] = key
        save_config(sayintentions_api_key=key)
        if isinstance(self.watcher, SayIntentionsWatcher):
            self.watcher.update_key(key)

    def on_test_si(self) -> None:
        self._save_si_key()
        key = self.si_key_var.get().strip()
        if not key:
            self.si_status.configure(text="enter your API key first")
            return
        self.btn_si_test.configure(text="…", state="disabled")
        self.si_status.configure(text="checking…")
        threading.Thread(target=self._si_test_worker, args=(key,),
                         name="SiTest", daemon=True).start()

    def _si_test_worker(self, key: str) -> None:
        ok, msg = si_check_key(key)
        self.si_results.put((ok, msg))

    def _save_telegram(self) -> None:
        token = self.tg_token_var.get().strip()
        chat = self.tg_chat_var.get().strip()
        if token == self.notifier.token and chat == self.notifier.chat_id:
            return                      # nothing changed, skip the disk write
        self.notifier.update(token, chat)
        self.cfg["telegram_bot_token"] = token
        self.cfg["telegram_chat_id"] = chat
        save_config(telegram_bot_token=token, telegram_chat_id=chat)

    def on_test_telegram(self) -> None:
        self._save_telegram()
        if not self.notifier.configured:
            self.tg_status.configure(text="enter a bot token and chat ID first")
            return
        self.btn_test.configure(text="…", state="disabled")
        self.tg_status.configure(text="sending test message…")
        threading.Thread(target=self._test_worker, name="TgTest", daemon=True).start()

    def _test_worker(self) -> None:
        ok, msg = self.notifier.send_sync(
            f"{APP_NAME}: test alert - notifications are working.")
        self.telegram_results.put((ok, msg))

    def _check_notify(self) -> None:
        """Send a resume alert on the paused->running edge, but only when we
        announced the pause that preceded it, so it can never fire on its own."""
        paused = self.status.sim_paused
        was, self._was_paused = self._was_paused, paused
        if was and not paused and self._pause_notified:
            self._pause_notified = False
            if self.cfg.get("notify_on_resume", True):
                self.notifier.notify(f"▶ {APP_NAME}: MSFS 2024 resumed")

    def _pause_message(self, trigger: Trigger) -> str:
        when = time.strftime("%H:%MZ", time.gmtime())
        detail = (trigger.text or "").strip()
        head = f"⏸ {APP_NAME}: paused MSFS 2024"
        mid = trigger.label + (f" · {detail}" if detail else "")
        return f"{head}\n{mid}\n{when}"

    def _tick(self) -> None:
        while True:
            try:
                kind, detail = self.hotkey.events.get_nowait()
            except queue.Empty:
                break
            if kind == "pressed":
                # Keep the global shortcut's semantics identical to the visible
                # Pause Sim / Resume Sim control.
                self.on_pause_toggle()
            else:
                self.hotkey_status.configure(text=detail)
        while True:
            try:
                trigger = self.events.get_nowait()
            except queue.Empty:
                break
            self._on_trigger(trigger)
        while True:
            try:
                result = self.plan_results.get_nowait()
            except queue.Empty:
                break
            self._on_plan_result(*result)
        while True:
            try:
                ok, msg = self.telegram_results.get_nowait()
            except queue.Empty:
                break
            self.btn_test.configure(text="SEND TEST", state="normal")
            self.tg_status.configure(
                text="test sent - check Telegram" if ok else f"failed: {msg}")
        while True:
            try:
                ok, msg = self.si_results.get_nowait()
            except queue.Empty:
                break
            self.btn_si_test.configure(text="TEST", state="normal")
            self.si_status.configure(text=msg)
        self._check_rearm()
        self._check_notify()
        self._eval_waypoint()
        self._refresh()
        self.root.after(200, self._tick)

    def _ready(self, now: float) -> bool:
        """True when a trigger is allowed to fire - armed, not still frozen on a
        previous one, and past any grace hold."""
        return self.armed and not self.await_resume and now >= self.hold_until

    def _fire(self, trigger: Trigger) -> None:
        """Pause the sim and apply the configured re-arm policy."""
        self.last = (trigger, True)
        # Drop back to real time first, so you never resume into fast-forward on
        # the descent.  Ordered before the pause so it lands while time still runs.
        if self.speed_target != 1.0:
            self.speed_target = 1.0
            self.sim.set_rate(1.0)
        self._pause()
        if self.cfg.get("notify_on_pause", True):
            self.notifier.notify(self._pause_message(trigger))
            self._pause_notified = self.notifier.configured
        now = time.monotonic()
        if self.rearm_mode == "manual":
            self.armed = False
        elif self.rearm_mode == "resume" and self.status.sim_connected:
            # Stay frozen until you unpause; only safe while connected, otherwise
            # we could never see the pause lift and would hang.
            self.await_resume = True
            self.saw_paused = False
            self.hold_until = 0.0
        else:                                   # "timer", or resume with no sim
            self.hold_until = now + self.cfg["rearm_seconds"]

    def _check_rearm(self) -> None:
        """In resume mode, hold frozen until you unpause, then arm again right
        away - no countdown."""
        if not self.await_resume:
            return
        if self.status.sim_paused:
            self.saw_paused = True              # the pause has taken hold
        elif self.saw_paused:                   # ...and now you have resumed
            self.await_resume = False
            self.saw_paused = False
            self.hold_until = 0.0

    def _on_trigger(self, trigger: Trigger) -> None:
        if self._ready(time.monotonic()):
            self._fire(trigger)
        else:
            self.last = (trigger, False)

    def _eval_waypoint(self) -> None:
        """Pause once when we come within the arm radius of the chosen waypoint.

        This runs alongside the STAR / descent trigger - whichever lands first
        wins - so selecting a waypoint keeps the ATC clearance as a backstop."""
        wp = self.selected_wp
        if wp is None or self.wp_fired or not self._ready(time.monotonic()):
            return
        lat, lon = self.status.plane_lat, self.status.plane_lon
        if lat is None or lon is None:
            return
        dist = nm_between(lat, lon, wp.lat, wp.lon)
        if dist <= self.cfg["waypoint_arm_nm"]:
            self.wp_fired = True
            self._fire(Trigger(f"WP {wp.ident}",
                               f"{dist:.1f} nm from {wp.ident}", time.time()))

    def on_pause_toggle(self) -> None:
        if self.status.sim_paused:
            self._resume()
        else:
            self._pause()

    def on_speed(self, rate: float) -> None:
        self.speed_target = rate
        self.sim.set_rate(rate)

    def on_toggle_arm(self) -> None:
        self.armed = not self.armed
        if self.armed:
            self.hold_until = 0.0
            self.await_resume = False
            self.saw_paused = False
            self.wp_fired = False

    def on_load_plan(self) -> None:
        if self.loading_plan:
            return
        ident = self.id_var.get().strip()
        if not ident:
            self.plan_status.configure(text="enter your SimBrief Pilot ID or username")
            return
        # Remember it so it is typed once, ever - both in this session and on disk.
        if ident != str(self.cfg.get("simbrief_id") or ""):
            self.cfg["simbrief_id"] = ident
            save_config(simbrief_id=ident)
        self.loading_plan = True
        self.btn_load.configure(text="…", state="disabled")
        self.plan_status.configure(text="loading plan from SimBrief…")
        threading.Thread(target=self._load_plan_worker, args=(ident,),
                         name="SimBrief", daemon=True).start()

    def _load_plan_worker(self, ident: str) -> None:
        try:
            plan = fetch_simbrief_plan(ident)
            self.plan_results.put(("ok", plan))
        except Exception as exc:      # surface any failure as a status message
            self.plan_results.put(("err", str(exc)))

    def _on_plan_result(self, kind: str, payload) -> None:
        self.loading_plan = False
        self.btn_load.configure(text="LOAD", state="normal")
        if kind == "err":
            self.plan_status.configure(text=f"SimBrief: {payload}")
            return
        plan: Plan = payload
        self.waypoints = plan.waypoints
        self._populate_wp_menu(plan.waypoints)
        self._select_wp(None)         # a fresh plan clears the old selection
        self.plan_status.configure(text=plan.summary)

    def _select_wp(self, wp: Waypoint | None, label: str | None = None) -> None:
        self.selected_wp = wp
        self.wp_fired = False
        self.wp_var.set(label or NO_WP_LABEL)

    def _refresh(self) -> None:
        status = self.status
        self.row_sim[0].configure(bg=GREEN if status.sim_connected else GREY)
        self.row_sim[1].configure(text=status.sim_detail.upper())
        self.row_log[0].configure(bg=GREEN if status.log_ok else GREY)
        self.row_log[1].configure(text=status.log_detail.upper())

        self.btn_pause.configure(text="RESUME SIM" if status.sim_paused else "PAUSE SIM")
        self._refresh_speed()

        now = time.monotonic()
        if not self.armed:
            self._set_banner(DISARMED_BG, "DISARMED", "no pauses will fire")
            self.btn_arm.configure(text="ARM")
        elif self.await_resume:
            self._set_banner(TRIGGERED_BG, "PAUSED", "resume when you are ready")
            self.btn_arm.configure(text="DISARM")
        elif now < self.hold_until:
            seconds = int(self.hold_until - now) + 1
            self._set_banner(TRIGGERED_BG, "TRIGGERED", f"re-arms in {seconds}s")
            self.btn_arm.configure(text="DISARM")
        else:
            if self.selected_wp is not None:
                nm = int(round(self.cfg["waypoint_arm_nm"]))
                sub = f"will pause {nm} nm before {self.selected_wp.ident}"
            else:
                sub = "will pause on a STAR or descent clearance"
            self._set_banner(ARMED_BG, "ARMED", sub)
            self.btn_arm.configure(text="DISARM")

        if self.last is not None:
            trigger, fired = self.last
            tag = trigger.label if fired else f"{trigger.label} (NOT ARMED)"
            self.trigger_label.configure(text=f"LAST TRIGGER · {tag}")
            self.trigger_time.configure(
                text=time.strftime("%H:%M:%S", time.localtime(trigger.at)))
            self.quote.configure(text=trigger.text or "(no text logged)")

    def _refresh_speed(self) -> None:
        # Highlight the notch the sim is actually running at when connected;
        # otherwise show what is queued.  Rates off the 1/2/4 scale light nothing.
        rate = self.status.sim_rate if self.status.sim_connected else self.speed_target
        active = min(SPEEDS, key=lambda r: abs(r - rate))
        if abs(active - rate) > 0.25:
            active = None
        for value, btn in self.speed_btns.items():
            if value == active:
                btn.configure(bg=SPEED_ON_BG, fg=SPEED_ON_FG, activebackground=SPEED_ON_BG)
            else:
                btn.configure(bg=CARD_BG, fg=MUTED, activebackground="#efece6")

    def _set_banner(self, bg: str, main: str, sub: str) -> None:
        self.banner.configure(bg=bg)
        self.banner_top.configure(bg=bg)
        self.banner_main.configure(bg=bg, text=main)
        self.banner_sub.configure(bg=bg, text=f"> {sub.upper()}")


def main() -> int:
    enable_dpi_awareness()
    App(load_config()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
