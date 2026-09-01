#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 jshef747
# BATC Pauser is free software under the GNU AGPL-3.0; see LICENSE and NOTICE.
"""BATC Pauser - pause Microsoft Flight Simulator 2024 at a waypoint of your
choosing, or when BeyondATC clears you for the arrival or issues a descent.

Two ways to arm:

  * Pick a waypoint from your SimBrief flight plan and the sim pauses a few
    miles before you reach it.
  * Pick nothing and it falls back to BeyondATC: it clears you for the STAR or
    issues a descent, and the sim pauses the moment that clearance shows up.

BeyondATC has no public API, but it is a Unity app that writes a running
transcript of every ATC transmission to Player.log.  Calls addressed to the
player land as a [ControllerScript] + [Instruction] pair; when datalink is in
use they arrive instead as a [CPDLC] uplink.  This watches for both and sends
SimConnect PAUSE_ON the moment an arrival or descent clearance shows up.  The
waypoint arm reads the aircraft's own position over SimConnect instead.

Sections below, in order:
    1. Configuration
    2. LogWatcher      - tails Player.log and emits Trigger events
    3. SimBrief        - fetches a flight plan and its waypoints
    4. SimLink         - keeps a SimConnect connection alive, pauses, reads position
    5. BatcController  - suspends/resumes the BeyondATC process
    6. UI              - a small always-on-top tkinter window
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
import urllib.parse
import urllib.request
from ctypes import wintypes
from ctypes.wintypes import DWORD
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont

APP_NAME = "BATC Pauser"
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

    # Freeze BeyondATC too while paused, by suspending its process - it stops
    # talking and holds its state, then picks up where it left off on resume.
    "pause_beyondatc": True,
    "beyondatc_process": "BeyondATC.exe",

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


def save_simbrief_id(value: str) -> None:
    """Persist just the SimBrief id back to config.json, leaving every other
    key the user has set untouched."""
    data: dict = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            pass
    data["simbrief_id"] = value
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"{APP_NAME}: could not save simbrief_id ({exc})", file=sys.stderr)


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
        self.log_detail = "waiting for BeyondATC"
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


class BatcController:
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
# 6. UI
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


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.status = Status()
        self.events: "queue.Queue[Trigger]" = queue.Queue()
        self.plan_results: "queue.Queue[tuple]" = queue.Queue()
        self.watcher = LogWatcher(cfg, self.status, self.events)
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

        # Optional: freeze BeyondATC's process alongside the sim.
        self.batc = (BatcController(str(cfg.get("beyondatc_process") or "BeyondATC.exe"))
                     if cfg.get("pause_beyondatc") else None)

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

        # Dark title band, flush to the window edge under the OS chrome.
        header = tk.Frame(root, bg=HEADER_BG)
        header.pack(fill="x")
        hin = tk.Frame(header, bg=HEADER_BG, padx=self.px(13), pady=self.px(8))
        hin.pack(fill="x")
        tk.Frame(hin, bg=AMBER, width=self.px(8), height=self.px(8)).pack(side="left")
        tk.Label(hin, text="BATC PAUSER", bg=HEADER_BG, fg=HEADER_FG,
                 font=self.font_head).pack(side="left", padx=(self.px(9), 0))
        tk.Label(hin, text="v1.2", bg=HEADER_BG, fg=FAINT,
                 font=self.font_small).pack(side="right")

        outer = tk.Frame(root, bg=BG, padx=self.px(14), pady=self.px(14))
        outer.pack(fill="both", expand=True)

        # Pins the content width so the window does not jitter as text changes.
        tk.Frame(outer, bg=BG, height=1, width=self.px(352)).pack(fill="x")

        # Status readout: two rows boxed by a hairline, split by a divider.
        box = tk.Frame(outer, bg=LINE, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="x")
        self.row_sim = self._status_row(box, "MSFS 2024")
        tk.Frame(box, bg=LINE, height=1).pack(fill="x")
        self.row_log = self._status_row(box, "BEYONDATC")

        self._build_plan_row(outer)

        # Banner: a bold state word with a marker, over a line spelling out
        # exactly what Armed will do.  Boxed with an ink hairline.
        self.banner = tk.Frame(outer, bg=ARMED_BG, highlightthickness=1,
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

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", pady=(0, self.px(4)))
        self.trigger_label = tk.Label(head, text="LAST TRIGGER", bg=BG, fg=MUTED,
                                      font=self.font_small)
        self.trigger_label.pack(side="left")
        self.trigger_time = tk.Label(head, text="--:--:--", bg=BG, fg=FAINT,
                                     font=self.font_small)
        self.trigger_time.pack(side="right")

        self.quote = tk.Label(outer, text="nothing yet", bg=CARD_BG, fg="#35322c",
                              font=self.font_mono, justify="left", anchor="w",
                              wraplength=self.px(326), padx=self.px(10), pady=self.px(8),
                              highlightthickness=1, highlightbackground=LINE)
        self.quote.pack(fill="x")

        self._build_speed_row(outer)

        buttons = tk.Frame(outer, bg=BG)
        buttons.pack(fill="x", pady=(self.px(12), 0))
        # One button that mirrors the sim: it only ever says "Resume sim" when
        # the sim is actually paused, so it can never imply a pause that is not
        # there.  It doubles as the old Test button when the sim is running.
        self.btn_pause = self._button(buttons, "PAUSE SIM", self.on_pause_toggle,
                                      last=False, primary=True)
        self.btn_arm = self._button(buttons, "DISARM", self.on_toggle_arm, last=True)

        self._refresh()

    def _status_row(self, parent, name: str):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=BG, padx=self.px(11), pady=self.px(8))
        inner.pack(fill="x")
        mark = tk.Frame(inner, bg=GREY, width=self.px(7), height=self.px(7))
        mark.pack(side="left", pady=self.px(3))
        tk.Label(inner, text=name, bg=BG, fg=MUTED, font=self.font_body,
                 width=11, anchor="w").pack(side="left", padx=(self.px(9), 0))
        value = tk.Label(inner, text="", bg=BG, fg=INK, font=self.font_body, anchor="w")
        value.pack(side="left", fill="x", expand=True)
        return mark, value

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
        self.root.after(200, self._tick)
        self.root.mainloop()

    def quit(self) -> None:
        self.watcher.stop()
        self.sim.stop()
        if self.batc is not None:       # never leave BeyondATC frozen on exit
            self.batc.resume()
        self.root.destroy()

    def _pause(self) -> None:
        self.sim.pause()
        if self.batc is not None:
            self.batc.suspend()

    def _resume(self) -> None:
        self.sim.resume()
        if self.batc is not None:
            self.batc.resume()

    def _tick(self) -> None:
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
        self._check_rearm()
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
            save_simbrief_id(ident)
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
