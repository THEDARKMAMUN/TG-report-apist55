import os
import json
import random
import requests
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler
from bs4 import BeautifulSoup
import phonenumbers
from phonenumbers import PhoneNumberFormat
from random_user_agent.user_agent import UserAgent
from random_user_agent.params import SoftwareName, OperatingSystem
from upstash_redis import Redis

try:
    from emailtools import generate
except ImportError:
    def generate(_domain="gmail"):
        return f"user{random.randint(10000, 999999)}@gmail.com"

# ==========================================
# আপনার দেওয়া টোকেন এবং URL এখানে সেট করা হলো
# ==========================================
UPSTASH_URL = "https://climbing-doe-187467.upstash.io"
UPSTASH_TOKEN = "gQAAAAAAAtxLAAIgcDFiZjRmNzlkNmEyNDU0MDRmOTFmNzEwZTYzZDgyOGVkMQ"

# Vercel Env Vars থাকলে সেটা ব্যবহার করবে, না থাকলে উপরের ডিফল্টটি ব্যবহার করবে
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", UPSTASH_URL)
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)

redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

# Proxy and User-Agent setup
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

def extract_csrf_token(html):
    soup = BeautifulSoup(html, 'html.parser')
    token_input = soup.find('input', {'name': 'csrf_token'})
    return token_input.get('value', '') if token_input else None

def process_single_proxy(proxy, proxy_type, username):
    """প্রতিবার Cron চললে এটি ১টি প্রক্সি দিয়ে ট্রাই করবে"""
    user_agent = user_agent_rotator.get_random_user_agent()
    headers = BASE_HEADERS.copy()
    headers['user-agent'] = user_agent
    url = 'https://telegram.org/support'
    
    try:
        session = requests.Session()
        proxy_dict = {'http': f'{proxy_type}://{proxy}', 'https': f'{proxy_type}://{proxy}'}
        
        response = session.get(url, proxies=proxy_dict, timeout=TIME_OUT, headers=headers)
        if response.status_code != 200: return False
        
        csrf_token = extract_csrf_token(response.text)
        soup = BeautifulSoup(response.text, 'html.parser')
        form = soup.find('form', action="/support")
        if not form: return False

        # Message & Email Generate
        try:
            with open('message.txt', 'r', encoding='utf-8') as f:
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
            redis.incr('success_count')
            return True
    except:
        pass
    return False

class handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        command = params.get('command', [''])[0].strip().lower()
        raw_link = params.get('link', [''])[0].strip()
        
        username = raw_link.split('t.me/')[-1].split('/')[0].lstrip('@') if 't.me/' in raw_link else raw_link.lstrip('@')

        # 1. START COMMAND
        if command == '/start':
            if not username: return self._send_json(400, {"error": "Missing link"})
            redis.set('is_running', '1')
            redis.set('target', username)
            redis.set('success_count', 0)
            redis.set('error_count', 0)
            return self._send_json(200, {"status": "success", "message": f"Task started for {username}"})

        # 2. STOP COMMAND
        elif command == '/stop':
            redis.set('is_running', '0')
            return self._send_json(200, {"status": "success", "message": "Stopped"})

        # 3. LOGS COMMAND
        elif command == '/logs':
            is_running = redis.get('is_running') == '1'
            return self._send_json(200, {
                "status": "success",
                "data": {
                    "running": is_running,
                    "target": redis.get('target') or "",
                    "success_count": int(redis.get('success_count') or 0),
                    "error_count": int(redis.get('error_count') or 0),
                }
            })

        # 4. CRON TICK (Vercel Cron automatically calls this every minute)
        elif command == '/cron_tick':
            if redis.get('is_running') != '1':
                return self._send_json(200, {"status": "idle"})
            
            target = redis.get('target')
            p_type = random.choice(['http', 'socks4', 'socks5'])
            try:
                with open(f'{p_type}_proxies.txt', 'r') as f:
                    proxies = [p.strip() for p in f.readlines() if p.strip()]
                if proxies:
                    proxy = random.choice(proxies)
                    success = process_single_proxy(proxy, p_type, target)
                    if not success:
                        redis.incr('error_count')
            except Exception as e:
                pass
            
            return self._send_json(200, {"status": "tick_processed"})
        
        return self._send_json(400, {"error": "Invalid command"})
