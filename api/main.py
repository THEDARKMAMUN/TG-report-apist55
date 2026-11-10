import os
import json
import random
import re
import signal
import sys
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread, Lock, Event
from time import sleep, time
from collections import deque
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import phonenumbers
from phonenumbers import PhoneNumberFormat
from random_user_agent.user_agent import UserAgent
from random_user_agent.params import SoftwareName, OperatingSystem

try:
    from emailtools import generate
except ImportError:
    # fallback: simple email generator
    def generate(_domain="gmail"):
        name = f"user{random.randint(10000, 999999)}"
        return f"{name}@gmail.com"


# =========================================================
# User-Agent rotator
# =========================================================
software_names = [
    SoftwareName.CHROME.value,
    SoftwareName.FIREFOX.value,
    SoftwareName.EDGE.value,
    SoftwareName.OPERA.value,
]
operating_systems = [
    OperatingSystem.WINDOWS.value,
    OperatingSystem.LINUX.value,
    OperatingSystem.MAC.value,
]
user_agent_rotator = UserAgent(
    software_names=software_names,
    operating_systems=operating_systems,
    limit=1200,
)

BASE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8,"
              "application/signed-exchange;v=b3;q=0.7",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9,hi;q=0.8,es;q=0.7,pt;q=0.6",
    "cache-control": "max-age=0",
    "origin": "https://telegram.org",
    "priority": "u=0, i",
    "referer": "https://telegram.org/support",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

TIME_OUT = 15
PROXIES_TYPES = ("http", "socks4", "socks5")
LOGS_FILE = "logs.txt"
ERRORS_FILE = "errors.txt"


# =========================================================
# Thread-safe state manager
# =========================================================
class StateManager:
    def __init__(self):
        self.lock = Lock()
        self.is_running = False
        self.stop_event = Event()
        self.target = ""
        self.success_count = 0
        self.error_count = 0
        self.start_time = 0
        self.logs = deque(maxlen=1000)   # recent logs memory-তে
        self.log_seq = 0                 # প্রতিটা log-এর incrementing id (SSE/polling-এর জন্য)

    # ---------- logging ----------
    def log(self, text):
        entry = {
            "id": 0,
            "timestamp": time(),
            "log": text,
        }
        with self.lock:
            self.log_seq += 1
            entry["id"] = self.log_seq
            self.logs.append(entry)
        # file-এও লিখে রাখি (rotation নেই — দরকার হলে নিজে handle করুন)
        try:
            with open(LOGS_FILE, "a", encoding="utf-8") as f:
                f.write(
                    f"[{datetime.fromtimestamp(entry['timestamp']).isoformat()}] {text}\n"
                )
        except Exception:
            pass

    # ---------- stats ----------
    def get_stats(self, since_id: int = 0):
        with self.lock:
            recent = [l for l in self.logs if l["id"] > since_id]
            return {
                "running": self.is_running,
                "target": self.target,
                "success_count": self.success_count,
                "error_count": self.error_count,
                "uptime_seconds": int(time() - self.start_time) if self.start_time else 0,
                "log_seq": self.log_seq,
                "recent_logs": recent,
            }

    def inc_success(self):
        with self.lock:
            self.success_count += 1

    def inc_error(self):
        with self.lock:
            self.error_count += 1

    def reset_for_start(self, target):
        with self.lock:
            if self.is_running:
                return False
            self.is_running = True
            self.stop_event.clear()
            self.target = target
            self.start_time = time()
            self.success_count = 0
            self.error_count = 0
            self.logs.clear()
            self.log_seq = 0
            return True

    def request_stop(self):
        with self.lock:
            if not self.is_running:
                return False
            self.is_running = False
            self.stop_event.set()
            return True


state = StateManager()


# =========================================================
# Helpers
# =========================================================
def generate_random_phone_number():
    while True:
        country_code = "+{}".format(random.randint(1, 999))
        national_number = str(random.randint(1000000000, 9999999999))
        phone_number_str = country_code + national_number
        try:
            phone_number = phonenumbers.parse(phone_number_str)
            if phonenumbers.is_valid_number(phone_number):
                return phonenumbers.format_number(
                    phone_number, PhoneNumberFormat.E164
                )
        except phonenumbers.phonenumberutil.NumberParseException:
            continue


def get_random_line(filename, username):
    try:
        with open(filename, "r", encoding="utf-8") as file:
            lines = file.readlines()
            if not lines:
                return f"Please investigate {username}"
            line = random.choice(lines).strip()
            return line.replace("{username}", username)
    except Exception:
        return f"Please investigate {username}"


def extract_csrf_token(html):
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", {"name": "csrf_token"})
    if token_input:
        return token_input.get("value", "")
    return None


# =========================================================
# Worker
# =========================================================
def control(proxy, proxy_type, username):
    if not state.is_running:
        return

    user_agent = user_agent_rotator.get_random_user_agent()
    headers = BASE_HEADERS.copy()
    headers["user-agent"] = user_agent
    url = "https://telegram.org/support"

    try:
        session = requests.Session()
        proxy_dict = {
            "http": f"{proxy_type}://{proxy}",
            "https": f"{proxy_type}://{proxy}",
        }

        # ---- GET form page ----
        response = session.get(
            url, proxies=proxy_dict, timeout=TIME_OUT, headers=headers
        )
        if response.status_code != 200:
            state.inc_error()
            return

        csrf_token = extract_csrf_token(response.text)
        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", action="/support")
        if not form:
            state.inc_error()
            return

        message = get_random_line("message.txt", username)
        email = generate("gmail")
        phone = generate_random_phone_number()

        # ---- build form data ----
        data = {}
        for input_tag in form.find_all(["input", "textarea"]):
            name_attr = input_tag.get("name")
            if not name_attr:
                continue
            if name_attr == "csrf_token" and csrf_token:
                data[name_attr] = csrf_token
            elif "email" in name_attr.lower():
                data[name_attr] = email
            elif "phone" in name_attr.lower():
                data[name_attr] = phone
            elif "problem" in name_attr.lower() or "message" in name_attr.lower():
                data[name_attr] = message
            else:
                data[name_attr] = input_tag.get("value", "")

        # textarea double-check
        textarea = form.find("textarea")
        if textarea and textarea.get("name"):
            data[textarea.get("name")] = message

        # ---- POST (requests নিজেই encode করবে) ----
        post_headers = headers.copy()
        post_headers.pop("content-length", None)  # requests নিজে সেট করবে
        post_headers["content-type"] = "application/x-www-form-urlencoded"

        response = session.post(
            url,
            data=data,
            headers=post_headers,
            timeout=TIME_OUT,
            allow_redirects=True,
        )

        if response.status_code == 200:
            state.inc_success()
            state.log(
                f"SUCCESS: Email: {email} | Phone: {phone} | Proxy: {proxy}"
            )
        else:
            state.inc_error()
            state.log(
                f"FAILED: Status: {response.status_code} | Proxy: {proxy}"
            )

    except Exception as e:
        state.inc_error()
        try:
            with open(ERRORS_FILE, "a+", encoding="utf-8") as err_file:
                err_file.write(f"{datetime.now().isoformat()} | {e}\n")
        except Exception:
            pass


def get_views_from_saved_proxies(proxy_type, proxies, username):
    for proxy in proxies:
        if not state.is_running or state.stop_event.is_set():
            break
        proxy = proxy.strip()
        if proxy:
            control(proxy, proxy_type, username)


def run_loop(target_username):
    state.log(f"Worker initiated for target: {target_username}")

    while state.is_running and not state.stop_event.is_set():
        threads = []
        for proxy_type in PROXIES_TYPES:
            if state.stop_event.is_set():
                break

            filename = f"{proxy_type}_proxies.txt"
            if not os.path.exists(filename):
                continue

            try:
                with open(filename, "r", encoding="utf-8") as f:
                    proxies = [p.strip() for p in f.readlines() if p.strip()]
            except Exception as e:
                state.log(f"Proxy file read error ({filename}): {e}")
                continue

            chunked = [proxies[i : i + 70] for i in range(0, len(proxies), 70)]
            for chunk in chunked:
                if state.stop_event.is_set():
                    break
                t = Thread(
                    target=get_views_from_saved_proxies,
                    args=(proxy_type, chunk, target_username),
                    daemon=True,
                )
                threads.append(t)
                t.start()

        # wait for this batch
        for t in threads:
            while t.is_alive():
                if state.stop_event.is_set():
                    break
                t.join(timeout=0.5)

        # if stop requested — break immediately
        if state.stop_event.is_set():
            break

        sleep(2)

    state.log("Worker loop completely halted.")


# =========================================================
# HTTP handler
# =========================================================
class handler(BaseHTTPRequestHandler):
    # --- helpers ---
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code, html):
        body = html.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # quiet default logging
        pass

    # --- OPTIONS ---
    def do_OPTIONS(self):
        self._send_json(200, {"status": "ok"})

    # --- GET ---
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        raw_link = params.get("link", [""])[0].strip()
        command = params.get("command", [""])[0].strip().lower()

        # username extraction
        username = raw_link
        if "t.me/" in username:
            username = username.split("t.me/")[-1].split("/")[0]
        username = username.lstrip("@")

        # ---------- /start ----------
        if command == "/start":
            if not username:
                return self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": "Missing or invalid 'link' parameter.",
                    },
                )

            if not state.reset_for_start(username):
                return self._send_json(
                    409,
                    {
                        "status": "error",
                        "message": f"Task already actively running for target: {state.target}",
                    },
                )

            Thread(target=run_loop, args=(username,), daemon=True).start()

            return self._send_json(
                200,
                {
                    "status": "success",
                    "message": f"Reporting task started for {username}",
                    "data": state.get_stats(),
                },
            )

        # ---------- /stop ----------
        elif command == "/stop":
            if not state.request_stop():
                return self._send_json(
                    200,
                    {
                        "status": "idle",
                        "message": "No active tasks are currently running.",
                    },
                )
            state.log("Received /stop command. Terminating task...")
            return self._send_json(
                200,
                {
                    "status": "success",
                    "message": "Reporting process stopping...",
                    "data": state.get_stats(),
                },
            )

        # ---------- /logs (JSON, supports ?since_id=N) ----------
        elif command == "/logs":
            # SSE stream mode
            if params.get("stream", ["0"])[0] == "1":
                return self._stream_logs()

            try:
                since_id = int(params.get("since_id", ["0"])[0])
            except ValueError:
                since_id = 0

            return self._send_json(
                200,
                {
                    "status": "success",
                    "data": state.get_stats(since_id=since_id),
                },
            )

        # ---------- / (HTML dashboard) ----------
        elif command == "" and parsed.path in ("/", "/dashboard"):
            return self._send_html(200, DASHBOARD_HTML)

        # ---------- invalid ----------
        else:
            return self._send_json(
                400,
                {
                    "status": "error",
                    "message": "Invalid or missing 'command' parameter. "
                               "Supported commands: /start, /stop, /logs",
                    "usage": "/api?link=https://t.me/example&command=/start",
                },
            )

    # --- SSE stream ---
    def _stream_logs(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # nginx-এর জন্য
        self._cors_headers()
        self.end_headers()

        last_seq = 0
        try:
            while True:
                stats = state.get_stats(since_id=last_seq)
                payload = json.dumps(stats)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()

                if stats["recent_logs"]:
                    last_seq = stats["recent_logs"][-1]["id"]

                # stop চাইলে stream বন্ধ
                if not stats["running"] and stats["log_seq"] == last_seq:
                    # শেষ update পাঠিয়ে break
                    break

                sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# =========================================================
# Dashboard (auto-refresh via SSE + polling fallback)
# =========================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Telegram Reporter — Live</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px; background: #0d1117; color: #c9d1d9;
    font-family: ui-monospace, Menlo, Consolas, monospace;
  }
  h1 { color: #58a6ff; font-size: 20px; margin: 0 0 16px; }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 16px; min-width: 130px;
  }
  .card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; }
  .card .value { font-size: 20px; font-weight: 700; margin-top: 4px; }
  .green { color: #3fb950; }
  .red { color: #f85149; }
  .blue { color: #58a6ff; }
  .gray { color: #8b949e; }
  #logs {
    background: #010409; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px; height: 60vh; overflow-y: auto; white-space: pre-wrap;
    font-size: 12px; line-height: 1.5;
  }
  .row-SUCCESS { color: #3fb950; }
  .row-FAILED  { color: #f85149; }
  .row-other   { color: #c9d1d9; }
  .status-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 6px; vertical-align: middle;
  }
  .dot-on  { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  .dot-off { background: #484f58; }
  .controls { margin: 12px 0; display: flex; gap: 8px; align-items: center; }
  input {
    background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
    padding: 8px 12px; border-radius: 6px; font-family: inherit; min-width: 260px;
  }
  button {
    background: #238636; border: none; color: #fff; padding: 8px 16px;
    border-radius: 6px; cursor: pointer; font-family: inherit; font-weight: 600;
  }
  button.stop { background: #da3633; }
  button:hover { opacity: 0.9; }
</style>
</head>
<body>

<h1><span id="statusDot" class="status-dot dot-off"></span>
    Telegram Reporter — Live Dashboard</h1>

<div class="controls">
  <input id="target" placeholder="https://t.me/username" value=""/>
  <button id="startBtn">Start</button>
  <button id="stopBtn" class="stop">Stop</button>
  <span id="connState" class="gray" style="font-size:12px;">connecting…</span>
</div>

<div class="cards">
  <div class="card"><div class="label">Target</div>
    <div id="vTarget" class="value blue">—</div></div>
  <div class="card"><div class="label">Success</div>
    <div id="vSuccess" class="value green">0</div></div>
  <div class="card"><div class="label">Errors</div>
    <div id="vErrors" class="value red">0</div></div>
  <div class="card"><div class="label">Uptime</div>
    <div id="vUptime" class="value">0s</div></div>
</div>

<div id="logs">waiting for logs…</div>

<script>
const $ = (id) => document.getElementById(id);
const logsEl = $('logs');
let lastSeq = 0;
let autoScroll = true;
logsEl.addEventListener('scroll', () => {
  autoScroll = (logsEl.scrollTop + logsEl.clientHeight >= logsEl.scrollHeight - 20);
});

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}
function fmtUptime(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

function applyStats(d) {
  $('vTarget').textContent = d.target || '—';
  $('vSuccess').textContent = d.success_count;
  $('vErrors').textContent = d.error_count;
  $('vUptime').textContent = fmtUptime(d.uptime_seconds);

  const dot = $('statusDot');
  if (d.running) {
    dot.className = 'status-dot dot-on';
  } else {
    dot.className = 'status-dot dot-off';
  }

  if (d.recent_logs && d.recent_logs.length) {
    for (const entry of d.recent_logs) {
      if (entry.id <= lastSeq) continue;
      lastSeq = entry.id;

      const div = document.createElement('div');
      let cls = 'row-other';
      if (entry.log.startsWith('SUCCESS')) cls = 'row-SUCCESS';
      else if (entry.log.startsWith('FAILED')) cls = 'row-FAILED';
      div.className = cls;
      div.textContent = `[${fmtTime(entry.timestamp)}] ${entry.log}`;
      logsEl.appendChild(div);
    }
    if (autoScroll) logsEl.scrollTop = logsEl.scrollHeight;
  }
}

/* ---------------- SSE (primary) ---------------- */
let sse = null;
function connectSSE() {
  if (sse) try { sse.close(); } catch(e){}
  $('connState').textContent = 'connecting…';
  sse = new EventSource('/?command=/logs&stream=1');

  sse.onopen = () => { $('connState').textContent = 'live (SSE)'; };
  sse.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      applyStats(data);
    } catch(e) { console.error(e); }
  };
  sse.onerror = () => {
    $('connState').textContent = 'SSE lost — retrying…';
    try { sse.close(); } catch(e){}
    setTimeout(connectSSE, 3000);
  };
}

/* ---------------- Polling fallback ---------------- */
let pollTimer = null;
async function pollOnce() {
  try {
    const res = await fetch(`/?command=/logs&since_id=${lastSeq}`);
    const json = await res.json();
    if (json.status === 'success') applyStats(json.data);
    $('connState').textContent = 'live (polling)';
  } catch(e) {
    $('connState').textContent = 'polling failed';
  }
}
function startPolling() {
  if (pollTimer) return;
  pollOnce();
  pollTimer = setInterval(pollOnce, 2000);
}

/* ---------------- Start / Stop ---------------- */
async function startTask() {
  const link = $('target').value.trim();
  if (!link) { alert('Enter a t.me link'); return; }
  const url = `/?link=${encodeURIComponent(link)}&command=/start`;
  const res = await fetch(url);
  const json = await res.json();
  if (json.status !== 'success') alert(json.message || 'Failed');
  else {
    logsEl.innerHTML = '';
    lastSeq = 0;
  }
}
async function stopTask() {
  await fetch('/?command=/stop');
}

$('startBtn').onclick = startTask;
$('stopBtn').onclick = stopTask;

/* ---------------- boot ---------------- */
connectSSE();
/* polling fallback only if SSE never opens */
setTimeout(() => {
  if ($('connState').textContent.includes('connecting')) {
    try { sse.close(); } catch(e){}
    startPolling();
  }
}, 5000);
</script>
</body>
</html>
"""


# =========================================================
# Main entry
# =========================================================
def _shutdown(signum, frame):
    print("\nShutting down…")
    state.request_stop()
    sleep(0.5)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    PORT = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"Server running on http://localhost:{PORT}")
    print(f"  • Dashboard  : http://localhost:{PORT}/")
    print(f"  • Start      : http://localhost:{PORT}/?link=https://t.me/username&command=/start")
    print(f"  • Logs (JSON): http://localhost:{PORT}/?command=/logs")
    print(f"  • Logs (SSE) : http://localhost:{PORT}/?command=/logs&stream=1")
    print(f"  • Stop       : http://localhost:{PORT}/?command=/stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.request_stop()
        server.server_close()
        print("Server stopped.")
