import os
import json
import random
import time
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread, Lock, Event
from collections import deque
import requests
from bs4 import BeautifulSoup
import phonenumbers
from phonenumbers import PhoneNumberFormat
from random_user_agent.user_agent import UserAgent
from random_user_agent.params import SoftwareName, OperatingSystem

try:
    from emailtools import generate
except ImportError:
    def generate(_domain="gmail"):
        return f"user{random.randint(10000, 999999)}@gmail.com"

# ফোল্ডার ডিরেক্টরি ঠিক করা (যাতে প্রজেক্ট রুট থেকে ফাইল রিড করতে পারে)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class TelegramState:
    def __init__(self):
        self.lock = Lock()
        self.is_running = False
        self.stop_event = Event()
        self.target = ""
        self.success_count = 0
        self.error_count = 0
        self.start_time = 0
        self.logs = deque(maxlen=500)
        self.log_seq = 0

    def log(self, text):
        with self.lock:
            self.log_seq += 1
            self.logs.append({"id": self.log_seq, "timestamp": time.time(), "log": text})

    def get_stats(self, since_id=0):
        with self.lock:
            return {
                "running": self.is_running,
                "target": self.target,
                "success_count": self.success_count,
                "error_count": self.error_count,
                "uptime_seconds": int(time.time() - self.start_time) if self.start_time else 0,
                "log_seq": self.log_seq,
                "recent_logs": [l for l in self.logs if l["id"] > since_id]
            }

    def inc_success(self):
        with self.lock: self.success_count += 1

    def inc_error(self):
        with self.lock: self.error_count += 1

    def reset_for_start(self, target):
        with self.lock:
            if self.is_running: return False
            self.is_running = True
            self.stop_event.clear()
            self.target = target
            self.start_time = time.time()
            self.success_count = 0
            self.error_count = 0
            self.logs.clear()
            return True

    def request_stop(self):
        with self.lock:
            if not self.is_running: return False
            self.is_running = False
            self.stop_event.set()
            return True

state = TelegramState()

software_names = [SoftwareName.CHROME.value, SoftwareName.FIREFOX.value, SoftwareName.EDGE.value]
operating_systems = [OperatingSystem.WINDOWS.value, OperatingSystem.LINUX.value, OperatingSystem.MAC.value]
user_agent_rotator = UserAgent(software_names=software_names, operating_systems=operating_systems, limit=500)

BASE_HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'content-type': 'application/x-www-form-urlencoded',
    'origin': 'https://telegram.org',
    'referer': 'https://telegram.org/support'
}
TIME_OUT = 10
PROXIES_TYPES = ('http', 'socks4', 'socks5')

def load_proxies(proxy_type):
    filename = os.path.join(BASE_DIR, f"{proxy_type}_proxies.txt")
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return [p.strip() for p in f.readlines() if p.strip()]
    except:
        return []

def extract_csrf_token(html):
    soup = BeautifulSoup(html, 'html.parser')
    token_input = soup.find('input', {'name': 'csrf_token'})
    return token_input.get('value', '') if token_input else None

def send_report(proxy, proxy_type, username):
    if not state.is_running or state.stop_event.is_set():
        return

    user_agent = user_agent_rotator.get_random_user_agent()
    headers = BASE_HEADERS.copy()
    headers['user-agent'] = user_agent
    url = 'https://telegram.org/support'
    
    try:
        session = requests.Session()
        proxy_dict = {'http': f'{proxy_type}://{proxy}', 'https': f'{proxy_type}://{proxy}'}
        
        response = session.get(url, proxies=proxy_dict, timeout=TIME_OUT, headers=headers)
        if response.status_code != 200: 
            state.inc_error()
            return
        
        csrf_token = extract_csrf_token(response.text)
        soup = BeautifulSoup(response.text, 'html.parser')
        form = soup.find('form', action="/support")
        if not form: 
            state.inc_error()
            return

        try:
            msg_file = os.path.join(BASE_DIR, 'message.txt')
            with open(msg_file, 'r', encoding='utf-8') as f:
                message = random.choice(f.readlines()).strip().replace('{username}', username)
        except:
            message = f"Please investigate {username}"

        email = generate('gmail')
        phone = f"+{random.randint(1,99)}{random.randint(100000000,999999999)}"

        data = {}
        for input_tag in form.find_all(['input', 'textarea']):
            name_attr = input_tag.get('name')
            if not name_attr: continue
            if name_attr == 'csrf_token': data[name_attr] = csrf_token
            elif 'email' in name_attr.lower(): data[name_attr] = email
            elif 'phone' in name_attr.lower(): data[name_attr] = phone
            elif 'problem' in name_attr.lower() or 'message' in name_attr.lower(): data[name_attr] = message
            else: data[name_attr] = input_tag.get('value', '')

        response = session.post(url, data=data, headers=headers, timeout=TIME_OUT)
        if response.status_code == 200:
            state.inc_success()
            state.log(f"SUCCESS: {email} | Proxy: {proxy}")
        else:
            state.inc_error()
            state.log(f"FAILED: Status {response.status_code} | Proxy: {proxy}")
    except Exception as e:
        state.inc_error()
        state.log(f"ERROR: {str(e)[:40]} | Proxy: {proxy}")

def get_views_from_proxies(proxy_type, proxies, username):
    for proxy in proxies:
        if not state.is_running or state.stop_event.is_set():
            break
        proxy = proxy.strip()
        if proxy:
            send_report(proxy, proxy_type, username)

def worker_loop(target_username):
    state.log(f"Worker initiated for target: {target_username}")

    while state.is_running and not state.stop_event.is_set():
        threads = []
        for proxy_type in PROXIES_TYPES:
            if state.stop_event.is_set():
                break

            proxies = load_proxies(proxy_type)
            if not proxies:
                continue

            chunked = [proxies[i:i + 70] for i in range(0, len(proxies), 70)]
            for chunk in chunked:
                if state.stop_event.is_set():
                    break
                t = Thread(target=get_views_from_proxies, args=(proxy_type, chunk, target_username), daemon=True)
                threads.append(t)
                t.start()

        for t in threads:
            while t.is_alive():
                if state.stop_event.is_set():
                    break
                t.join(timeout=0.5)

        if state.stop_event.is_set():
            break
        time.sleep(2)

    state.log("Worker loop completely halted.")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Telegram Reporter — Live Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Inter', sans-serif; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #e2e8f0; min-height: 100vh; padding: 20px; }
  .container { max-width: 1400px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding: 20px 24px; background: rgba(30, 41, 59, 0.6); backdrop-filter: blur(12px); border-radius: 16px; border: 1px solid rgba(148, 163, 184, 0.1); }
  .logo { display: flex; align-items: center; gap: 12px; }
  .logo-icon { width: 42px; height: 42px; border-radius: 12px; background: linear-gradient(135deg, #3b82f6, #8b5cf6); display: flex; align-items: center; justify-content: center; font-size: 20px; box-shadow: 0 4px 20px rgba(59, 130, 246, 0.4); }
  .logo-text { font-size: 20px; font-weight: 700; background: linear-gradient(135deg, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .logo-sub { font-size: 11px; color: #64748b; font-weight: 500; letter-spacing: 1px; }
  .status-badge { display: flex; align-items: center; gap: 8px; padding: 8px 16px; background: rgba(15, 23, 42, 0.6); border-radius: 999px; border: 1px solid rgba(148, 163, 184, 0.2); font-size: 13px; font-weight: 500; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #64748b; transition: all 0.3s; }
  .status-dot.active { background: #10b981; box-shadow: 0 0 12px #10b981; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .controls { background: rgba(30, 41, 59, 0.6); backdrop-filter: blur(12px); border-radius: 16px; padding: 20px 24px; margin-bottom: 20px; border: 1px solid rgba(148, 163, 184, 0.1); display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  .input-group { flex: 1; min-width: 280px; position: relative; }
  .input-group input { width: 100%; padding: 12px 16px 12px 44px; background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(148, 163, 184, 0.2); border-radius: 10px; color: #e2e8f0; font-size: 14px; font-family: inherit; transition: all 0.2s; }
  .input-group input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1); }
  .input-group::before { content: "@"; position: absolute; left: 16px; top: 50%; transform: translateY(-50%); color: #64748b; font-weight: 600; }
  button { padding: 12px 24px; border: none; border-radius: 10px; font-family: inherit; font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 8px; }
  .btn-start { background: linear-gradient(135deg, #10b981, #059669); color: white; box-shadow: 0 4px 16px rgba(16, 185, 129, 0.3); }
  .btn-start:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(16, 185, 129, 0.4); }
  .btn-stop { background: linear-gradient(135deg, #ef4444, #dc2626); color: white; box-shadow: 0 4px 16px rgba(239, 68, 68, 0.3); }
  .btn-stop:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(239, 68, 68, 0.4); }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }
  .stat-card { background: rgba(30, 41, 59, 0.6); backdrop-filter: blur(12px); border-radius: 16px; padding: 20px; border: 1px solid rgba(148, 163, 184, 0.1); position: relative; overflow: hidden; transition: transform 0.2s; }
  .stat-card:hover { transform: translateY(-2px); }
  .stat-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: linear-gradient(90deg, transparent, var(--accent), transparent); }
  .stat-card.target { --accent: #3b82f6; }
  .stat-card.success { --accent: #10b981; }
  .stat-card.error { --accent: #ef4444; }
  .stat-card.uptime { --accent: #8b5cf6; }
  .stat-label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin-bottom: 8px; }
  .stat-value { font-size: 28px; font-weight: 700; color: #f1f5f9; }
  .stat-value.target { color: #60a5fa; }
  .stat-value.success { color: #34d399; }
  .stat-value.error { color: #f87171; }
  .stat-value.uptime { color: #a78bfa; }
  .logs-container { background: rgba(30, 41, 59, 0.6); backdrop-filter: blur(12px); border-radius: 16px; border: 1px solid rgba(148, 163, 184, 0.1); overflow: hidden; }
  .logs-header { padding: 16px 20px; border-bottom: 1px solid rgba(148, 163, 184, 0.1); display: flex; justify-content: space-between; align-items: center; background: rgba(15, 23, 42, 0.4); }
  .logs-title { font-size: 14px; font-weight: 600; color: #cbd5e1; display: flex; align-items: center; gap: 8px; }
  .logs-count { font-size: 11px; padding: 2px 8px; background: rgba(59, 130, 246, 0.2); color: #60a5fa; border-radius: 999px; font-weight: 600; }
  .logs-body { height: 60vh; overflow-y: auto; padding: 12px 16px; font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 12.5px; line-height: 1.7; }
  .logs-body::-webkit-scrollbar { width: 8px; }
  .logs-body::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.5); }
  .logs-body::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.3); border-radius: 4px; }
  .log-entry { padding: 3px 0; display: flex; gap: 10px; }
  .log-time { color: #64748b; flex-shrink: 0; }
  .log-text { word-break: break-all; }
  .log-text.success { color: #34d399; }
  .log-text.failed { color: #f87171; }
  .log-text.error { color: #fb923c; }
  .log-text.info { color: #94a3b8; }
  .empty-state { text-align: center; padding: 60px 20px; color: #64748b; font-style: italic; }
  @media (max-width: 768px) { .header { padding: 16px; } .controls { padding: 16px; } .stats-grid { grid-template-columns: repeat(2, 1fr); } .stat-value { font-size: 22px; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">
      <div class="logo-icon">⚡</div>
      <div>
        <div class="logo-text">Telegram Reporter</div>
        <div class="logo-sub">LIVE DASHBOARD</div>
      </div>
    </div>
    <div class="status-badge">
      <div class="status-dot" id="statusDot"></div>
      <span id="connState">Connecting...</span>
    </div>
  </div>
  
  <div class="controls">
    <div class="input-group">
      <input id="target" placeholder="username or https://t.me/username" />
    </div>
    <button class="btn-start" id="startBtn">▶ Start</button>
    <button class="btn-stop" id="stopBtn">■ Stop</button>
  </div>
  
  <div class="stats-grid">
    <div class="stat-card target">
      <div class="stat-label">🎯 Target</div>
      <div class="stat-value target" id="vTarget">—</div>
    </div>
    <div class="stat-card success">
      <div class="stat-label">✓ Success</div>
      <div class="stat-value success" id="vSuccess">0</div>
    </div>
    <div class="stat-card error">
      <div class="stat-label">✗ Errors</div>
      <div class="stat-value error" id="vErrors">0</div>
    </div>
    <div class="stat-card uptime">
      <div class="stat-label">⏱ Uptime</div>
      <div class="stat-value uptime" id="vUptime">0s</div>
    </div>
  </div>
  
  <div class="logs-container">
    <div class="logs-header">
      <div class="logs-title">
        📋 Live Logs
        <span class="logs-count" id="logsCount">0</span>
      </div>
    </div>
    <div class="logs-body" id="logs">
      <div class="empty-state">Waiting for activity...</div>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const logsEl = $('logs');
let lastSeq = 0, autoScroll = true;

logsEl.addEventListener('scroll', () => {
  autoScroll = (logsEl.scrollTop + logsEl.clientHeight >= logsEl.scrollHeight - 20);
});

function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString('en-US', { hour12: false }); }
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
  if (d.running) dot.classList.add('active'); else dot.classList.remove('active');
  
  if (d.recent_logs && d.recent_logs.length) {
    if (logsEl.querySelector('.empty-state')) logsEl.innerHTML = '';
    for (const entry of d.recent_logs) {
      if (entry.id <= lastSeq) continue;
      lastSeq = entry.id;
      const div = document.createElement('div');
      div.className = 'log-entry';
      let cls = 'info';
      if (entry.log.startsWith('SUCCESS')) cls = 'success';
      else if (entry.log.startsWith('FAILED')) cls = 'failed';
      else if (entry.log.startsWith('ERROR')) cls = 'error';
      div.innerHTML = `<span class="log-time">[${fmtTime(entry.timestamp)}]</span><span class="log-text ${cls}">${entry.log}</span>`;
      logsEl.appendChild(div);
    }
    $('logsCount').textContent = lastSeq;
    if (autoScroll) logsEl.scrollTop = logsEl.scrollHeight;
  }
}

let sse;
function connectSSE() {
  sse = new EventSource('/?command=/logs&stream=1');
  sse.onopen = () => { $('connState').textContent = 'Live'; $('statusDot').classList.add('active'); };
  sse.onmessage = (ev) => { try { applyStats(JSON.parse(ev.data)); } catch(e) {} };
  sse.onerror = () => { $('connState').textContent = 'Reconnecting...'; try { sse.close(); } catch(e) {}; setTimeout(connectSSE, 3000); };
}
connectSSE();

$('startBtn').onclick = async () => {
  const link = $('target').value.trim();
  if (!link) return alert('Please enter a username or link');
  const res = await fetch(`/?link=${encodeURIComponent(link)}&command=/start`);
  const json = await res.json();
  if (json.status !== 'success') alert(json.message || 'Failed to start');
  else { logsEl.innerHTML = ''; lastSeq = 0; }
};
$('stopBtn').onclick = async () => { await fetch('/?command=/stop'); };
$('target').addEventListener('keypress', (e) => { if (e.key === 'Enter') $('startBtn').click(); });
</script>
</body>
</html>
"""

class handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code, html):
        body = html.encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        command = params.get("command", [""])[0].lower()
        raw_link = params.get("link", [""])[0].strip()
        username = raw_link.split('t.me/')[-1].split('/')[0].lstrip('@') if 't.me/' in raw_link else raw_link.lstrip('@')

        if command == "/start":
            if not username:
                return self._send_json(400, {"status": "error", "message": "Missing link"})
            if not state.reset_for_start(username):
                return self._send_json(409, {"status": "error", "message": f"Task already running for {state.target}"})
            
            Thread(target=worker_loop, args=(username,), daemon=True).start()
            return self._send_json(200, {"status": "success", "message": f"Started for {username}"})

        elif command == "/stop":
            if not state.request_stop():
                return self._send_json(200, {"status": "idle", "message": "Not running"})
            state.log("Stopping...")
            return self._send_json(200, {"status": "success", "message": "Stopping"})

        elif command == "/logs":
            if params.get("stream", ["0"])[0] == "1":
                return self._stream_logs()
            since_id = int(params.get("since_id", ["0"])[0])
            return self._send_json(200, {"status": "success", "data": state.get_stats(since_id)})
        
        elif command == "" and parsed.path in ("/", "/dashboard"):
            return self._send_html(200, DASHBOARD_HTML)

        return self._send_json(400, {"status": "error", "message": "Invalid Command"})

    def _stream_logs(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        
        last_seq = 0
        try:
            while True:
                stats = state.get_stats(since_id=last_seq)
                self.wfile.write(f"data: {json.dumps(stats)}\n\n".encode('utf-8'))
                self.wfile.flush()
                if stats["recent_logs"]:
                    last_seq = stats["recent_logs"][-1]["id"]
                if not stats["running"] and stats["log_seq"] == last_seq: break
                time.sleep(1)
        except:
            pass

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"🚀 Server running on port {PORT}")
    server.serve_forever()
