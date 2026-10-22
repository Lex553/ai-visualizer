#!/usr/bin/env python3
# ai-visualizer: give your AI agent a face.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ai-visualizer server. Python standard library only, nothing to install.

Serves the face gallery at http://127.0.0.1:8790/ and exposes:

  /state   polled by the faces (~8x/sec):
           {"state":  "idle|listening|thinking|speaking",
            "level":  0.0-1.0,       voice loudness while speaking
            "samples": [64 floats],  raw waveform snapshot (0s when quiet)
            "alert":  bool,          optional attention signal
            "loading": bool}         true while the voice line plays its
                                     own thinking sound (we stay quiet)
  /config  the merged ai-visualizer.json plus the list of installed
           faces, discovered by scanning the faces/ folder. Drop a new
           folder with an index.html into faces/ and it appears in the
           gallery. That is the whole plugin system.

READ-ONLY on the signal bus. The bus is three tiny files written by a
voice line (backtalk writes them natively, github.com/jaredrhod/backtalk):

  .voice_state        idle | listening | thinking | speaking
  .voice_waveform     JSON {ts, samples: [64 floats]} while audio plays
  .voice_loading_pid  exists while the voice line plays a thinking sound
  .voice_alert        optional: non-empty file = attention needed

Where the bus lives comes from "bus_dir" in ai-visualizer.json (default:
this folder). Point it at your backtalk folder, or point backtalk's
"signals_dir" here. Either direction works.

Run:
  python3 server.py             the real bus
  python3 server.py --mock speaking
                                no voice line needed: /state synthesizes
                                the chosen state (idle|listening|thinking
                                |speaking) so you can see a face perform
  python3 server.py --no-open   do not auto-open the browser
Ctrl-C stops.
"""
import json
import math
import mimetypes
import os
import sys
import threading
import time
import webbrowser
import urllib.request
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATES = {"idle", "listening", "thinking", "speaking"}
WAVEFORM_STALE_S = 0.6
# A pid file proves a voice is alive, so this only matters for an older
# voice that does not publish one. Fifteen minutes is long enough for a
# genuinely slow turn and short enough that a dead voice cannot lie for
# a quarter of an hour.
VOICE_STALE_S = 15 * 60

DEFAULTS = {
    "name": "JARVIS",       # shown on the chip / headers, yours to change
    "badge": "",            # optional handle shown in some faces' chrome
    "face": "board",        # the default face the root URL opens
    "port": 8790,
    "bus_dir": "",          # where the .voice_* files live ("" = here)
    "thinking_sound": True, # play assets/thinking.wav while thinking
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((HERE / "ai-visualizer.json").read_text())
        for k, v in user.items():
            cfg[k] = v
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"[config] ai-visualizer.json is not valid JSON ({e}), "
              f"using defaults")
    return cfg


CFG = load_config()
BUS = Path(CFG["bus_dir"]).expanduser() if CFG.get("bus_dir") else HERE

MOCK = None
NO_OPEN = "--no-open" in sys.argv
if "--mock" in sys.argv:
    i = sys.argv.index("--mock")
    MOCK = sys.argv[i + 1] if len(sys.argv) > i + 1 else "speaking"
    if MOCK not in STATES:
        MOCK = "speaking"
PORT = int(CFG.get("port", 8790))
if "--port" in sys.argv:
    i = sys.argv.index("--port")
    PORT = int(sys.argv[i + 1])


def _read_pid(path):
    """The pid a bus file names, or None. Never raises."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    """Ask the OS, never the file.

    A pid file outlives a crash or a force-kill, so its existence proves
    nothing; the reason it is still worth writing is that os.kill(pid, 0)
    can prove the process is GONE and self-heal the bus. PermissionError
    means a live process we may not signal, which is still alive.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ThreadingHTTPServer runs every poll on its own thread, so the fallback
# for an empty file needs a guard. Only the last state is shared, and the
# worst case without this is a one-poll flicker to a slightly older state.
_last_state = "idle"
_last_state_lock = threading.Lock()


def list_faces():
    faces = []
    fdir = HERE / "faces"
    if fdir.is_dir():
        for p in sorted(fdir.iterdir()):
            if p.is_dir() and (p / "index.html").exists():
                meta = {"id": p.name, "title": p.name.title(), "tagline": ""}
                try:
                    meta.update(json.loads((p / "face.json").read_text()))
                except (OSError, ValueError):
                    pass
                meta["id"] = p.name
                faces.append(meta)
    return faces


def mock_bus():
    t = time.time()
    level = 0.0
    samples = [0.0] * 64
    if MOCK == "speaking":
        level = abs(math.sin(t * 6.0)) * 0.85
        samples = [
            (math.sin(i * 0.55 + t * 9.0) * 0.6
             + math.sin(i * 1.7 - t * 13.0) * 0.4)
            * 9000.0 * (0.35 + 0.65 * abs(math.sin(t * 2.6)))
            for i in range(64)
        ]
    return {"state": MOCK, "level": level, "samples": samples,
            "alert": False, "loading": MOCK == "thinking",
            # Faked so the usage readout can be looked at without
            # spending a real session to make it appear.
            "rate_limits": {
                "five_hour": {"utilization": 0.34, "resets_at": t + 9200},
                "seven_day": {"utilization": 0.61, "resets_at": t + 288000},
            }}


def read_bus():
    if MOCK:
        return mock_bus()
    global _last_state
    now = time.time()

    # THE STATE IS NOT A HEARTBEAT. backtalk writes it on transitions only,
    # so "thinking for four minutes" and "died four minutes ago" look the
    # same in the file. The pid is what separates them, and the 15-minute
    # window is the backstop for an older voice that publishes no pid.
    state_path = BUS / ".voice_state"
    try:
        raw_state = state_path.read_text().strip().lower()
        state_mtime = state_path.stat().st_mtime
    except OSError:
        raw_state = ""
        state_mtime = 0.0

    voice_pid = _read_pid(BUS / ".voice_pid")
    if voice_pid is not None:
        fresh = _pid_alive(voice_pid)
    else:
        fresh = state_mtime > 0 and (now - state_mtime) < VOICE_STALE_S

    with _last_state_lock:
        previous = _last_state
        if not fresh:
            state = "idle"
        elif raw_state in STATES:
            state = raw_state
        else:
            # Empty, torn or unknown: keep what the face was already
            # showing rather than snapping to idle for one frame. A real
            # writer replaces the file atomically, so this only happens
            # for an older writer or a genuinely unknown value.
            state = previous
        _last_state = state

    level = 0.0
    samples = [0.0] * 64
    # Parse into locals FIRST, then promote. The old order set "speaking"
    # before parsing, so a payload of ["abc"] * 64 left a talking face
    # with a flat, dead waveform that never recovered.
    candidate = None
    try:
        payload = json.loads((BUS / ".voice_waveform").read_text())
        age = now - float(payload.get("ts", 0))
        raw = payload.get("samples") or []
        # age >= 0 closes the future-timestamp hole: a clock skew or a
        # machine waking with the wrong time used to make a waveform
        # fresh forever.
        if raw and 0 <= age < WAVEFORM_STALE_S:
            parsed = [float(s) for s in raw[:64]]
            mean = sum(abs(s) for s in parsed) / len(parsed)
            candidate = (parsed, min(1.0, mean / 3000.0))
    except (OSError, ValueError, KeyError, TypeError):
        candidate = None
    if candidate is not None:
        # A fresh waveform IS speech, whatever the state file says.
        state = "speaking"
        samples, level = candidate
    try:
        alert = (BUS / ".voice_alert").stat().st_size > 0
    except OSError:
        alert = False
    # A pid FILE is not a playing sound: the old boolean kept the
    # browser's thinking sound muted forever after one zombie ffplay.
    loading = _pid_alive(_read_pid(BUS / ".voice_loading_pid"))
    # Absent unless the voice line was told to publish it, which is the
    # normal case: it is the account holder's own spend and it stays off
    # until asked for. An empty dict simply means no readout.
    rate_limits = {}
    try:
        rate_limits = json.loads((BUS / ".voice_rate_limits").read_text())
    except (OSError, ValueError):
        pass
    return {"state": state, "level": level, "samples": samples,
            "alert": alert, "loading": loading, "rate_limits": rate_limits}


class Handler(BaseHTTPRequestHandler):
    # Everything the browser actually needs. The old root was HERE, which
    # served server.py, the personal config and the whole of .git -- a
    # browsable history and a leaked absolute bus path. Anything not on
    # this list is a 404, whatever the path resolves to.
    _ROOT_FILES = {"index.html", "core.js"}
    _ROOT_DIRS = {"faces", "assets"}

    def _host_ok(self):
        # The server binds loopback only, but without this check a
        # DNS-rebinding page in any browser on the machine could read the
        # bus and the gallery through its own hostname.
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self._host_ok():
            self._send(b"forbidden", "text/plain", 403)
            return
        path = self.path.split("?")[0]
        try:
            if path == "/state":
                self._send(json.dumps(read_bus()).encode(),
                           "application/json")
            elif path == "/config":
                out = {"name": CFG["name"], "badge": CFG["badge"],
                       "face": CFG["face"],
                       "thinking_sound": bool(CFG["thinking_sound"]),
                       "faces": list_faces()}
                self._send(json.dumps(out).encode(), "application/json")
            else:
                self._static(path)
        except ConnectionError:
            # THE WHOLE FAMILY, not one member of it. A tab closed or
            # reloaded mid-response raises ConnectionResetError, which is a
            # SIBLING of BrokenPipeError rather than a subclass -- so
            # catching only BrokenPipeError sent it to the generic branch
            # below, which then wrote a 500 back down the socket that had
            # just died and raised a SECOND, uncaught error from inside
            # flush_headers(). One disconnect, two tracebacks. ConnectionError
            # is the common parent of Reset, Broken, Aborted and Refused.
            pass
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            try:
                self._send(body, "application/json", 500)
            except ConnectionError:
                # A real error AND the client already gone. There is nobody
                # left to tell; saying so twice helps no one.
                pass

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        rel = path.lstrip("/")
        parts = rel.split("/")
        allowed = (rel in self._ROOT_FILES
                   or (len(parts) > 1 and parts[0] in self._ROOT_DIRS))
        if not allowed:
            self._send(b"not found", "text/plain", 404)
            return
        target = (HERE / rel).resolve()
        if target != HERE and HERE not in target.parents:
            self._send(b"not found", "text/plain", 404)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or \
            "application/octet-stream"
        self._send(target.read_bytes(), ctype)

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    mode = f"MOCK={MOCK}" if MOCK else f"bus: {BUS}"
    root = f"http://127.0.0.1:{PORT}/"
    # The browser opens on the configured face; the gallery stays at "/" for switching.
    face = CFG.get("face", "")
    url = f"{root}faces/{face}/" if face and (HERE / "faces" / face / "index.html").exists() else root
    # ALREADY RUNNING IS NOT AN ERROR, and treating it as one was the whole
    # bug. Closing the browser tab does not stop this server; it keeps going
    # headless. Relaunching then failed to bind, died before the line that
    # opens the browser, and took the traceback with it when the launcher
    # window closed. The end-user symptom was "I can hear my agent but the
    # face never shows up", with the face running perfectly the entire time.
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        # Something holds the port. Ask it whether it is us before claiming
        # anything: a stranger on this port is a different problem and
        # deserves a different sentence.
        mine = False
        try:
            with urllib.request.urlopen(root + "state", timeout=2) as r:
                mine = r.status == 200
        except Exception:
            mine = False
        if mine:
            print(f"already running at {root}  opening it instead", flush=True)
            if not NO_OPEN:
                webbrowser.open(url)
            sys.exit(0)
        print(f"port {PORT} is taken by something that is not this server.",
              flush=True)
        print("Close whatever is using it, or set a different \"port\" in "
              "ai-visualizer.json.", flush=True)
        sys.exit(1)
    srv.allow_reuse_address = True
    print(f"ai-visualizer on {root}  opening {url}  ({mode})  Ctrl-C stops", flush=True)
    if not NO_OPEN:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
