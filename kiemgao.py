"""
Web Scraper Server
Chạy auto-refresh 24/7 với thông tin từ environment variables
"""

import os
import sys
import time
import json
import re
import random
import requests
import shutil
import tempfile
import glob
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class ServerScraper:
    def __init__(self):
        # Cấu hình từ environment variables cho site 1 (link999.app)
        self.url1 = os.getenv('SCRAPER_URL', 'https://link999.app/auth/signin')
        self.username1 = os.getenv('SCRAPER_USERNAME', 'donam962004@gmail.com')
        self.password1 = os.getenv('SCRAPER_PASSWORD', 'nam523181')
        
        # Ưu tiên REFRESH_INTERVAL (giây), nếu không có thì dùng SCRAPER_REFRESH_MINUTES (phút)
        refresh_interval_env = os.getenv('REFRESH_INTERVAL')
        if refresh_interval_env:
            self.refresh_interval = int(refresh_interval_env)
        else:
            self.refresh_interval = int(os.getenv('SCRAPER_REFRESH_MINUTES', '2')) * 60  # phút -> giây
        self.headless = os.getenv('SCRAPER_HEADLESS', 'true').lower() == 'true'
        
        # Auto cleanup on startup (mặc định true cho Railway)
        self.cleanup_on_startup = os.getenv('CLEANUP_ON_STARTUP', 'true').lower() == 'true'
        
        # Số lần chạy trước khi reset cache
        self.reset_after_iterations = 30
        
        # Telegram config
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        self.last_telegram_message_id = None  # Lưu ID tin nhắn cuối cùng để xóa
        self.site1_campaign_info = []  # Lưu thông tin campaign cho site 1
        
        self.credentials_file = 'saved_credentials.json'
        self.cookies_dir = 'saved_cookies'
        self.telegram_state_file = 'telegram_state.json'
        
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        ]
        
        # Tạo thư mục cookies
        if not os.path.exists(self.cookies_dir):
            os.makedirs(self.cookies_dir)
        
        # Auto cleanup on startup để xóa sạch cache từ phiên deploy cũ
        if self.cleanup_on_startup:
            self.log("🧹 Auto cleanup on startup: Đang dọn dẹp cache từ phiên deploy cũ...")
            self.cleanup_on_startup_safe()
        
        # Load telegram state từ file
        self.load_telegram_state()
    
    def log(self, message, end='\n'):
        """Log với timestamp"""
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{timestamp}] {message}", end=end)
    
    def save_telegram_state(self):
        """Lưu trạng thái Telegram (chat_id, message_id) vào file"""
        try:
            state = {
                'chat_id': self.telegram_chat_id,
                'bot_token': self.telegram_bot_token,
                'last_message_id': self.last_telegram_message_id,
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            with open(self.telegram_state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi lưu telegram state: {e}")
            return False
    
    def load_telegram_state(self):
        """Tải trạng thái Telegram từ file (chat_id, bot_token, message_id)"""
        try:
            if not os.path.exists(self.telegram_state_file):
                return False
            
            with open(self.telegram_state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            
            # Load chat_id và bot_token từ file (ưu tiên file hơn environment variable)
            if 'chat_id' in state and state['chat_id']:
                self.telegram_chat_id = state['chat_id']
            if 'bot_token' in state and state['bot_token']:
                self.telegram_bot_token = state['bot_token']
            if 'last_message_id' in state:
                self.last_telegram_message_id = state['last_message_id']
            
            self.log(f"✅ Đã tải telegram state: chat_id={self.telegram_chat_id}, message_id={self.last_telegram_message_id}")
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi tải telegram state: {e}")
            return False
    
    def delete_telegram_message(self, message_id):
        """Xóa tin nhắn Telegram theo ID"""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/deleteMessage"
            data = {
                'chat_id': self.telegram_chat_id,
                'message_id': message_id
            }
            response = requests.post(url, json=data, timeout=10)
            
            if response.status_code == 200:
                self.log(f"✅ Đã xóa tin nhắn cũ (ID: {message_id})")
                return True
            else:
                return False
        except Exception as e:
            self.log(f"⚠️  Không thể xóa tin nhắn cũ: {e}")
            return False
    
    def send_telegram_message(self, message):
        """Gửi tin nhắn qua Telegram bot - tự động xóa tin nhắn cũ trước khi gửi mới"""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            self.log("⚠️  Không có Telegram config, bỏ qua gửi tin nhắn")
            return False
        
        try:
            # Xóa tin nhắn cũ nếu có
            if self.last_telegram_message_id:
                self.delete_telegram_message(self.last_telegram_message_id)
            
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            data = {
                'chat_id': self.telegram_chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, json=data, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                # Lưu message_id để xóa lần sau
                self.last_telegram_message_id = result['result']['message_id']
                self.save_telegram_state()  # Lưu vào file
                self.log("✅ Đã gửi tin nhắn Telegram")
                return True
            else:
                self.log(f"❌ Lỗi gửi Telegram: {response.text}")
                return False
        except Exception as e:
            self.log(f"❌ Lỗi gửi Telegram: {e}")
            return False
    
    def save_credentials(self, domain, username, password):
        """Lưu thông tin đăng nhập"""
        try:
            credentials = {}
            if os.path.exists(self.credentials_file):
                with open(self.credentials_file, 'r', encoding='utf-8') as f:
                    credentials = json.load(f)
            
            credentials[domain] = {
                'username': username,
                'password': password,
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            with open(self.credentials_file, 'w', encoding='utf-8') as f:
                json.dump(credentials, f, indent=2, ensure_ascii=False)
            
            self.log(f"✅ Đã lưu thông tin đăng nhập cho {domain}")
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi lưu thông tin: {e}")
            return False
    
    def load_credentials(self, domain):
        """Tải thông tin đăng nhập"""
        try:
            if not os.path.exists(self.credentials_file):
                return None
            
            with open(self.credentials_file, 'r', encoding='utf-8') as f:
                credentials = json.load(f)
            
            if domain in credentials:
                return credentials[domain]
            return None
        except Exception as e:
            self.log(f"❌ Lỗi khi tải thông tin: {e}")
            return None
    
    def save_cookies(self, context, domain):
        """Lưu cookies"""
        try:
            cookies = context.cookies()
            cookie_file = os.path.join(self.cookies_dir, f"{domain.replace('.', '_')}_cookies.json")
            
            with open(cookie_file, 'w', encoding='utf-8') as f:
                json.dump(cookies, f, indent=2)
            
            self.log(f"✅ Đã lưu cookies cho {domain}")
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi lưu cookies: {e}")
            return False
    
    def load_cookies(self, context, domain):
        """Tải cookies"""
        try:
            cookie_file = os.path.join(self.cookies_dir, f"{domain.replace('.', '_')}_cookies.json")
            
            if not os.path.exists(cookie_file):
                return False
            
            with open(cookie_file, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            
            context.add_cookies(cookies)
            self.log(f"✅ Đã tải cookies cho {domain}")
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi tải cookies: {e}")
            return False
    
    def clear_cookies_cache(self):
        """Xóa toàn bộ cache cookies, credentials, output files và Playwright temp files (KHÔNG xóa telegram state)"""
        try:
            # Xóa toàn bộ thư mục saved_cookies/
            if os.path.exists(self.cookies_dir):
                shutil.rmtree(self.cookies_dir)
                self.log(f"🗑️  Đã xóa toàn bộ thư mục: {self.cookies_dir}")
                # Tạo lại thư mục rỗng
                os.makedirs(self.cookies_dir)
                self.log(f"✅ Đã tạo lại thư mục rỗng: {self.cookies_dir}")
            
            # Xóa credentials file
            if os.path.exists(self.credentials_file):
                os.remove(self.credentials_file)
                self.log(f"🗑️  Đã xóa credentials file")
            
            # Xóa server_output.txt
            output_file = "server_output.txt"
            if os.path.exists(output_file):
                os.remove(output_file)
                self.log(f"🗑️  Đã xóa {output_file}")
            
            # Xóa Playwright temporary files
            playwright_temp_dirs = [
                os.path.join(tempfile.gettempdir(), 'playwright-*'),
                os.path.join(tempfile.gettempdir(), 'ms-playwright-*'),
            ]
            
            for temp_pattern in playwright_temp_dirs:
                matching_paths = glob.glob(temp_pattern)
                for temp_path in matching_paths:
                    # Chỉ xóa thư mục, bỏ qua các file lẻ (vd: .whl cache) trùng pattern
                    if not os.path.isdir(temp_path):
                        continue
                    try:
                        shutil.rmtree(temp_path)
                        self.log(f"🗑️  Đã xóa Playwright temp: {temp_path}")
                    except Exception as e:
                        self.log(f"⚠️  Không thể xóa {temp_path}: {e}")
            
            # KHÔNG xóa telegram_state_file để giữ lại message_id
            
            self.log("✅ Đã dọn dẹp toàn bộ cache (cookies, credentials, output, playwright temp) - giữ lại telegram state")
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi dọn dẹp cache: {e}")
            return False
    
    def cleanup_on_startup_safe(self):
        """Dọn dẹp toàn bộ cache từ phiên deploy cũ, chỉ giữ lại telegram state (chat_id và message_id)"""
        try:
            self.log("🧹 Bắt đầu cleanup startup: Xóa toàn bộ cache cũ, chỉ giữ telegram state...")
            
            # Backup telegram state trước khi cleanup
            telegram_backup = None
            if os.path.exists(self.telegram_state_file):
                try:
                    with open(self.telegram_state_file, 'r', encoding='utf-8') as f:
                        telegram_backup = json.load(f)
                    self.log(f"💾 Đã backup telegram state: chat_id={telegram_backup.get('chat_id', 'N/A')}, message_id={telegram_backup.get('last_message_id', 'N/A')}")
                except Exception as e:
                    self.log(f"⚠️  Không thể backup telegram state: {e}")
            
            # Xóa toàn bộ thư mục saved_cookies/
            if os.path.exists(self.cookies_dir):
                shutil.rmtree(self.cookies_dir)
                self.log(f"🗑️  [Startup] Đã xóa thư mục cookies cũ: {self.cookies_dir}")
                os.makedirs(self.cookies_dir)
                self.log(f"✅ [Startup] Đã tạo lại thư mục cookies rỗng")
            
            # Xóa credentials file
            if os.path.exists(self.credentials_file):
                os.remove(self.credentials_file)
                self.log(f"🗑️  [Startup] Đã xóa credentials cũ")
            
            # Xóa server_output.txt
            output_file = "server_output.txt"
            if os.path.exists(output_file):
                os.remove(output_file)
                self.log(f"🗑️  [Startup] Đã xóa output file cũ")
            
            # Xóa Playwright temporary files
            playwright_temp_dirs = [
                os.path.join(tempfile.gettempdir(), 'playwright-*'),
                os.path.join(tempfile.gettempdir(), 'ms-playwright-*'),
            ]
            
            for temp_pattern in playwright_temp_dirs:
                matching_paths = glob.glob(temp_pattern)
                for temp_path in matching_paths:
                    if not os.path.isdir(temp_path):
                        continue
                    try:
                        shutil.rmtree(temp_path)
                        self.log(f"🗑️  [Startup] Đã xóa Playwright temp: {temp_path}")
                    except Exception as e:
                        self.log(f"⚠️  [Startup] Không thể xóa {temp_path}: {e}")
            
            # Khôi phục telegram state (nếu có backup)
            if telegram_backup:
                try:
                    with open(self.telegram_state_file, 'w', encoding='utf-8') as f:
                        json.dump(telegram_backup, f, indent=2, ensure_ascii=False)
                    self.log(f"✅ [Startup] Đã khôi phục telegram state (giữ lại chat_id và message_id)")
                except Exception as e:
                    self.log(f"⚠️  Không thể khôi phục telegram state: {e}")
            
            self.log("✅ [Startup] Đã dọn dẹp hoàn toàn cache cũ - chỉ giữ lại telegram state")
            return True
        except Exception as e:
            self.log(f"❌ [Startup] Lỗi khi cleanup: {e}")
            return False
    
    def reset_scraper_state(self):
        """Reset hoàn toàn trạng thái của scraper, chỉ giữ lại telegram chat ID"""
        try:
            self.log("🔄 Bắt đầu reset hoàn toàn trạng thái scraper...")
            
            # Dọn dẹp cache
            self.clear_cookies_cache()
            
            # Reset campaign info
            self.site1_campaign_info = []
            self.log("🗑️  Đã reset campaign info")
            
            # Giữ lại telegram_chat_id và telegram_bot_token từ environment
            # Chỉ reset message_id trong state
            if self.last_telegram_message_id:
                self.log(f"🗑️  Đã reset telegram message_id (giữ lại chat_id: {self.telegram_chat_id})")
                self.last_telegram_message_id = None
                # Không xóa telegram_state_file để giữ lại cấu hình
            
            self.log("✅ Đã reset hoàn toàn trạng thái scraper (chỉ giữ lại telegram chat ID)")
            return True
        except Exception as e:
            self.log(f"❌ Lỗi khi reset trạng thái: {e}")
            return False
    
    def simulate_mouse_movement(self, page):
        """Mô phỏng di chuột ngẫu nhiên như người thật"""
        try:
            viewport = page.viewport_size
            if viewport:
                for _ in range(3):
                    x = random.randint(100, viewport['width'] - 100)
                    y = random.randint(100, viewport['height'] - 100)
                    page.mouse.move(x, y)
                    time.sleep(random.uniform(0.1, 0.3))
        except:
            pass
    
    def simulate_typing(self, element, text):
        """Mô phỏng gõ phím như người thật"""
        try:
            element.clear()
            for char in text:
                element.type(char)
                time.sleep(random.uniform(0.05, 0.15))
        except:
            element.fill(text)
    
    def auto_login(self, page, username, password):
        """Tự động đăng nhập với hành vi người thật"""
        try:
            # Simulate mouse movement before typing
            self.simulate_mouse_movement(page)
            
            email_selectors = [
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
            
            password_selectors = [
                'input[name="password"]',
                'input#password',
                'input#signin-password',
                'input[type="password"]',
                'input[placeholder*="password"]',
                'input[placeholder*="Password"]',
                'input[placeholder*="Mật khẩu"]',
            ]
            
            submit_selectors = [
                'button#invisibleCaptchaSignin',
                'button[type="submit"]',
                'button:has-text("Sign In")',
                'button:has-text("Đăng nhập")',
                'button:has-text("Đăng Nhập Ngay")',
                'button:has-text("Login")',
                'input[type="submit"]',
            ]
            
            email_field = None
            for selector in email_selectors:
                try:
                    email_field = page.wait_for_selector(selector, timeout=2000)
                    if email_field:
                        break
                except:
                    continue
            
            if email_field:
                # Click vào field trước khi gõ
                email_field.click()
                time.sleep(random.uniform(0.3, 0.6))
                # Simulate typing
                self.simulate_typing(email_field, username)
                time.sleep(random.uniform(0.5, 1.0))
            
            password_field = None
            for selector in password_selectors:
                try:
                    password_field = page.wait_for_selector(selector, timeout=2000)
                    if password_field:
                        break
                except:
                    continue
            
            if password_field:
                # Click vào field trước khi gõ
                password_field.click()
                time.sleep(random.uniform(0.3, 0.6))
                # Simulate typing
                self.simulate_typing(password_field, password)
                time.sleep(random.uniform(0.5, 1.0))
            
            submit_button = None
            for selector in submit_selectors:
                try:
                    submit_button = page.wait_for_selector(selector, timeout=2000)
                    if submit_button:
                        break
                except:
                    continue
            
            if submit_button:
                # Mouse movement trước khi click
                self.simulate_mouse_movement(page)
                time.sleep(random.uniform(0.2, 0.5))
                submit_button.click()
                return True
            
            return False
        except Exception as e:
            self.log(f"❌ Lỗi đăng nhập: {e}")
            return False
    
    def scrape_site(self, url, username, password, site_name, retry_count=0, max_retries=3):
        """Scrape một site cụ thể và trả về campaign info"""
        site_campaign_info = []
        
        try:
            with sync_playwright() as p:
                # Random user agent mỗi lần
                user_agent = random.choice(self.user_agents)
                
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-dev-shm-usage',
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-web-security',
                        '--disable-features=VizDisplayCompositor',
                    ]
                )
                
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
                page.add_init_script("""
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
                """)
                
                # Truy cập trực tiếp login page
                self.log(f"🌐 [{site_name}] Đang truy cập: {url}")
                page.goto(url, wait_until='domcontentloaded', timeout=60000)
                time.sleep(random.uniform(3, 7))  # Random delay 3-7 giây
                
                # Luôn thử login nếu có credentials (đặc biệt cho site 2)
                if username and password:
                    # Kiểm tra xem có cần login không
                    body_text = page.inner_text('body').lower()
                    needs_login = any(keyword in body_text for keyword in ['sign in', 'login', 'email', 'password', 'username', 'đăng nhập', 'đăng nhập ngay'])
                    
                    # Force login cho site 2 hoặc nếu phát hiện cần login
                    if needs_login or site_name == "Site2":
                        self.log(f"🔐 [{site_name}] Thực hiện đăng nhập...")
                        self.log(f"🌐 [{site_name}] Đang đăng nhập: {url}")
                        
                        if self.auto_login(page, username, password):
                            self.log(f"✅ [{site_name}] Đăng nhập thành công")
                            time.sleep(random.uniform(4, 8))  # Random delay sau login
                            self.save_cookies(context, domain)
                        else:
                            self.log(f"⚠️ [{site_name}] Login thất bại")
                            context.close()
                            browser.close()
                            
                            # Nếu login thất bại và chưa retry hết, dọn dẹp cache và thử lại
                            if retry_count < max_retries:
                                self.log(f"🔄 [{site_name}] Dọn dẹp cache và thử lại từ đầu (retry {retry_count + 1}/{max_retries})...")
                                self.clear_cookies_cache()
                                time.sleep(2)
                                return self.scrape_site(url, username, password, site_name, retry_count + 1, max_retries)
                            else:
                                self.log(f"❌ [{site_name}] Đã thử lại {max_retries} lần nhưng vẫn thất bại")
                                return []
                
                # Luôn chuyển sang dashboard (dù có login hay không)
                dashboard_url = f"https://{domain}/member/dashboard"
                self.log(f"🌐 [{site_name}] Đang chuyển sang dashboard: {dashboard_url}")
                try:
                    page.goto(dashboard_url, wait_until='domcontentloaded', timeout=90000)
                except Exception as goto_err:
                    # Mạng/server chậm thoáng qua: thử lại 1 lần trước khi coi là lỗi thật sự
                    self.log(f"⚠️ [{site_name}] Timeout khi vào dashboard, thử lại lần nữa: {goto_err}")
                    time.sleep(3)
                    page.goto(dashboard_url, wait_until='domcontentloaded', timeout=90000)
                time.sleep(random.uniform(2, 5))
                
                # Kiểm tra xem đã vào dashboard thành công chưa
                current_url = page.url
                self.log(f"🔗 [{site_name}] URL hiện tại: {current_url}")
                
                # Đọc và hiển thị phần Tiến độ & Phân bổ số mã theo Campaign
                self.log("="*60)
                self.log(f"📊 [{site_name}] TIẾN ĐỘ & PHÂN BỔ SỐ MÃ THEO CAMPAIGN:")
                self.log("="*60)
                
                # Sử dụng Playwright để đọc trực tiếp từ HTML elements
                try:
                    self.log(f"🔍 [{site_name}] Bắt đầu tìm kiếm phần tử...")
                    # Tìm tất cả các phần tử strong.text-success
                    success_elements = page.query_selector_all('strong.text-success')
                    self.log(f"🔍 [{site_name}] Tìm thấy {len(success_elements)} phần tử strong.text-success")
                    
                    if len(success_elements) == 0:
                        self.log(f"⚠️ [{site_name}] Không tìm thấy phần tử strong.text-success nào")
                        # Hiển thị nội dung page để debug
                        page_content = page.inner_text('body')[:500]
                        self.log(f"📄 [{site_name}] Nội dung page (500 ký tự đầu): {page_content}")
                    
                    for idx, success_elem in enumerate(success_elements):
                        try:
                            self.log(f"🔍 [{site_name}] Đang xử lý phần tử #{idx + 1}")
                            # Lấy số lượng mã còn
                            remaining_codes = success_elem.inner_text()
                            self.log(f"🔢 [{site_name}] Số lượng mã còn: {remaining_codes}")
                            
                            # Sử dụng JavaScript để tìm tên campaign từ phần tử badge gần nhất
                            campaign_name = success_elem.evaluate('''
                                (element) => {
                                    // Tìm phần tử cha chứa div flex
                                    let parent = element.parentElement;
                                    while (parent && !parent.style.display?.includes('flex')) {
                                        parent = parent.parentElement;
                                        if (!parent || parent.tagName === 'BODY') return null;
                                    }
                                    
                                    if (!parent) return null;
                                    
                                    // Tìm badge element trong phần tử cha (sibling div)
                                    const flexContainer = parent.parentElement;
                                    if (!flexContainer) return null;
                                    
                                    // Tìm tất cả các badge trong container
                                    const badges = flexContainer.querySelectorAll('.badge');
                                    for (const badge of badges) {
                                        const text = badge.innerText || badge.textContent;
                                        let name = null;
                                        if (text.includes('Google 4 Step')) name = 'Octo 4';
                                        else if (text.includes('Google 2 Step')) name = 'Octo 2';
                                        else if (text.includes('Google 3 Step')) name = 'Octo 3';
                                        if (!name) continue;

                                        // Số camp nằm trong <small> cạnh badge: "(38 camp | 35.5%)"
                                        const small = badge.parentElement.querySelector('small');
                                        const m = small ? small.innerText.match(/(\\d[\\d,.]*)\\s*camp/i) : null;
                                        return {name: name, camps: m ? m[1] : null};
                                    }

                                    return null;
                                }
                            ''')

                            if campaign_name:
                                camps = campaign_name.get('camps')
                                camp_part = f" - {camps} camp" if camps else ""
                                campaign_line = f"{campaign_name['name']}{camp_part} - {remaining_codes} mã"
                                print(campaign_line)
                                site_campaign_info.append(campaign_line)
                            else:
                                self.log(f"⚠️ [{site_name}] Không tìm thấy tên campaign cho số lượng: {remaining_codes}")
                            
                        except Exception as e:
                            self.log(f"Lỗi khi đọc campaign element #{idx + 1}: {e}")
                            continue
                            
                except Exception as e:
                    self.log(f"Lỗi khi đọc campaign info: {e}")
                    import traceback
                    self.log(f"Traceback: {traceback.format_exc()}")
                    # Fallback: hiển thị toàn bộ nếu có lỗi
                    dashboard_content = page.inner_text('body')
                    print(dashboard_content)
                
                self.log(f"📊 [{site_name}] Tổng số campaign tìm thấy: {len(site_campaign_info)}")
                self.log("="*60)
                
                context.close()
                browser.close()
                
                return site_campaign_info
                
        except Exception as e:
            error_msg = str(e)
            self.log(f"❌ [{site_name}] Lỗi scrape: {e}")
            
            # Nếu là lỗi timeout hoặc login liên quan, dọn dẹp cache và thử lại
            if retry_count < max_retries and ('timeout' in error_msg.lower() or 'login' in error_msg.lower() or 'element' in error_msg.lower()):
                self.log(f"🔄 [{site_name}] Phát hiện lỗi timeout/login, dọn dẹp cache và thử lại (retry {retry_count + 1}/{max_retries})...")
                self.clear_cookies_cache()
                time.sleep(2)
                return self.scrape_site(url, username, password, site_name, retry_count + 1, max_retries)
            
            return []
    
    def scrape_once(self):
        """Scrape site một lần"""
        # Reset campaign info
        self.site1_campaign_info = []
        
        # Scrape site 1
        self.site1_campaign_info = self.scrape_site(self.url1, self.username1, self.password1, "Site1", retry_count=0)
        
        # Trả về kết quả dummy để tương thích với code cũ
        return "dummy_content", "dummy_content_full"
    
    def run(self):
        """Chạy auto-refresh liên tục"""
        self.log("🚀 BẮT ĐẦU SERVER SCRAPER")
        self.log(f"🌐 Site: {self.url1}")
        self.log(f"⏱️  Refresh mỗi: {self.refresh_interval//60} phút")
        self.log(f"👤 Username: {self.username1 if self.username1 else 'Không có'}")
        self.log(f"🖥️  Headless: {self.headless}")
        self.log(f"🧹 Auto cleanup on startup: {self.cleanup_on_startup}")
        self.log(f"🔄 Reset cache định kỳ sau: {self.reset_after_iterations} lần")
        self.log("="*60)
        
        # Lưu credentials từ environment
        if self.username1 and self.password1:
            domain1 = urlparse(self.url1).netloc
            self.save_credentials(domain1, self.username1, self.password1)
        
        # Lưu telegram state (chat_id, bot_token) vào file để lần deploy sau khôi phục
        self.save_telegram_state()
        
        iteration = 0
        
        try:
            while True:
                iteration += 1
                
                # Reset định kỳ để tránh lỗi đăng nhập
                if iteration > 1 and (iteration - 1) % self.reset_after_iterations == 0:
                    self.log(f"\n🔄 Đạt mốc {self.reset_after_iterations} lần quét, tiến hành reset cache định kỳ để chống lỗi...")
                    self.cleanup_on_startup_safe()
                    
                self.log(f"\n{'='*60}")
                self.log(f"🔄 Lần #{iteration}")
                self.log(f"{'='*60}")
                
                result = self.scrape_once()
                
                if result:  # result is now a tuple (text_content, text_content_full)
                    text_content, text_content_full = result
                    self.log("✅ Scrape thành công")
                    
                    # Lưu kết quả
                    output_file = "server_output.txt"
                    all_campaign_info = []
                    if self.site1_campaign_info:
                        all_campaign_info.extend(self.site1_campaign_info)
                    
                    with open(output_file, 'w', encoding='utf-8') as f:
                        f.write(f"Lần #{iteration} - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write("="*60 + "\n")
                        f.write("\n".join(all_campaign_info))
                    
                    self.log(f"📄 Đã lưu: {output_file}")
                    
                    # Gửi tin nhắn với thông tin campaign theo format mới
                    telegram_message = ""
                    
                    # Site 1
                    domain1 = urlparse(self.url1).netloc
                    telegram_message += f"{domain1}:\n"
                    if self.site1_campaign_info:
                        for campaign_line in self.site1_campaign_info:
                            telegram_message += f"{campaign_line}\n"
                    else:
                        telegram_message += "Không có thông tin campaign\n"
                    
                    self.send_telegram_message(telegram_message)
                else:
                    self.log("❌ Scrape thất bại")
                
                self.log(f"\n⏳ Đợi {self.refresh_interval//60} phút...")
                
                # Đợi với bộ đếm ngược
                for remaining in range(self.refresh_interval, 0, -1):
                    mins, secs = divmod(remaining, 60)
                    if secs == 0 or remaining <= 10:  # Hiển thị mỗi phút hoặc 10 giây cuối
                        self.log(f"   ⏱️  Còn lại: {mins:02d}:{secs:02d}", end='\r')
                    time.sleep(1)
                self.log("")  # Xuống dòng sau khi đếm xong
                
        except KeyboardInterrupt:
            self.log(f"\n⏹️  Đã dừng sau {iteration} lần")
        except Exception as e:
            self.log(f"\n❌ Lỗi: {e}")


if __name__ == "__main__":
    scraper = ServerScraper()
    
    # Kiểm tra xem có yêu cầu reset trạng thái không
    if len(sys.argv) > 1 and sys.argv[1] == '--reset':
        scraper.log("🔄 Chế độ reset trạng thái scraper...")
        if scraper.reset_scraper_state():
            scraper.log("✅ Reset trạng thái thành công!")
        else:
            scraper.log("❌ Reset trạng thái thất bại!")
        sys.exit(0)
    
    scraper.run()
