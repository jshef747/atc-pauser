# BATC Pauser

Pauses Microsoft Flight Simulator 2024 at a point you choose - either a **waypoint from
your SimBrief plan**, or the moment **BeyondATC** clears you for the arrival or issues a
descent. Walk away in cruise, come back to a sim frozen where you wanted it.

## Running it

Double-click **`Start BATC Pauser.bat`**. Nothing to install: it uses the Python and
`SimConnect` package already on this machine.

Order does not matter - start it before or after MSFS and BeyondATC, in any combination.
It reconnects on its own and survives either one restarting.

## The window

| Row | Meaning |
|---|---|
| **MSFS 2024** | green once SimConnect is connected *and proven* - the events it needs are mapped |
| **BeyondATC** | green while `Player.log` is actively being written; `idle for 53m` means BeyondATC is closed |
| **Pause at** | pick a waypoint from your loaded SimBrief plan, or leave it on `— arrival / descent —` to arm on the ATC clearance instead. **Load** fetches your latest plan |
| **Banner** | bold state - `ARMED`, `PAUSED`, `TRIGGERED`, or `DISARMED` - over a line spelling out exactly what will happen (`will pause 5 nm before HELEN`, or `will pause on a STAR or descent clearance`) |
| **Last trigger** | which arm fired (`WP HELEN`, `STAR`, `DESCENT`, `CPDLC`), local time, and the clearance text or distance |
| **Speed** | time acceleration - `1×`, `2×`, `4×`; the live rate is highlighted. Accelerate through cruise, then it drops back to `1×` automatically the moment a pause fires, so you never resume into fast-forward |

- **Pause sim / Resume sim** is one button that follows the sim: it reads `Pause sim`
  while the sim is running (press it to freeze the sim now - handy to confirm the MSFS
  link) and flips to `Resume sim` only while the sim is actually paused.
- **Disarm** for when you are back at the desk and do not want surprise pauses.

The pause is a **complete freeze**. MSFS 2024 has no SimConnect pause that stops the world
clock (both `PAUSE_ON` and `PAUSE_SET` only give an *active* pause - the aircraft freezes
but time of day keeps advancing, confirmed against the sim and Asobo's own dev notes). So
the app freezes the aircraft **and** holds the clock still, re-setting the Zulu time once a
second while paused (`ZULU_HOURS_SET` / `ZULU_MINUTES_SET`). Set `hold_clock` to `false` to
let time run during a pause.

## Pausing at a waypoint

Type your SimBrief **Pilot ID** or **username** into the **SimBrief** box in the window
and press **Load** - it is saved for next time, so you enter it once. The dropdown then
fills with every fix in your plan tagged by phase (`CLB`, `CRZ`, `DES`). Pick one and the
sim pauses `waypoint_arm_nm` (default 5) nautical miles before you reach it, measured
from the aircraft's own SimConnect position.

Selecting a waypoint does **not** switch off the ATC arm - both run at once, and
whichever fires first pauses the sim. So a descent clearance that comes before your
waypoint still catches you.

## Freezing BeyondATC too

With `pause_beyondatc` on (the default), a pause also **suspends the BeyondATC process** -
it stops talking and holds its state, then picks up exactly where it left off on resume.
BeyondATC has no API, so this is done by freezing its threads at the OS level (fully
reversible; the app also thaws it on exit). Set `beyondatc_process` if your executable is
named differently. Caveats: pausing mid-transmission cuts the audio abruptly, and a very
long freeze may make BeyondATC's backend connection reconnect on resume.

## Re-arming after a pause

By default (`rearm_mode: "resume"`) a pause **stays put until you resume it** - the banner
reads `PAUSED · resume when you are ready`, and nothing re-pauses you while you are away.
The moment you press **Resume sim** it arms again immediately, so a later descent still
catches you.

Two other modes are available via `rearm_mode`:

- `"timer"` - arm again `rearm_seconds` after the pause, resumed or not.
- `"manual"` - one pause per arming; it disarms and waits for you to press **Arm**.

## How it knows

BeyondATC has no public API, but it is a Unity app that writes every transmission to:

```
%USERPROFILE%\AppData\LocalLow\Skirmish Mode Games, Inc\BeyondATC\Player.log
```

**Voice.** Calls to your aircraft are logged as a `[ControllerScript]` line naming the
BeyondATC class that generated them, followed by an `[Instruction]` line with the text:

```
[ControllerScript] AICommunicationSystem.IFR.ExpectStarInstructionScript
------------------------------
[PlayerState] Thursday 01:33, lat: 40.6055, alt: 41197, com1: 127.500(On)
[Instruction] Smart Cat 234, cleared NAVE1K arrival, runway 19L.
------------------------------
```

Two properties make this exact rather than a guess:

- `[ControllerScript]` appears **only on ATC-initiated calls**. Your own readback lands
  as a bare `[Instruction]` with no script line, so it can never trigger a pause.
- `[Instruction]` is **only ever emitted for your aircraft**. In a full flight's log,
  all 65 `[Instruction]` lines were the player's and 258 descent calls to other traffic
  appeared under `[LocalVoiceInput]`, which is never read for triggers. So it cannot
  pause on another aircraft's clearance, and needs no callsign matching to guarantee it.

**CPDLC.** On datalink the same clearances arrive as uplinks instead:

```
[CPDLC] Message queued: ID=41 Type=CPDLC From=NTTT To=TTW234 MRN=40 Resp=WU Content=DESCEND TO AND MAINTAIN FL180
```

Here direction does matter, so the watcher learns your callsign from the
`CPDLC_Relay: Inbound` line (always your own downlink) and only fires on `To=<you>`.
That is why your own `REQUEST DESCEND` does not trip it.

Pausing itself is SimConnect `PAUSE_ON` (an active pause), with the Zulu clock held still
each second so time of day does not drift (see **The window** above). BeyondATC keeps
talking while the sim is frozen, because it runs outside the sim - so you still hear the
clearance.

## Configuration

`config.json` sits next to the program and is written with defaults on first run.

| Key | Default | Notes |
|---|---|---|
| `player_log` | `"auto"` | `auto` resolves the path above; or give an explicit path |
| `simconnect_dll` | `"auto"` | point at an MSFS 2024 SDK `SimConnect.dll` if the bundled one ever fails |
| `simbrief_id` | `""` | your SimBrief Pilot ID (numeric) or username; usually set from the **SimBrief** box in the window rather than here |
| `waypoint_arm_nm` | `5.0` | how many nm before the chosen waypoint to pause |
| `hold_clock` | `true` | hold the Zulu time of day still while paused (MSFS 2024 has no clock-stopping pause); `false` lets time run |
| `rearm_mode` | `"resume"` | `resume`, `timer`, or `manual` - see **Re-arming after a pause** |
| `trigger_patterns` | 3 scripts | regexes matched against whole log lines |
| `cpdlc_enabled` | `true` | set `false` to ignore datalink entirely |
| `cpdlc_uplink_content_patterns` | 3 patterns | regexes matched against `Content=` of uplinks **to you** |
| `poll_interval_ms` | `250` | how often the log is checked |
| `instruction_timeout_s` | `10` | fire anyway if the clearance text never arrives |
| `log_idle_seconds` | `180` | older than this and BeyondATC is reported idle |
| `rearm_seconds` | `30` | grace hold before arming again - only used by `timer` mode (`resume` mode re-arms instantly) |
| `pause_beyondatc` | `true` | suspend the BeyondATC process while paused so it freezes too |
| `beyondatc_process` | `"BeyondATC.exe"` | the process name to suspend |
| `start_armed` | `true` | |
| `always_on_top` | `true` | |

Triggers are plain regexes, so retuning what pauses the sim - or repairing it if a
BeyondATC update renames a class - is an edit, not a rebuild. Other useful class names
seen in BeyondATC 1.10: `LeavingStarInstructionScript`, `VectorInstructionScript`,
`CruiseAltitudeCorrectScript`, `ApproachHandoffScript`.

The file is read as UTF-8 with or without a byte-order mark, so editing it in Notepad
is safe.

## Verified

Confirmed by test against BeyondATC 1.10.0 and the real `Player.log`:

- all three trigger scripts fire, with the right clearance text
- your readback, handoffs, cruise-altitude corrections and other aircraft's descent
  clearances do **not** fire
- CPDLC uplinks fire; your own CPDLC requests and logon traffic do not
- a trigger with no `[Instruction]` line still fires after the timeout
- pre-existing log history never fires on startup
- BeyondATC restarting (log truncated) is handled, and watching does not lock
  BeyondATC out of writing its own log
- the app reports honestly when MSFS is absent rather than claiming a connection

**Still to confirm with the sim running** - the two things no offline test can settle:

1. Click **Test** with MSFS 2024 in a flight. The sim must visibly pause, and
   **Resume sim** must unpause it. This proves the bundled MSFS-2020-era
   `SimConnect.dll` talks to MSFS 2024. If it does not, set `simconnect_dll`.
2. On a real CPDLC leg, capture the actual `Message queued` uplinks and tighten
   `cpdlc_uplink_content_patterns` to the wording BeyondATC really sends. The defaults
   are deliberately broad; because direction is filtered first, a broad pattern can only
   over-match ATC's own uplinks, never your requests.
