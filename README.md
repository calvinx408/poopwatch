# PoopWatch

Watches a Reolink camera feed and works out whether a dog defecated on your
property, and whether whoever was walking it picked the mess up.

Get a push notification on your phone the moment it happens, with a
snapshot and a link to the event. It's free, with no account needed, and
it works on iPhone and Android through [ntfy](https://ntfy.sh).

![PoopWatch dashboard](docs/dashboard-demo.png)

It is built as a funnel, because running a vision model on 24/7 footage is both
slow and expensive. Each stage only passes survivors to the next:

| Stage | What it does | Cost |
|---|---|---|
| 0. Camera AI | Reolink's own on-device `dog_cat` detector fires | free |
| 1. Local detector | YOLO11 confirms a dog is actually in frame *(optional)* | free |
| 2. Gate | A local VLM screens the frames for a squatting posture | free via Ollama |
| 3. Verdict | A vision model judges defecation, handler presence, and cleanup (Ollama or Claude) | only on gate hits |

In practice the camera's own detector eliminates almost everything, the gate
eliminates the dogs that were merely sniffing, and the paid stage runs a handful
of times a day at most. Set `VERDICT_MODEL=ollama` to run it entirely free.

## Cameras

Add your cameras in the dashboard's **Settings** panel. Each camera needs a name,
an IP address, a channel (0 for a standalone camera, or the channel number on an
NVR), and a password. You can add as many cameras as you like.

To add a camera:
1. Open the dashboard and click **Settings**. Find the **Cameras** section.
2. Click **Add camera** and fill in its name (for example `front yard`), its IP
   address, and its channel. Use `0` for a standalone camera.
3. Enter the camera's login password, then click **Test connection**. It checks
   that the camera answers.
4. Choose a **Stream**: `main` for full resolution, or `sub` for less data.
5. Click **Save and close**. The camera starts recording right away.

Passwords are stored in your operating system's credential store, not in the
project. Camera names, addresses, and channels are stored in the settings
database under `evidence\`.

Each camera runs its own recorder, poll loop, and cooldown, so one camera going
offline retries on its own without stalling the others.

### Keeping a slow machine healthy

Every camera adds a continuously running ffmpeg process plus its share of disk.
Three levers, in the order worth reaching for:

1. **Turn a camera off from the dashboard.** Each camera is a pill in the header
  , click it to stop or start that one camera. Stopping it kills its ffmpeg
   process and disk writes immediately, and the others keep running.
2. **`MAX_CONCURRENT_ANALYSES`** (default 2) caps how many events may be
   analyzed at once *across all cameras*, so simultaneous triggers queue instead
   of fighting over the GPU. Capture is deliberately outside that limit, it is
   mostly just waiting out the post-roll window.
3. **Choose the `sub` stream** for a camera in Settings, and a shorter retention
   time, which cut disk and bandwidth sharply, at some cost in image detail.

Rolling-buffer disk use is roughly `cameras x retention x bitrate`.

## Why a rolling buffer

Dogs squat briefly, and a camera's motion detector often fires *after* the fact.
PoopWatch continuously stream-copies the camera into 10-second segments (no
re-encoding, so effectively 0% CPU) and treats an event as a *time range* into
that ring buffer. That gives it 30 seconds of footage from **before** the
trigger, which is usually where the evidence actually is.

## Requirements

- Python 3.11+ (tested on 3.14)
- ffmpeg, `winget install Gyan.FFmpeg`
- Node 20+ (only to build the dashboard)
- A Reolink camera or NVR reachable on your network, with RTSP enabled
- [Ollama](https://ollama.com) for the free local gate, or an Anthropic API key

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# free local gate
ollama pull qwen2.5vl:7b

# the dashboard
cd web && npm install && npm run build && cd ..

```

Camera addresses, passwords, and alert settings are entered in the dashboard's
**Settings** panel, so you don't need a `.env` file. Copy `.env.example` to
`.env` only if you want Claude as the verdict model, which needs
`ANTHROPIC_API_KEY`.

Check everything before you rely on it:

```bash
.venv\Scripts\python -m poopwatch probe
.venv\Scripts\python -m poopwatch probe --camera driveway   # just one
.venv\Scripts\python -m poopwatch probe --skip-rtsp         # skip the slow check
```

`probe` verifies ffmpeg, the gate backend, your alert channels, and then every
camera in turn, API reachability, whether that model supports animal AI
detection, snapshots, and RTSP, and tells you which piece is the broken one.

## Running

```bash
.venv\Scripts\python -m poopwatch serve      # dashboard + watcher
.venv\Scripts\python -m poopwatch watch      # headless
.venv\Scripts\python -m poopwatch replay FILE.mp4   # classify existing footage
```

The dashboard is at <http://127.0.0.1:8420>. `replay` is the fastest way to tune
the prompts: point it at a clip you already have and see what the model says, no
camera or waiting involved.

For UI development, `cd web && npm run dev` serves on :5173 and proxies data
to the Python backend.

### Starting it on Windows

`start-poopwatch.bat` in the project folder starts the server in a minimized
window and opens the dashboard in your browser. Double-click it to start the
app by hand.

To start it automatically when you log in, put a shortcut to the launcher in
your Startup folder. Press `Win+R`, type `shell:startup`, and create a shortcut
there that points to `start-poopwatch.bat` in the project folder. Startup
shortcuts run at login, which is what you want: camera passwords live in your
Windows account's credential store, which only exists once you're logged in.

To stop the automatic start, delete that shortcut from the Startup folder.

To restart after changing code, double-click `restart-poopwatch.bat`. It stops
any running copy of the server, then starts it again. Don't run
`start-poopwatch.bat` while the server is still running: the second copy can't
bind port 8420.

### Dashboard password

By default the dashboard is reachable only from this computer. To open it from
other devices on your home network:

1. Set a password. It's stored in your OS credential store, not in the project:
   ```
   .venv\Scripts\python -m poopwatch set-password
   ```
2. Set `HOST=0.0.0.0` in `.env`. `serve` refuses to listen on the network until
   a password is set.
3. Restart the server. Browsers ask for a login. The username is `poopwatch`,
   and the password is the one you set.

To go back to local-only, run `.venv\Scripts\python -m poopwatch clear-password`
and set `HOST` back to `127.0.0.1`.

If you forget the password, only someone at the PC can reset it, because the
app has no "forgot password" link. Run these in the project folder:

```
.venv\Scripts\python -m poopwatch clear-password
.venv\Scripts\python -m poopwatch set-password
```

Then restart the server. Every device will need the new password. If `HOST` is
still `0.0.0.0`, the server won't start until a password is set.

The password is sent over plain HTTP, so on a shared Wi-Fi network someone
could read it. For encryption, put the dashboard behind Tailscale or a reverse
proxy such as Caddy.

Analysis needs Ollama running. The Ollama installer adds a Startup shortcut, so
it starts at login. Delete the `Ollama.lnk` shortcut from `shell:startup` if you
don't want that.

## The dashboard

Events are cards with a thumbnail and a verdict badge, red for *not picked up*,
green for *cleaned up*, muted for the ones a cheap stage filtered out. Each card
is tagged with the camera it came from. Opening one gives you the clip, a
scrubbable frame strip, the model's reasoning, and buttons to mark it correct, a
false positive, or handled.

To remove an event, open it and click **Delete event** in the "Danger zone"
section at the bottom. This deletes its database row and its clip, frames, and
thumbnail. It can't be undone.

The header carries one pill per camera: a green dot means healthy, and clicking
it stops or starts that camera on its own. With more than one camera configured,
the filter row also gains a per-camera filter.

Filtered events are kept deliberately: they are the cheapest way to see what the
funnel is throwing away, and whether it's throwing away too much.

## Configuration

Most settings are edited in the dashboard's **Settings** panel. Camera and alert
settings are saved in `evidence/poopwatch.db`, and passwords go to the operating
system's credential store (Windows Credential Manager, macOS Keychain, or Secret
Service on Linux). `.env` is optional and overrides the saved settings. See
`.env.example` for the full annotated list. The settings worth knowing:

| Setting | Default | Notes |
|---|---|---|
| `PRE_ROLL_SECONDS` | 30 | Footage kept from before the trigger |
| `POST_ROLL_SECONDS` | 120 | How long to watch for cleanup afterwards |
| `GATE_BACKEND` | `ollama` | `ollama` (free), `haiku` (~$0.001/event), `off` |
| `VERDICT_MODEL` | `claude-opus-5` | Set to `ollama` to run fully free |
| `DISABLE_LOCAL_DETECTOR` | `false` | Skip stage 1 even if ultralytics is installed |
| `MAX_CONCURRENT_ANALYSES` | 2 | Concurrent analyses across all cameras |

The optional free local detector needs `pip install -r requirements-local.txt`
(this pulls in torch, ~2GB). Without it, stage 1 is simply skipped.

## Running costs

Ollama runs the gate and verdict for free. If you switch either stage to Claude,
you pay Anthropic per token. These are list prices per million tokens, checked
2026-11-07:

| Model | Input | Output |
|---|---|---|
| Claude Haiku 5.5 (prompts up to 100K tokens) | $0.10 | $0.50 |
| Claude Opus 5.5 | $4.00 | $20.00 |

Images are billed as input tokens: each image costs about
`ceil(width / 28) × ceil(height / 28)` tokens. Your 1280×960 camera frames are
about 1,610 tokens each. A verdict sends 16 frames, and the gate sends 6.

Estimated cost per event at that resolution:

| Stage | Haiku 5.5 | Opus 5.5 |
|---|---|---|
| Gate (6 frames) | under $0.001 | about $0.05 |
| Verdict (16 frames) | about $0.003 | about $0.13 |

These are estimates. Actual cost depends on the number of frames, the output
length, and how often the gate passes. Check the
[current pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
before you rely on these numbers.

## Alerts: push notifications to your phone

PoopWatch sends a push notification to your phone through
[ntfy](https://ntfy.sh). It's free, needs no account, and works on iPhone and
Android. Each alert includes a snapshot from the camera, and tapping it opens
the event on your dashboard.

Setup takes about two minutes:
1. Install the ntfy app and pick a topic name nobody else would guess, such as
   a random word plus a few digits.
2. In the dashboard's **Settings**, under **Alerts**, enter that topic in
   **ntfy topic** and save.
3. Subscribe to the same topic in the ntfy app.

Email works too. Enter the SMTP settings under **Alerts**. For Gmail you need an
app password, not your account password.

What you get:
- **Poop left behind:** always sent. A dog defecated and nobody picked it up.
- **Poop picked up:** a "Good human" message, when the owner cleans up. Optional.
- **Dog walk-by:** a message when a dog walks past without pooping. Optional.
- **Camera offline:** sent if a camera has no video for 10 minutes, and again
  when it's back.

Choose the optional alerts with the **Extra alerts** dropdown in Settings.

To link alerts to the dashboard, enter the dashboard address under **Alerts**,
for example `http://192.168.4.143:8420`. Your phone has to reach that address,
so it works on your home Wi-Fi or over a private network such as Tailscale.

## Checking and improving detection

Check the cameras whenever recordings look damaged or stop:

```
.venv\Scripts\python -m poopwatch check-cameras
```

It reports ping loss, whether each stream answers, and how many decode errors
the latest recording has. Decode errors mean parts of the video were damaged in
transit, usually from Wi-Fi.

Improve the prompts from your feedback:

1. Open events in the dashboard, mark wrong ones **False positive**, and write
   a note on why. Notes are saved with the **Save note** button.
2. Export them:
   ```
   .venv\Scripts\python -m poopwatch export-false-positives
   ```
   This writes `evidence
eviewalse_positives.md`.
3. Run Claude Code with `scripts\prompt_review.md` as its instructions. It
   proposes changes in `evidence
eview\prompt_proposal.diff` and doesn't edit
   the prompts itself.
4. Save representative events as test cases, then check any change against
   them:
   ```
   .venv\Scripts\python -m poopwatch save-case <event id> <name> <pass|reject>
   .venv\Scripts\python -m poopwatch regress
   ```
   Keep a change only if every case still passes.

To run the detector and gate again on an event's saved frames, for example after
a change:

```
.venv\Scripts\python -m poopwatch reprocess-event <event id>
```

## Accuracy, honestly

This is a camera pointed at a yard, not a courtroom exhibit.

- The verdict model is deliberately conservative and will report
  `defecation_occurred=false` when the footage is dark, distant, or obstructed.
  Expect misses before expect false alarms.
- "Did they pick it up" is the hardest judgment in the system. A handler who
  bags the mess just outside the frame, or after the post-roll window closes,
  will read as a violation. Lengthen `POST_ROLL_SECONDS` if you see that.
- A 7B local model is a decent gate but noticeably weaker at the temporal
  reasoning the verdict needs. Running stage 3 on Ollama costs nothing and will
  be meaningfully less reliable.
- Use the false-positive button. Those labels are what you tune against.

## Privacy

This records video of a public-facing area, which may include people who have
not consented to being filmed and analyzed. A few things follow from that:

- **`evidence/` is gitignored and must stay that way.** It holds raw footage,
  extracted frames, and the SQLite log. None of it belongs in a public repo.
- `.env` is gitignored; only `.env.example` with placeholders is committed.
- Camera credentials are scrubbed from ffmpeg error output before it is logged
  or served, because ffmpeg echoes the RTSP URL, password included, in most
  of its error messages, and the dashboard displays those.
- The server binds to `127.0.0.1` by default, so only this machine can reach it.
  To open it on your home network, set a dashboard password first (see
  "Dashboard password" below). Don't expose it to the internet.
- Frames of your camera's view are sent to the Anthropic API when stage 3 runs.
  Set `VERDICT_MODEL=ollama` if you would rather nothing left the machine.
- Camera recording laws vary by jurisdiction, particularly where audio or a
  neighbour's property is in frame. Worth a look before you point it anywhere.

## Layout

```
poopwatch/
  config.py     settings from .env
  reolink.py    camera HTTP API (AI state, snapshots), one client per camera
  capture.py    rolling segment recorder, clip assembly, frame extraction
  detector.py   optional free YOLO stage
  vision.py     Ollama + Claude backends behind one interface
  analyze.py    the schemas and prompts for the gate and the verdict
  pipeline.py   the funnel
  watcher.py    one poll loop per camera, plus the pool that owns them
  server.py     FastAPI backend
  store.py      SQLite event log
  notify.py     ntfy + email
web/            React dashboard (Vite)
```

## License

PolyForm Noncommercial 1.0.0. See [LICENSE](LICENSE).

You may use, copy, and change PoopWatch for personal, hobby, research, and other noncommercial purposes. Commercial use, including selling it or offering it as a paid service, needs a separate commercial license from the copyright holders.
