"""
Web Scraper Server - Multi-site version
Chạy auto-refresh 24/7 với thông tin từ environment variables
Hỗ trợ nhiều site cùng lúc

Kiến trúc chính:
- ServerScraper: quản lý toàn bộ vòng đời scraping (login -> dashboard -> đọc campaign -> báo Telegram)
- Mỗi site được scrape trong 1 thread riêng (tránh conflict với asyncio loop của PaaS)
- Có health-check HTTP server để PaaS (Railway/Heroku) biết app còn sống
- Tự dọn cache khi lỗi transient, tự reset định kỳ, ghi file atomic, log thread-safe
"""

import os
import sys
import time
import json
import random
import signal
import logging
import threading
import traceback
import shutil
import tempfile
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Load environment variables from .env file
load_dotenv()


# ============================== CONFIG / CONSTANTS ==============================

# --- File paths ---
CREDENTIALS_FILE = 'saved_credentials.json'
COOKIES_DIR = 'saved_cookies'
TELEGRAM_STATE_FILE = 'telegram_state.json'
OUTPUT_FILE = 'server_output.txt'
OUTPUT_MAX_BYTES = 200 * 1024  # Giới hạn 200KB cho output file (tránh phình vô hạn)

# --- Timing ---
DEFAULT_REFRESH_MINUTES = 2
MAX_RETRIES = 3               # Số lần retry tối đa khi gặp lỗi transient
RESET_AFTER_ITERATIONS = 5    # Reset cache định kỳ sau N lần quét
THREAD_JOIN_TIMEOUT = 300     # Timeout tối đa cho 1 lần scrape (giây)
LOGIN_TIMEOUT_MS = 60_000     # Timeout khi goto login page
DASHBOARD_TIMEOUT_MS = 90_000 # Timeout khi goto dashboard
CLICK_TIMEOUT_MS = 15_000     # Timeout cho click submit login
TELEGRAM_API_TIMEOUT = 10     # Timeout cho request tới Telegram API

# --- Health check ---
HEALTH_SERVER_ENABLED = os.getenv('HEALTH_SERVER_ENABLED', 'true').lower() == 'true'

# --- Browser hardening ---
BROWSER_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-web-security',
    '--disable-features=VizDisplayCompositor',
]

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
]

# Anti-bot init script (đưa ra ngoài để dễ bảo trì)
ANTI_BOT_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['vi-VN', 'vi', 'en-US', 'en']
    });
    window.chrome = {
        runtime: {}
    };
"""

# --- Login selectors (tách ra để dễ cập nhật khi site đổi giao diện) ---
EMAIL_SELECTORS = [
    'input[name="username"]',
    'input#username',
    'input#signin-username',
    'input[type="email"]',
    'input[name="email"]',
    'input[placeholder*="email"]',
    'input[placeholder*="Email"]',
    'input[placeholder*="username"]',
    'input[placeholder*="Username"]',
    'input[placeholder*="Tên đăng nhập"]',
    'input[placeholder*="địa chỉ email"]',
]

PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input#password',
    'input#signin-password',
    'input[type="password"]',
    'input[placeholder*="password"]',
    'input[placeholder*="Password"]',
    'input[placeholder*="Mật khẩu"]',
]

SUBMIT_SELECTORS = [
    'button#invisibleCaptchaSignin',
    'button[type="submit"]',
    'button:has-text("Sign In")',
    'button:has-text("Đăng nhập")',
    'button:has-text("Đăng Nhập Ngay")',
    'button:has-text("Login")',
    'input[type="submit"]',
]

# Keywords phát hiện trang login
LOGIN_KEYWORDS = ['sign in', 'login', 'email', 'password', 'username', 'đăng nhập', 'đăng nhập ngay']

# --- Logger config (thread-safe qua logging module) ---
def _configure_utf8_stdio():
    """
    Ép stdout/stderr dùng UTF-8 để emoji trong log không gây UnicodeEncodeError.
    Trên Windows console (cp1252/cp437), in emoji có thể crash app -> cần reconfigure.
    An toàn khi chạy trên Linux/PaaS (đã là UTF-8).
    """
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001 - không critical, bỏ qua
            pass


_configure_utf8_stdio()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger('kiemgao')



# ============================== HEALTH CHECK SERVER ==============================

class _HealthHandler(BaseHTTPRequestHandler):
    """Handler tối giản trả 200 OK cho GET/HEAD (dùng cho PaaS health-check)."""

    def log_message(self, fmt, *args):  # noqa: A002 - theo signature stdlib
        return  # tắt log mặc định của http.server

    def _respond(self):
        body = b'OK'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_HEAD(self):
        self._respond()


def start_health_server():
    """Khởi động HTTP server nền để PaaS health-check (chỉ khi có PORT)."""
    if not HEALTH_SERVER_ENABLED:
        logger.info("🩺 Health server: DISABLED (HEALTH_SERVER_ENABLED=false)")
        return None

    port_env = os.getenv('PORT')
    if not port_env:
        logger.info("🩺 Health server: bỏ qua vì không có biến PORT (chạy local?)")
        return None

    try:
        port = int(port_env)
    except ValueError:
        logger.warning(f"⚠️  PORT='{port_env}' không hợp lệ - bỏ qua health server")
        return None

    def _serve():
        try:
            httpd = ThreadingHTTPServer(('0.0.0.0', port), _HealthHandler)
            logger.info(f"🩺 Health server đang chạy tại http://0.0.0.0:{port} (GET /)")
            httpd.serve_forever()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️  Không thể chạy health server: {e}")

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t



# ============================== SCRAPER ==============================

class ServerScraper:
    def __init__(self):
        # --- Cấu hình site 1 (bắt buộc có credentials) ---
        self.url1 = os.getenv('SCRAPER_URL', 'https://link999.app/auth/signin')
        self.username1 = os.getenv('SCRAPER_USERNAME')
        self.password1 = os.getenv('SCRAPER_PASSWORD')

        # --- Cấu hình site 2 (tùy chọn) ---
        self.url2 = os.getenv('SCRAPER_URL2', 'https://link999.win/auth/signin')
        self.username2 = os.getenv('SCRAPER_USERNAME2')
        self.password2 = os.getenv('SCRAPER_PASSWORD2')

        # Validate credentials: KHÔNG hardcode mật khẩu trong code
        self._validate_credentials()

        # Ưu tiên REFRESH_INTERVAL (giây), nếu không có thì dùng SCRAPER_REFRESH_MINUTES (phút)
        refresh_interval_env = os.getenv('REFRESH_INTERVAL')
        if refresh_interval_env:
            self.refresh_interval = int(refresh_interval_env)
        else:
            self.refresh_interval = int(
                os.getenv('SCRAPER_REFRESH_MINUTES', str(DEFAULT_REFRESH_MINUTES))
            ) * 60

        self.headless = os.getenv('SCRAPER_HEADLESS', 'true').lower() == 'true'
        self.cleanup_on_startup = os.getenv('CLEANUP_ON_STARTUP', 'true').lower() == 'true'
        self.reset_after_iterations = RESET_AFTER_ITERATIONS

        # Danh sách pattern lỗi transient để retry + clear cache
        self.retry_patterns = [
            'timeout', 'login', 'element',
            'connection', 'refused', 'reset', 'dns', 'network',
            'closed', 'crash', 'cdp', 'playwright', 'browser',
            'err_', 'executable', 'sandbox', 'process',
            'net::', 'protocol', 'session',
        ]

        # Telegram config
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        self.last_telegram_message_id = None  # ID tin nhắn cuối cùng để xóa
        self.site1_campaign_info = []
        self.site2_campaign_info = []

        # Lock bảo vệ cleanup/cache (tránh race condition giữa 2 thread site)
        self._cleanup_lock = threading.Lock()
        # Lock bảo vệ ghi file state
        self._state_lock = threading.RLock()
        # Cờ dừng để graceful shutdown
        self._stop_event = threading.Event()

        # Tạo thư mục cookies
        os.makedirs(COOKIES_DIR, exist_ok=True)

        # Auto cleanup on startup để xóa sạch cache từ phiên deploy cũ
        if self.cleanup_on_startup:
            self.log("🧹 Auto cleanup on startup: Đang dọn dẹp cache từ phiên deploy cũ...")
            self.cleanup_on_startup_safe()

        # Load telegram state từ file
        self.load_telegram_state()

    # ------------------------------ Helpers ------------------------------

    def _validate_credentials(self):
        """Đảm bảo credentials được cung cấp qua env, không hardcode trong code."""
        missing = []
        if not self.username1 or not self.password1:
            missing.append('SCRAPER_USERNAME/SCRAPER_PASSWORD')
        # Site 2 là tùy chọn: chỉ cảnh báo nếu thiếu 1 trong 2
        if bool(self.username2) ^ bool(self.password2):
            self.log("⚠️  Site 2: chỉ có 1 trong 2 biến USERNAME2/PASSWORD2 - sẽ bỏ qua site 2")

        if missing:
            raise ValueError(
                "Thiếu biến môi trường bắt buộc: " + ", ".join(missing) +
                ". Hãy cấu hình trong .env hoặc environment variables của PaaS."
            )

    def log(self, message, end='\n'):
        """Log với timestamp (thread-safe qua logging module).

        Không bao giờ raise lỗi encoding: nếu console không in được ký tự (vd emoji
        trên Windows cp1252), tự fallback về bản ASCII-safe.
        """
        if end == '\r':
            # Đếm ngược: chỉ in đè khi đang chạy trong terminal thật (TTY)
            if sys.stdout.isatty():
                try:
                    sys.stdout.write('\r' + str(message))
                    sys.stdout.flush()
                except (UnicodeEncodeError, ValueError):
                    pass
            # Khi redirect ra file/pipe thì bỏ qua để tránh rác log
            return

        try:
            logger.info(str(message))
        except (UnicodeEncodeError, ValueError):
            # Console không hỗ trợ ký tự -> ghi bản đã loại bỏ ký tự ngoài ASCII
            try:
                logger.info(str(message).encode('ascii', 'replace').decode('ascii'))
            except Exception:  # noqa: BLE001 - log không bao giờ được làm chết app
                pass


    def _atomic_write_json(self, path, data):
        """Ghi JSON an toàn: ghi file tạm rồi os.replace (tránh file hỏng khi bị kill)."""
        tmp_path = f"{path}.tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def _run_in_new_thread(self, func, *args, timeout=THREAD_JOIN_TIMEOUT, **kwargs):
        """
        Chạy hàm trong thread mới để tránh lỗi 'Playwright Sync API inside asyncio loop'.

        Playwright Sync API kiểm tra asyncio.get_running_loop(); nếu thread hiện tại
        có event loop đang chạy thì ném lỗi. Thread mới không có event loop nên OK.

        Có timeout để tránh treo process khi thread con bị hang (PaaS 24/7).
        """
        result = [None]
        error = [None]

        def target():
            try:
                result[0] = func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - chuyển lỗi sang thread chính
                error[0] = e

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            raise TimeoutError(f"{getattr(func, '__name__', 'task')} vượt quá timeout {timeout}s")

        if error[0]:
            raise error[0]
        return result[0]


    def _telegram_api(self, method, payload):
        """Gọi Telegram Bot API, trả về dict result hoặc None nếu lỗi."""
        if not self.telegram_bot_token:
            return None
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/{method}"
        try:
            response = requests.post(url, json=payload, timeout=TELEGRAM_API_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            self.log(f"❌ Telegram {method} lỗi: {response.text}")
            return None
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Telegram {method} exception: {e}")
            return None

    def _remove_cache_files(self, label=""):
        """
        Xóa cookies/credentials/output + Playwright temp (dùng chung cho cleanup).
        KHÔNG xóa telegram state. Giả định caller đã giữ self._cleanup_lock.
        """
        prefix = f"[{label}] " if label else ""

        # Xóa toàn bộ thư mục cookies rồi tạo lại rỗng
        if os.path.exists(COOKIES_DIR):
            shutil.rmtree(COOKIES_DIR, ignore_errors=True)
            os.makedirs(COOKIES_DIR, exist_ok=True)
            self.log(f"🗑️  {prefix}Đã xóa và tạo lại thư mục: {COOKIES_DIR}")

        # Xóa credentials file
        if os.path.exists(CREDENTIALS_FILE):
            try:
                os.remove(CREDENTIALS_FILE)
                self.log(f"🗑️  {prefix}Đã xóa credentials file")
            except OSError as e:
                self.log(f"⚠️  {prefix}Không thể xóa credentials: {e}")

        # Xóa output file
        if os.path.exists(OUTPUT_FILE):
            try:
                os.remove(OUTPUT_FILE)
                self.log(f"🗑️  {prefix}Đã xóa {OUTPUT_FILE}")
            except OSError as e:
                self.log(f"⚠️  {prefix}Không thể xóa output: {e}")

        # Xóa Playwright temp dirs
        playwright_temp_patterns = [
            os.path.join(tempfile.gettempdir(), 'playwright-*'),
            os.path.join(tempfile.gettempdir(), 'ms-playwright-*'),
        ]
        for pattern in playwright_temp_patterns:
            for temp_path in glob.glob(pattern):
                if not os.path.isdir(temp_path):
                    continue
                try:
                    shutil.rmtree(temp_path)
                    self.log(f"🗑️  {prefix}Đã xóa Playwright temp: {temp_path}")
                except OSError as e:
                    self.log(f"⚠️  {prefix}Không thể xóa {temp_path}: {e}")

    # ------------------------------ Telegram state ------------------------------

    def save_telegram_state(self):
        """Lưu trạng thái Telegram (chat_id, message_id) vào file (atomic)."""
        try:
            state = {
                'chat_id': self.telegram_chat_id,
                'bot_token': self.telegram_bot_token,
                'last_message_id': self.last_telegram_message_id,
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            with self._state_lock:
                self._atomic_write_json(TELEGRAM_STATE_FILE, state)
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi lưu telegram state: {e}")
            return False

    def load_telegram_state(self):
        """Tải trạng thái Telegram từ file (chat_id, bot_token, message_id)."""
        try:
            if not os.path.exists(TELEGRAM_STATE_FILE):
                return False

            with open(TELEGRAM_STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)

            # Load chat_id và bot_token từ file (ưu tiên file hơn environment variable)
            if state.get('chat_id'):
                self.telegram_chat_id = state['chat_id']
            if state.get('bot_token'):
                self.telegram_bot_token = state['bot_token']
            if 'last_message_id' in state:
                self.last_telegram_message_id = state['last_message_id']

            self.log(f"✅ Đã tải telegram state: chat_id={self.telegram_chat_id}, "
                     f"message_id={self.last_telegram_message_id}")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi tải telegram state: {e}")
            return False

    def delete_telegram_message(self, message_id):
        """Xóa tin nhắn Telegram theo ID."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return False

        result = self._telegram_api('deleteMessage', {
            'chat_id': self.telegram_chat_id,
            'message_id': message_id,
        })
        if result and result.get('ok'):
            self.log(f"✅ Đã xóa tin nhắn cũ (ID: {message_id})")
            return True
        return False

    def send_telegram_message(self, message):
        """Gửi tin nhắn Telegram - tự động xóa tin nhắn cũ trước khi gửi mới."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            self.log("⚠️  Không có Telegram config, bỏ qua gửi tin nhắn")
            return False

        # Xóa tin nhắn cũ nếu có
        if self.last_telegram_message_id:
            self.delete_telegram_message(self.last_telegram_message_id)

        result = self._telegram_api('sendMessage', {
            'chat_id': self.telegram_chat_id,
            'text': message,
            'parse_mode': 'HTML',
        })

        if result and result.get('ok'):
            self.last_telegram_message_id = result['result']['message_id']
            self.save_telegram_state()  # Lưu vào file
            self.log("✅ Đã gửi tin nhắn Telegram")
            return True

        self.log("❌ Lỗi gửi Telegram")
        return False


    # ------------------------------ Credentials / cookies ------------------------------

    def save_credentials(self, domain, username, password):
        """Lưu thông tin đăng nhập (atomic)."""
        try:
            with self._state_lock:
                credentials = {}
                if os.path.exists(CREDENTIALS_FILE):
                    with open(CREDENTIALS_FILE, 'r', encoding='utf-8') as f:
                        credentials = json.load(f)

                credentials[domain] = {
                    'username': username,
                    'password': password,
                    'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                }
                self._atomic_write_json(CREDENTIALS_FILE, credentials)

            self.log(f"✅ Đã lưu thông tin đăng nhập cho {domain}")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi lưu thông tin: {e}")
            return False

    def load_credentials(self, domain):
        """Tải thông tin đăng nhập."""
        try:
            if not os.path.exists(CREDENTIALS_FILE):
                return None
            with open(CREDENTIALS_FILE, 'r', encoding='utf-8') as f:
                credentials = json.load(f)
            return credentials.get(domain)
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi tải thông tin: {e}")
            return None

    def _cookie_file(self, domain):
        return os.path.join(COOKIES_DIR, f"{domain.replace('.', '_')}_cookies.json")

    def save_cookies(self, context, domain):
        """Lưu cookies (atomic)."""
        try:
            cookies = context.cookies()
            with self._state_lock:
                self._atomic_write_json(self._cookie_file(domain), cookies)
            self.log(f"✅ Đã lưu cookies cho {domain}")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi lưu cookies: {e}")
            return False

    def load_cookies(self, context, domain):
        """Tải cookies."""
        try:
            cookie_file = self._cookie_file(domain)
            if not os.path.exists(cookie_file):
                return False
            with open(cookie_file, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            context.add_cookies(cookies)
            self.log(f"✅ Đã tải cookies cho {domain}")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi tải cookies: {e}")
            return False

    # ------------------------------ Cleanup / reset ------------------------------

    def clear_cookies_cache(self):
        """Xóa cache (cookies, credentials, output, playwright temp). KHÔNG xóa telegram state.

        Được bảo vệ bởi lock để tránh 2 thread site xóa lẫn nhau giữa chừng.
        """
        try:
            with self._cleanup_lock:
                self.log("🧹 Đang dọn dẹp cache (cookies, credentials, output, playwright temp)...")
                self._remove_cache_files()
            self.log("✅ Đã dọn dẹp cache - giữ lại telegram state")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi dọn dẹp cache: {e}")
            return False

    def cleanup_on_startup_safe(self):
        """Dọn cache từ phiên deploy cũ, chỉ giữ lại telegram state (chat_id + message_id)."""
        try:
            with self._cleanup_lock:
                self.log("🧹 Bắt đầu cleanup startup: Xóa cache cũ, chỉ giữ telegram state...")

                # Backup telegram state trước khi cleanup
                telegram_backup = None
                if os.path.exists(TELEGRAM_STATE_FILE):
                    try:
                        with open(TELEGRAM_STATE_FILE, 'r', encoding='utf-8') as f:
                            telegram_backup = json.load(f)
                        self.log(f"💾 Đã backup telegram state: "
                                 f"chat_id={telegram_backup.get('chat_id', 'N/A')}, "
                                 f"message_id={telegram_backup.get('last_message_id', 'N/A')}")
                    except Exception as e:  # noqa: BLE001
                        self.log(f"⚠️  Không thể backup telegram state: {e}")

                # Xóa cache (dùng chung helper)
                self._remove_cache_files(label="Startup")

                # Khôi phục telegram state (nếu có backup)
                if telegram_backup:
                    try:
                        self._atomic_write_json(TELEGRAM_STATE_FILE, telegram_backup)
                        self.log("✅ [Startup] Đã khôi phục telegram state (giữ chat_id và message_id)")
                    except Exception as e:  # noqa: BLE001
                        self.log(f"⚠️  Không thể khôi phục telegram state: {e}")

            self.log("✅ [Startup] Đã dọn dẹp xong - chỉ giữ lại telegram state")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ [Startup] Lỗi khi cleanup: {e}")
            return False

    def reset_scraper_state(self):
        """Reset hoàn toàn trạng thái scraper, chỉ giữ lại telegram chat ID."""
        try:
            self.log("🔄 Bắt đầu reset hoàn toàn trạng thái scraper...")
            self.clear_cookies_cache()

            self.site1_campaign_info = []
            self.site2_campaign_info = []
            self.log("🗑️  Đã reset campaign info")

            if self.last_telegram_message_id:
                self.log(f"🗑️  Đã reset telegram message_id (giữ chat_id: {self.telegram_chat_id})")
                self.last_telegram_message_id = None

            self.log("✅ Đã reset hoàn toàn trạng thái scraper (chỉ giữ telegram chat ID)")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi khi reset trạng thái: {e}")
            return False


    # ------------------------------ Human-like behavior ------------------------------

    def simulate_mouse_movement(self, page):
        """Mô phỏng di chuột ngẫu nhiên như người thật."""
        try:
            viewport = page.viewport_size
            if viewport:
                for _ in range(3):
                    x = random.randint(100, viewport['width'] - 100)
                    y = random.randint(100, viewport['height'] - 100)
                    page.mouse.move(x, y)
                    time.sleep(random.uniform(0.1, 0.3))
        except Exception:  # noqa: BLE001 - không critical, bỏ qua
            pass

    def simulate_typing(self, element, text):
        """Mô phỏng gõ phím như người thật (fallback fill nếu lỗi)."""
        try:
            element.clear()
            for char in text:
                element.type(char)
                time.sleep(random.uniform(0.05, 0.15))
        except Exception:  # noqa: BLE001
            element.fill(text)

    def _find_first(self, page, selectors, timeout=2000):
        """Trả về element đầu tiên tìm thấy theo danh sách selector, else None."""
        for selector in selectors:
            try:
                el = page.wait_for_selector(selector, timeout=timeout)
                if el:
                    return el
            except Exception:  # noqa: BLE001 - thử selector kế tiếp
                continue
        return None

    def auto_login(self, page, username, password):
        """Tự động đăng nhập với hành vi người thật."""
        try:
            self.simulate_mouse_movement(page)

            email_field = self._find_first(page, EMAIL_SELECTORS)
            if email_field:
                email_field.click()
                time.sleep(random.uniform(0.3, 0.6))
                self.simulate_typing(email_field, username)
                time.sleep(random.uniform(0.5, 1.0))

            password_field = self._find_first(page, PASSWORD_SELECTORS)
            if password_field:
                password_field.click()
                time.sleep(random.uniform(0.3, 0.6))
                self.simulate_typing(password_field, password)
                time.sleep(random.uniform(0.5, 1.0))

            submit_button = self._find_first(page, SUBMIT_SELECTORS)
            if submit_button:
                self.simulate_mouse_movement(page)
                time.sleep(random.uniform(0.2, 0.5))

                # Playwright click() chờ navigation hoàn tất nhưng site dùng invisible
                # captcha làm chậm redirect -> timeout. Dùng JS click (evaluate) không
                # chờ navigation -> không timeout.
                try:
                    submit_button.click(timeout=CLICK_TIMEOUT_MS)
                except Exception as click_err:  # noqa: BLE001
                    self.log(f"⚠️ Click timeout (navigation), dùng JS click thay thế: {click_err}")
                    try:
                        submit_button.evaluate("el => el.click()")
                    except Exception as js_err:  # noqa: BLE001
                        self.log(f"⚠️ JS click cũng lỗi ({js_err}), thử dispatch_event")
                        submit_button.dispatch_event('click')

                return True

            return False
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi đăng nhập: {e}")
            return False


    # ------------------------------ Core scrape ------------------------------

    def _read_campaign_info(self, page, site_name):
        """Đọc thông tin campaign từ dashboard, trả về list dạng 'N - remaining_codes'."""
        results = []
        self.log("=" * 60)
        self.log(f"📊 [{site_name}] TIẾN ĐỘ & PHÂN BỔ SỐ MÃ THEO CAMPAIGN:")
        self.log("=" * 60)

        try:
            success_elements = page.query_selector_all('strong.text-success')
            self.log(f"🔍 [{site_name}] Tìm thấy {len(success_elements)} phần tử strong.text-success")

            if len(success_elements) == 0:
                self.log(f"⚠️ [{site_name}] Không tìm thấy phần tử strong.text-success nào")
                page_content = page.inner_text('body')[:500]
                self.log(f"📄 [{site_name}] Nội dung page (500 ký tự đầu): {page_content}")

            for idx, success_elem in enumerate(success_elements):
                try:
                    remaining_codes = success_elem.inner_text()
                    self.log(f"🔢 [{site_name}] #{idx + 1} - Số lượng mã còn: {remaining_codes}")

                    campaign_info = success_elem.evaluate('''
                        (element) => {
                            let parent = element.parentElement;
                            while (parent && !parent.style.display?.includes('flex')) {
                                parent = parent.parentElement;
                                if (!parent || parent.tagName === 'BODY') return null;
                            }
                            if (!parent) return null;

                            const flexContainer = parent.parentElement;
                            if (!flexContainer) return null;

                            const badges = flexContainer.querySelectorAll('.badge');
                            for (const badge of badges) {
                                const text = badge.innerText || badge.textContent;
                                if (!text.includes('Google 4 Step')) continue;

                                const small = badge.parentElement.querySelector('small');
                                const m = small ? small.innerText.match(/(\\d[\\d,.]*)\\s*camp/i) : null;
                                return {camps: m ? m[1] : null};
                            }
                            return null;
                        }
                    ''')

                    if campaign_info:
                        camps = campaign_info.get('camps') or "0"
                        campaign_line = f"{camps} - {remaining_codes}"
                        print(campaign_line)
                        results.append(campaign_line)
                    else:
                        self.log(f"⚠️ [{site_name}] Không tìm thấy thông tin camp cho: {remaining_codes}")
                except Exception as e:  # noqa: BLE001
                    self.log(f"Lỗi khi đọc campaign element #{idx + 1}: {e}")
                    continue

        except Exception as e:  # noqa: BLE001
            self.log(f"Lỗi khi đọc campaign info: {e}")
            self.log(f"Traceback: {traceback.format_exc()}")
            try:
                print(page.inner_text('body'))
            except Exception:  # noqa: BLE001
                pass

        self.log(f"📊 [{site_name}] Tổng số campaign tìm thấy: {len(results)}")
        self.log("=" * 60)
        return results

    @staticmethod
    def _close_quietly(context, browser):
        """Đóng context/browser, bỏ qua mọi lỗi (idempotent)."""
        for obj in (context, browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass


    def scrape_site(self, url, username, password, site_name, retry_count=0, max_retries=MAX_RETRIES):
        """Scrape một site cụ thể và trả về campaign info."""
        browser = None
        context = None

        try:
            with sync_playwright() as p:
                user_agent = random.choice(USER_AGENTS)

                browser = p.chromium.launch(headless=self.headless, args=BROWSER_ARGS)

                context = browser.new_context(
                    user_agent=user_agent,
                    viewport={'width': 1920, 'height': 1080},
                    locale='vi-VN',
                    timezone_id='Asia/Ho_Chi_Minh',
                    permissions=['geolocation'],
                    geolocation={'latitude': 21.0285, 'longitude': 105.8542},  # Hanoi
                )

                domain = urlparse(url).netloc
                self.load_cookies(context, domain)

                page = context.new_page()
                page.add_init_script(ANTI_BOT_INIT_SCRIPT)

                # Truy cập trực tiếp login page
                self.log(f"🌐 [{site_name}] Đang truy cập: {url}")
                page.goto(url, wait_until='domcontentloaded', timeout=LOGIN_TIMEOUT_MS)
                time.sleep(random.uniform(3, 7))  # Random delay 3-7 giây

                # Luôn thử login nếu có credentials (đặc biệt cho site 2)
                if username and password:
                    body_text = page.inner_text('body').lower()
                    needs_login = any(kw in body_text for kw in LOGIN_KEYWORDS)

                    # Force login cho site 2 hoặc nếu phát hiện cần login
                    if needs_login or site_name == "Site2":
                        self.log(f"🔐 [{site_name}] Thực hiện đăng nhập...")
                        self.log(f"🌐 [{site_name}] Đang đăng nhập: {url}")

                        if self.auto_login(page, username, password):
                            self.log(f"✅ [{site_name}] Đăng nhập thành công")
                            time.sleep(random.uniform(4, 8))
                            self.save_cookies(context, domain)
                        else:
                            self.log(f"⚠️ [{site_name}] Login thất bại")
                            self._close_quietly(context, browser)
                            context = browser = None

                            if retry_count < max_retries:
                                self.log(f"🔄 [{site_name}] Retry sau khi dọn cache "
                                         f"({retry_count + 1}/{max_retries})...")
                                self.clear_cookies_cache()
                                time.sleep(2)
                                return self.scrape_site(url, username, password, site_name,
                                                        retry_count + 1, max_retries)
                            self.log(f"❌ [{site_name}] Đã thử lại {max_retries} lần nhưng vẫn thất bại")
                            return []

                # Luôn chuyển sang dashboard
                dashboard_url = f"https://{domain}/member/dashboard"
                self.log(f"🌐 [{site_name}] Đang chuyển sang dashboard: {dashboard_url}")
                try:
                    page.goto(dashboard_url, wait_until='domcontentloaded', timeout=DASHBOARD_TIMEOUT_MS)
                except Exception as goto_err:  # noqa: BLE001
                    # Mạng/server chậm thoáng qua: thử lại 1 lần trước khi coi là lỗi thật
                    self.log(f"⚠️ [{site_name}] Timeout khi vào dashboard, thử lại lần nữa: {goto_err}")
                    time.sleep(3)
                    page.goto(dashboard_url, wait_until='domcontentloaded', timeout=DASHBOARD_TIMEOUT_MS)
                time.sleep(random.uniform(2, 5))

                self.log(f"🔗 [{site_name}] URL hiện tại: {page.url}")

                results = self._read_campaign_info(page, site_name)

                self._close_quietly(context, browser)
                context = browser = None
                return results

        except Exception as e:  # noqa: BLE001
            error_msg = str(e)
            self.log(f"❌ [{site_name}] Lỗi scrape: {e}")

            error_lower = error_msg.lower()
            matched_patterns = [pat for pat in self.retry_patterns if pat in error_lower]
            if retry_count < max_retries and matched_patterns:
                self.log(f"🔄 [{site_name}] Lỗi transient (match: {matched_patterns}), "
                         f"dọn cache và thử lại (retry {retry_count + 1}/{max_retries})...")
                self.clear_cookies_cache()
                time.sleep(2)
                return self.scrape_site(url, username, password, site_name, retry_count + 1, max_retries)

            return []
        finally:
            # Luôn close để không leak browser process trên server 24/7
            self._close_quietly(context, browser)


    # ------------------------------ Iteration / run loop ------------------------------

    def scrape_once(self):
        """Scrape cả 2 site một lần, trả về (site1_info, site2_info, success_flag)."""
        self.site1_campaign_info = []
        self.site2_campaign_info = []

        # Mỗi site chạy trong thread mới để tránh lỗi 'Sync API inside asyncio loop'
        self.site1_campaign_info = self._run_in_new_thread(
            self.scrape_site, self.url1, self.username1, self.password1, "Site1", 0
        )

        # Chỉ scrape site 2 nếu có đủ credentials
        if self.username2 and self.password2:
            self.site2_campaign_info = self._run_in_new_thread(
                self.scrape_site, self.url2, self.username2, self.password2, "Site2", 0
            )
        else:
            self.log("ℹ️  Bỏ qua Site2 (thiếu credentials)")

        success = bool(self.site1_campaign_info or self.site2_campaign_info)
        return self.site1_campaign_info, self.site2_campaign_info, success

    def _write_output_file(self, iteration, all_campaign_info):
        """Ghi kết quả ra file, tự truncate nếu file quá lớn."""
        try:
            if os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > OUTPUT_MAX_BYTES:
                self.log(f"⚠️  {OUTPUT_FILE} vượt {OUTPUT_MAX_BYTES} bytes - truncate")

            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                f.write(f"Lần #{iteration} - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n")
                f.write("\n".join(all_campaign_info))
            self.log(f"📄 Đã lưu: {OUTPUT_FILE}")
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ Lỗi ghi {OUTPUT_FILE}: {e}")

    def _build_telegram_message(self):
        """Tạo nội dung tin nhắn Telegram rút gọn cho cả 2 site."""
        telegram_message = ""

        domain1 = urlparse(self.url1).netloc
        short_name1 = domain1.split('.')[-1].capitalize()
        if self.site1_campaign_info:
            for campaign_line in self.site1_campaign_info:
                telegram_message += f"{short_name1}: {campaign_line}\n"
        else:
            telegram_message += f"{short_name1}: ❌\n"

        if self.username2 and self.password2:
            domain2 = urlparse(self.url2).netloc
            short_name2 = domain2.split('.')[-1].capitalize()
            if self.site2_campaign_info:
                for campaign_line in self.site2_campaign_info:
                    telegram_message += f"{short_name2}: {campaign_line}\n"
            else:
                telegram_message += f"{short_name2}: ❌\n"

        return telegram_message

    def _countdown(self):
        """Đếm ngược tới lần scrape tiếp theo, có thể ngắt bằng stop event."""
        for remaining in range(self.refresh_interval, 0, -1):
            if self._stop_event.is_set():
                return
            mins, secs = divmod(remaining, 60)
            if secs == 0 or remaining <= 10:
                self.log(f"   ⏱️  Còn lại: {mins:02d}:{secs:02d}", end='\r')
            time.sleep(1)
        self.log("")


    def run(self):
        """Chạy auto-refresh liên tục."""
        self.log("🚀 BẮT ĐẦU SERVER SCRAPER - MULTI-SITE")
        self.log(f"🌐 Site 1: {self.url1}")
        site2_status = self.url2 if (self.username2 and self.password2) else '(bỏ qua - thiếu credentials)'
        self.log(f"🌐 Site 2: {site2_status}")
        self.log(f"⏱️  Refresh mỗi: {self.refresh_interval // 60} phút")
        self.log(f"👤 Username 1: {self.username1 if self.username1 else 'Không có'}")
        self.log(f"👤 Username 2: {self.username2 if self.username2 else 'Không có'}")
        self.log(f"🖥️  Headless: {self.headless}")
        self.log(f"🧹 Auto cleanup on startup: {self.cleanup_on_startup}")
        self.log(f"🔄 Reset cache định kỳ sau: {self.reset_after_iterations} lần")
        self.log("=" * 60)

        # Lưu credentials từ environment
        if self.username1 and self.password1:
            self.save_credentials(urlparse(self.url1).netloc, self.username1, self.password1)
        if self.username2 and self.password2:
            self.save_credentials(urlparse(self.url2).netloc, self.username2, self.password2)

        # Lưu telegram state để lần deploy sau khôi phục
        self.save_telegram_state()

        iteration = 0
        last_iteration_success = None

        try:
            while not self._stop_event.is_set():
                iteration += 1

                # Reset định kỳ để tránh lỗi đăng nhập
                if iteration > 1 and (iteration - 1) % self.reset_after_iterations == 0:
                    self.log(f"\n🔄 Đạt mốc {self.reset_after_iterations} lần quét, reset cache định kỳ...")
                    self.cleanup_on_startup_safe()

                # Nếu iteration trước fail, dọn cache TRƯỚC khi chạy lần này
                if last_iteration_success is False:
                    self.log(f"🧹 Iteration trước fail - dọn cache trước khi chạy {iteration}")
                    self.clear_cookies_cache()

                self.log(f"\n{'=' * 60}")
                self.log(f"🔄 Lần #{iteration}")
                self.log(f"{'=' * 60}")

                iter_start = time.time()
                try:
                    _s1, _s2, success = self.scrape_once()
                    elapsed = time.time() - iter_start

                    if success:
                        self.log(f"✅ Scrape thành công (mất {elapsed:.1f}s)")

                        all_campaign_info = list(self.site1_campaign_info) + list(self.site2_campaign_info)
                        self._write_output_file(iteration, all_campaign_info)

                        telegram_message = self._build_telegram_message()
                        self.send_telegram_message(telegram_message)
                        last_iteration_success = True
                    else:
                        self.log(f"❌ Cả 2 site fail (mất {elapsed:.1f}s) - dọn cache cho iteration sau")
                        self.clear_cookies_cache()
                        last_iteration_success = False

                except Exception as e:  # noqa: BLE001
                    self.log(f"❌ Lỗi bất ngờ trong scrape_once: {e}")
                    self.log(f"Traceback: {traceback.format_exc()}")
                    self.clear_cookies_cache()
                    last_iteration_success = False

                if self._stop_event.is_set():
                    break

                self.log(f"\n⏳ Đợi {self.refresh_interval // 60} phút...")
                self._countdown()

        except KeyboardInterrupt:
            self.log(f"\n⏹️  Đã dừng sau {iteration} lần")
        except Exception as e:  # noqa: BLE001
            self.log(f"\n❌ Lỗi: {e}")
            self.log(f"Traceback: {traceback.format_exc()}")


def main():
    # Kiểm tra conflict asyncio trước khi chạy.
    # Nếu PaaS (Railway/Heroku) chạy script trong asyncio loop, Playwright Sync API
    # sẽ báo lỗi -> giải pháp là chạy scrape trong thread mới (xem _run_in_new_thread).
    try:
        import asyncio
        asyncio.get_running_loop()
        print("⚠️  Phát hiện asyncio event loop đang chạy - Playwright sẽ chạy trong thread mới")
    except RuntimeError:
        pass  # Không có event loop đang chạy - bình thường

    scraper = ServerScraper()

    # Kiểm tra xem có yêu cầu reset trạng thái không
    if len(sys.argv) > 1 and sys.argv[1] == '--reset':
        scraper.log("🔄 Chế độ reset trạng thái scraper...")
        if scraper.reset_scraper_state():
            scraper.log("✅ Reset trạng thái thành công!")
        else:
            scraper.log("❌ Reset trạng thái thất bại!")
        sys.exit(0)

    # Khởi động health server cho PaaS
    start_health_server()

    # Graceful shutdown: bắt SIGTERM/SIGINT để thoát vòng lặp sạch sẽ
    def _handle_signal(signum, _frame):
        scraper.log(f"\n⏹️  Nhận signal {signum} - đang dừng an toàn...")
        scraper._stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _handle_signal)

    scraper.run()


if __name__ == "__main__":
    main()

