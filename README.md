# ATC Pauser

Pauses Microsoft Flight Simulator 2024 at a point you choose - either a **waypoint from
your SimBrief plan**, or the moment your ATC add-on (**BeyondATC** or **SayIntentions.AI**)
clears you for the arrival or issues a descent. Walk away in cruise, come back to a sim
frozen where you wanted it.

## Running it

Double-click **`Start ATC Pauser.bat`**. Nothing to install: it uses the Python and
`SimConnect` package already on this machine.

Order does not matter - start it before or after MSFS and BeyondATC, in any combination.
It reconnects on its own and survives either one restarting.

## The window

| Row | Meaning |
|---|---|
| **MSFS 2024** | green once SimConnect is connected *and proven* - the events it needs are mapped |
| **BeyondATC** / **SayIntent.** | the active provider (from Settings). BeyondATC: green while `Player.log` is being written (`idle for 53m` means it is closed). SayIntentions: `listening` once the API key works and a flight is active |
| **Pause at** | pick a waypoint from your loaded SimBrief plan, or leave it on `— arrival / descent —` to arm on the ATC clearance instead. **Load** fetches your latest plan |
| **Banner** | bold state - `ARMED`, `PAUSED`, `TRIGGERED`, or `DISARMED` - over a line spelling out exactly what will happen (`will pause 5 nm before HELEN`, or `will pause on a STAR or descent clearance`) |
| **Last trigger** | which arm fired (`WP HELEN`, `STAR`, `DESCENT`, `CPDLC`), local time, and the clearance text or distance |
| **Speed** | time acceleration - `1×`, `2×`, `4×`; the live rate is highlighted. Accelerate through cruise, then it drops back to `1×` automatically the moment a pause fires, so you never resume into fast-forward |

- **Pause sim / Resume sim** is one button that follows the sim: it reads `Pause sim`
  while the sim is running (press it to freeze the sim now - handy to confirm the MSFS
  link) and flips to `Resume sim` only while the sim is actually paused.
- **Disarm** for when you are back at the desk and do not want surprise pauses.
- The **⚙ gear** (top-right) opens **Settings**, where the *freeze BeyondATC* toggle and
  *Telegram phone alerts* live - kept off the main panel so it stays compact. It also has
  an optional **Global pause hotkey**: click its field and press a shortcut such as
  `Ctrl+Alt+P`. It works while MSFS has focus and toggles Pause / Resume just like the
  main button. It is off until you assign it. Use **Clear** to remove it.

The pause is a **complete freeze**. MSFS 2024 has no SimConnect pause that stops the world
clock (both `PAUSE_ON` and `PAUSE_SET` only give an *active* pause - the aircraft freezes
but time of day keeps advancing, confirmed against the sim and Asobo's own dev notes). So
the app freezes the aircraft **and** holds the clock still, re-setting the Zulu time once a
second while paused (`ZULU_HOURS_SET` / `ZULU_MINUTES_SET`). Set `hold_clock` to `false` to
let time run during a pause.

## Choosing your ATC provider

Open **Settings** (the ⚙ gear) and pick **BeyondATC** or **SayIntentions** at the top. The
choice is remembered.

- **BeyondATC** (the default) needs nothing extra - it reads BeyondATC's local `Player.log`.
- **SayIntentions.AI** has no local transcript; its ATC communications live in the cloud, so
  ATC Pauser reads them through SayIntentions' own API. Paste your **API key** (from the
  SayIntentions pilot portal - a subscription is required) into the **API KEY** field and
  press **TEST** to confirm it works. The key is stored locally in `config.json` and is only
  ever sent to SayIntentions.

The **SimBrief waypoint arm below works with either provider** (it uses only your flight plan
and the aircraft's position), so you can use it even without an ATC add-on running.

## Pausing at a waypoint

Type your SimBrief **Pilot ID** or **username** into the **SimBrief** box in the window
and press **Load** - it is saved for next time, so you enter it once. The dropdown then
fills with every fix in your plan tagged by phase (`CLB`, `CRZ`, `DES`). Pick one and the
sim pauses `waypoint_arm_nm` (default 5) nautical miles before you reach it, measured
from the aircraft's own SimConnect position.

Selecting a waypoint does **not** switch off the ATC arm - both run at once, and
whichever fires first pauses the sim. So a descent clearance that comes before your
waypoint still catches you.

## Freezing the ATC add-on too

With **Also freeze the ATC app when paused** on in **Settings** (⚙, the default), a pause
also **suspends the active ATC add-on's process** - it stops talking and holds its state,
then picks up where it left off on resume. Neither add-on exposes a control API, so this is
done by freezing its threads at the OS level (fully reversible; the app also thaws it on
exit). Set `beyondatc_process` / `sayintentions_process` if your executable is named
differently. Caveats: pausing mid-transmission cuts the audio abruptly. And because
**SayIntentions is cloud-based**, suspending its local client mutes it during the pause but
the server-side AI may move on, so it resumes less seamlessly than BeyondATC - if that
bothers you, turn this toggle off under SayIntentions and just freeze the sim.

## Getting a phone alert (Telegram)

Optional: have the app **message your phone the moment it pauses**, so you know to come
back. It uses a Telegram bot - free, needs nothing beyond Telegram itself, and it only
ever messages **you**.

One-time setup, about two minutes:

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, and follow the prompts
   (any name). BotFather replies with a **bot token** like `1234567890:AAE…` - copy it.
2. Open a chat with **@userinfobot** and send it anything. It replies with your numeric
   **chat ID**, e.g. `987654321` - copy it.
3. **Send your new bot any message** (e.g. `hi`). Telegram forbids a bot from messaging
   someone who has never written to it first, so skipping this makes the test fail with
   *"chat not found"*. This is the step people forget.
4. In ATC Pauser, click the **⚙ gear**, paste the token and chat ID into **Bot token**
   and **Chat ID**, and press **Send test**. A message should arrive on your phone. The
   fields save on their own.

From then on you get a **⏸ paused** message (with the trigger and Zulu time) when the app
pauses, and a **▶ resumed** message when you unpause. Clear either field to switch alerts
off. The token and chat ID can also be set directly in `config.json`, and the two messages
toggled with `notify_on_pause` / `notify_on_resume`.

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

**SayIntentions.AI** works differently: it keeps no local transcript (its local files are
sim telemetry). The ATC conversation lives in the cloud, so ATC Pauser polls SayIntentions'
`getCommsHistory` endpoint every few seconds with your API key, asking only for messages
newer than the last one it saw - the cloud equivalent of tailing a log. It matches what ATC
said (each entry's `outgoing_message`) against `si_trigger_patterns`, and because
SayIntentions only ever talks to you, no callsign filtering is needed. On first sight of a
flight it baselines to the current end, so the history already on the server never fires.

Pausing itself is SimConnect `PAUSE_ON` (an active pause), with the Zulu clock held still
each second so time of day does not drift (see **The window** above). The ATC add-on keeps
talking while the sim is frozen (it runs outside the sim), so you still hear the clearance.

## Configuration

`config.json` sits next to the program and is written with defaults on first run.

| Key | Default | Notes |
|---|---|---|
| `provider` | `"beyondatc"` | which ATC add-on to listen to: `"beyondatc"` or `"sayintentions"`; usually set from **Settings** |
| `player_log` | `"auto"` | (BeyondATC) `auto` resolves the path above; or give an explicit path |
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
| `pause_beyondatc` | `true` | suspend the active ATC add-on's process while paused so it freezes too (toggled in **Settings**; key name kept for back-compat) |
| `beyondatc_process` | `"BeyondATC.exe"` | process to suspend when the provider is BeyondATC |
| `sayintentions_api_key` | `""` | (SayIntentions) your pilot-portal API key; usually set from **Settings**. Blank = no clearance detection |
| `sayintentions_process` | `"SayIntentions.exe"` | process to suspend when the provider is SayIntentions |
| `si_trigger_patterns` | 4 patterns | (SayIntentions) case-insensitive regexes matched against what ATC said, to arm on arrival/descent |
| `si_poll_interval_s` | `3.0` | (SayIntentions) how often the cloud API is polled |
| `pause_hotkey` | `""` | optional Windows-wide Pause / Resume shortcut, e.g. `"CTRL+ALT+P"`; blank disables it |
| `telegram_bot_token` | `""` | Telegram bot token from @BotFather; usually set from **Settings**. Blank = alerts off |
| `telegram_chat_id` | `""` | your numeric chat ID from @userinfobot |
| `notify_on_pause` | `true` | send a Telegram message when the sim pauses |
| `notify_on_resume` | `true` | send a Telegram message when you resume |
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

1. Press **Pause sim** with MSFS 2024 in a flight. The sim must visibly pause, and
   **Resume sim** must unpause it. This proves the bundled `SimConnect.dll` talks to
   MSFS 2024. If it does not, set `simconnect_dll`.
2. On a real CPDLC leg, capture the actual `Message queued` uplinks and tighten
   `cpdlc_uplink_content_patterns` to the wording BeyondATC really sends. The defaults
   are deliberately broad; because direction is filtered first, a broad pattern can only
   over-match ATC's own uplinks, never your requests.
