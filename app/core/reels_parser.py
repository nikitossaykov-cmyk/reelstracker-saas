"""
Парсер рилсов — миграция из reels_parser.py
Убран main(), save_to_json(), настройка логгера.
Класс ReelsParser используется из worker.
"""

import requests
import json
import time
import re
import random
import logging
import zipfile
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

logger = logging.getLogger(__name__)


def _decode_html(s):
    """Декодирует HTML entities (&#x1d5dd; → 𝗝 и т.д.)"""
    if not s:
        return s
    try:
        import html as _html
        return _html.unescape(s)
    except Exception:
        return s


def _parse_count_str(text):
    """'1.2M', '10K', '1,234' → int. Вспомогательная функция для парсинга числа из мета-тегов Instagram."""
    if not text:
        return None
    try:
        s = str(text).strip().upper().replace(',', '').replace(' ', '')
        multipliers = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}
        for suffix, mult in multipliers.items():
            if suffix in s:
                return int(float(s.replace(suffix, '')) * mult)
        return int(re.sub(r'[^\d]', '', s) or 0)
    except Exception:
        return None


class ReelsParser:
    def __init__(self, proxy=None, accounts_file=None):
        """
        Инициализация парсера.

        Args:
            proxy: строка прокси (host:port:user:pass)
            accounts_file: путь к файлу с аккаунтами Instagram
        """
        self.proxy_raw = proxy
        self.proxy = self._format_proxy(proxy) if proxy else None
        self.driver = None
        self.accounts = []
        self.current_account_idx = 0

        # Загрузка аккаунта из переменной окружения
        self._load_account_from_env()

        if accounts_file:
            self.load_accounts(accounts_file)
        self.setup_selenium()

    def _load_account_from_env(self):
        """Загрузка Instagram аккаунта из переменной окружения INSTAGRAM_COOKIES"""
        import os
        cookies_str = os.environ.get('INSTAGRAM_COOKIES', '')
        if not cookies_str:
            return

        try:
            cookies = {}
            for cookie in cookies_str.split(';'):
                if '=' in cookie:
                    key, value = cookie.split('=', 1)
                    cookies[key.strip()] = value.strip()

            if 'sessionid' in cookies:
                self.accounts.append({
                    'login': 'env_account',
                    'cookies': cookies
                })
                logger.info("Загружен Instagram аккаунт из INSTAGRAM_COOKIES")
        except Exception as e:
            logger.warning(f"Ошибка загрузки куки из ENV: {e}")

    def load_accounts(self, accounts_file):
        """Загрузка аккаунтов Instagram с куки"""
        try:
            from pathlib import Path
            if not Path(accounts_file).exists():
                logger.warning(f"Файл аккаунтов не найден: {accounts_file}")
                return

            with open(accounts_file, 'r') as f:
                lines = f.readlines()
            for line in lines:
                line = line.strip()
                if not line or '||' not in line:
                    continue
                try:
                    parts = line.split('||')
                    creds = parts[0]
                    cookies_part = parts[1] if len(parts) > 1 else ''
                    cookies = {}
                    if cookies_part:
                        for cookie in cookies_part.split(';'):
                            if '=' in cookie:
                                key, value = cookie.split('=', 1)
                                cookies[key.strip()] = value.strip()
                    if 'sessionid' in cookies:
                        self.accounts.append({
                            'login': creds.split(':')[0] if ':' in creds else creds,
                            'cookies': cookies
                        })
                except Exception:
                    continue

            if self.accounts:
                logger.info(f"Загружено {len(self.accounts)} Instagram аккаунтов")
        except Exception as e:
            logger.warning(f"Ошибка загрузки аккаунтов: {e}")

    def get_next_account(self):
        """Получить следующий аккаунт для ротации"""
        if not self.accounts:
            return None
        account = self.accounts[self.current_account_idx]
        self.current_account_idx = (self.current_account_idx + 1) % len(self.accounts)
        return account

    def _shortcode_to_media_id(self, shortcode):
        """Конвертация shortcode Instagram в media_id"""
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        media_id = 0
        for char in shortcode:
            media_id = media_id * 64 + alphabet.index(char)
        return str(media_id)

    def _format_proxy(self, proxy_string):
        """Конвертация прокси в формат http://user:pass@host:port"""
        if not proxy_string:
            return None
        if proxy_string.startswith('http://') or proxy_string.startswith('https://'):
            return proxy_string
        parts = proxy_string.split(':')
        if len(parts) == 4:
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
        elif len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        else:
            logger.warning(f"Неизвестный формат прокси: {proxy_string}")
            return proxy_string

    def _get_proxy_extension(self):
        """Создание Chrome расширения для прокси с авторизацией"""
        if not self.proxy_raw:
            return None
        parts = self.proxy_raw.split(':')
        if len(parts) != 4:
            return None
        host, port, user, password = parts

        manifest_json = """
        {
            "version": "1.0.0",
            "manifest_version": 2,
            "name": "Chrome Proxy",
            "permissions": [
                "proxy", "tabs", "unlimitedStorage", "storage",
                "<all_urls>", "webRequest", "webRequestBlocking"
            ],
            "background": {"scripts": ["background.js"]},
            "minimum_chrome_version":"22.0.0"
        }
        """

        background_js = """
        var config = {
            mode: "fixed_servers",
            rules: {
                singleProxy: {scheme: "http", host: "%s", port: parseInt(%s)},
                bypassList: ["localhost"]
            }
        };
        chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});
        function callbackFn(details) {
            return {authCredentials: {username: "%s", password: "%s"}};
        }
        chrome.webRequest.onAuthRequired.addListener(
            callbackFn, {urls: ["<all_urls>"]}, ['blocking']
        );
        """ % (host, port, user, password)

        pluginfile = '/tmp/proxy_auth_plugin.zip'
        with zipfile.ZipFile(pluginfile, 'w') as zp:
            zp.writestr("manifest.json", manifest_json)
            zp.writestr("background.js", background_js)
        return pluginfile

    def setup_selenium(self):
        """Настройка Selenium для парсинга с прокси"""
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        ]
        chrome_options.add_argument(f'user-agent={random.choice(user_agents)}')

        if self.proxy_raw and len(self.proxy_raw.split(':')) == 4:
            proxy_extension = self._get_proxy_extension()
            if proxy_extension:
                chrome_options.add_extension(proxy_extension)
                logger.info("Selenium: прокси расширение загружено")

        try:
            from selenium.webdriver.chrome.service import Service
            import shutil
            import os
            from pathlib import Path

            # 1) Явные пути из ENV (из app/config.py) имеют высший приоритет
            env_chrome = os.environ.get('CHROME_BINARY_PATH', '').strip()
            env_driver = os.environ.get('CHROMEDRIVER_PATH', '').strip()

            # 2) Ищем Chrome: ENV → PATH → Mac .app → Linux стандарт
            mac_chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            chrome_binary = (
                env_chrome
                or shutil.which('google-chrome')
                or shutil.which('google-chrome-stable')
                or (mac_chrome if Path(mac_chrome).exists() else None)
                or '/usr/bin/google-chrome'
            )

            # 3) Ищем chromedriver: ENV → PATH → webdriver-manager (скачивает сам)
            chromedriver_path = env_driver or shutil.which('chromedriver')
            if not chromedriver_path:
                try:
                    from webdriver_manager.chrome import ChromeDriverManager
                    chromedriver_path = ChromeDriverManager().install()
                except Exception as wdm_err:
                    logger.warning(f"webdriver-manager не сработал: {wdm_err}")
                    chromedriver_path = '/usr/local/bin/chromedriver'

            chrome_options.binary_location = chrome_binary
            service = Service(executable_path=chromedriver_path)

            self.driver = webdriver.Chrome(service=service, options=chrome_options)
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            logger.info(f"Selenium успешно инициализирован (chrome: {chrome_binary}, driver: {chromedriver_path})")
        except Exception as e:
            logger.error(f"Ошибка инициализации Selenium: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self.driver = None

    def parse_instagram(self, url):
        """Парсинг Instagram Reels с авторизацией через куки"""
        try:
            logger.info(f"Парсинг Instagram: {url}")
            shortcode_match = re.search(r'/reel/([^/?]+)', url)
            if not shortcode_match:
                raise ValueError("Не удалось извлечь shortcode из URL")

            shortcode = shortcode_match.group(1)
            media_id = self._shortcode_to_media_id(shortcode)

            metrics = {
                'views': 0, 'likes': 0, 'comments': 0, 'shares': 0,
                'thumbnail_url': None,
                'author_username': None,
                'author_full_name': None,
                'published_at': None,           # ISO-строка (парсится на стороне worker)
                'caption': None,                # текст подписи
                'duration_seconds': None,       # float
                'timestamp': datetime.now().isoformat()
            }

            def _pick_thumbnail_from_graphql(media):
                """Извлечь URL обложки из GraphQL shortcode_media"""
                return media.get('thumbnail_src') or media.get('display_url')

            def _pick_thumbnail_from_mobile(item):
                """Извлечь URL обложки из Mobile API item"""
                iv = item.get('image_versions2', {}) or {}
                candidates = iv.get('candidates', []) or []
                if candidates:
                    return candidates[0].get('url')
                return None

            def _pick_caption_from_graphql(media):
                edges = (media.get('edge_media_to_caption', {}) or {}).get('edges', []) or []
                if edges:
                    return (edges[0].get('node', {}) or {}).get('text')
                return None

            def _pick_caption_from_mobile(item):
                cap = item.get('caption')
                if isinstance(cap, dict):
                    return cap.get('text')
                return None

            def _ts_to_iso(ts):
                """UNIX timestamp → ISO string (UTC)"""
                if ts is None:
                    return None
                try:
                    return datetime.utcfromtimestamp(int(ts)).isoformat()
                except Exception:
                    return None

            # Метод 0: GraphQL API через web endpoint
            try:
                # Пробуем получить данные через graphql query
                graphql_url = "https://www.instagram.com/graphql/query/"
                variables = {"shortcode": shortcode}
                params = {
                    "query_hash": "b3055c01b4b222b8a47dc12b090e4e64",  # media query hash
                    "variables": json.dumps(variables)
                }
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'X-IG-App-ID': '936619743392459',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': f'https://www.instagram.com/reel/{shortcode}/',
                }
                proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
                response = requests.get(graphql_url, params=params, headers=headers, proxies=proxies, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    media = data.get('data', {}).get('shortcode_media', {})
                    if media:
                        metrics['views'] = media.get('video_view_count', 0) or media.get('play_count', 0) or 0
                        metrics['likes'] = media.get('edge_media_preview_like', {}).get('count', 0)
                        metrics['comments'] = media.get('edge_media_to_comment', {}).get('count', 0) or media.get('edge_media_to_parent_comment', {}).get('count', 0)
                        # Метаданные (обложка, автор, пост-метаданные)
                        metrics['thumbnail_url'] = _pick_thumbnail_from_graphql(media)
                        owner = media.get('owner', {}) or {}
                        metrics['author_username'] = owner.get('username')
                        metrics['author_full_name'] = owner.get('full_name')
                        metrics['published_at'] = _ts_to_iso(media.get('taken_at_timestamp'))
                        metrics['caption'] = _pick_caption_from_graphql(media)
                        vd = media.get('video_duration')
                        metrics['duration_seconds'] = float(vd) if vd else None
                        logger.info(f"GraphQL web метрики: views={metrics['views']}, likes={metrics['likes']}, author=@{metrics['author_username']}")
                        if metrics['views'] > 0 or metrics['likes'] > 0:
                            return metrics
                else:
                    logger.debug(f"GraphQL web вернул {response.status_code}")
            except Exception as e:
                logger.debug(f"GraphQL web метод не сработал: {e}")

            # Метод 0.5: Mobile API
            try:
                api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
                headers = {
                    'User-Agent': 'Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100)',
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'X-IG-App-ID': '567067343352427',  # Android App ID
                    'X-IG-Device-ID': 'android-1234567890abcdef',
                    'X-IG-Connection-Type': 'WIFI',
                    'X-IG-Capabilities': '3brTvw8=',
                }
                proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
                response = requests.get(api_url, headers=headers, proxies=proxies, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    items = data.get('items', [])
                    if items:
                        item = items[0]
                        views = item.get('play_count', 0) or item.get('view_count', 0) or item.get('video_view_count', 0) or 0
                        if views > 0:
                            metrics['views'] = views
                        metrics['likes'] = item.get('like_count', 0) or metrics['likes']
                        metrics['comments'] = item.get('comment_count', 0) or metrics['comments']
                        metrics['shares'] = item.get('reshare_count', 0) or item.get('share_count', 0) or 0
                        # Метаданные
                        if not metrics['thumbnail_url']:
                            metrics['thumbnail_url'] = _pick_thumbnail_from_mobile(item)
                        user = item.get('user', {}) or {}
                        metrics['author_username'] = metrics['author_username'] or user.get('username')
                        metrics['author_full_name'] = metrics['author_full_name'] or user.get('full_name')
                        if not metrics['published_at']:
                            metrics['published_at'] = _ts_to_iso(item.get('taken_at'))
                        if not metrics['caption']:
                            metrics['caption'] = _pick_caption_from_mobile(item)
                        if not metrics['duration_seconds']:
                            vd = item.get('video_duration')
                            metrics['duration_seconds'] = float(vd) if vd else None
                        logger.info(f"Mobile API метрики: views={metrics['views']}, likes={metrics['likes']}, shares={metrics['shares']}, author=@{metrics['author_username']}")
                        if metrics['views'] > 0:
                            return metrics
                else:
                    logger.debug(f"Mobile API вернул {response.status_code}")
            except Exception as e:
                logger.debug(f"Mobile API метод не сработал: {e}")

            # Метод 1: API с куками
            account = self.get_next_account()
            if account:
                try:
                    api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
                    headers = {
                        'User-Agent': 'Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100)',
                        'Accept': '*/*',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'X-IG-App-ID': '936619743392459',
                        'X-IG-Device-ID': 'android-1234567890',
                        'X-IG-Connection-Type': 'WIFI',
                        'X-ASBD-ID': '129477',
                    }
                    cookies = account['cookies']
                    if 'Authorization' in cookies:
                        headers['Authorization'] = cookies['Authorization']
                    for key in ['X-IG-WWW-Claim', 'X-MID', 'IG-U-DS-USER-ID', 'IG-U-RUR']:
                        if key in cookies:
                            headers[key] = cookies[key]

                    proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
                    response = requests.get(
                        api_url, headers=headers,
                        cookies={k: v for k, v in cookies.items() if k in ['sessionid', 'csrftoken', 'ds_user_id', 'rur', 'mid']},
                        proxies=proxies, timeout=20
                    )

                    if response.status_code == 200:
                        data = response.json()
                        items = data.get('items', [])
                        if items:
                            item = items[0]
                            metrics['views'] = item.get('play_count', 0) or item.get('ig_play_count', 0) or item.get('view_count', 0) or 0
                            metrics['likes'] = item.get('like_count', 0)
                            metrics['comments'] = item.get('comment_count', 0)
                            metrics['shares'] = item.get('reshare_count', 0)
                            # Метаданные
                            if not metrics['thumbnail_url']:
                                metrics['thumbnail_url'] = _pick_thumbnail_from_mobile(item)
                            user = item.get('user', {}) or {}
                            metrics['author_username'] = metrics['author_username'] or user.get('username')
                            metrics['author_full_name'] = metrics['author_full_name'] or user.get('full_name')
                            if not metrics['published_at']:
                                metrics['published_at'] = _ts_to_iso(item.get('taken_at'))
                            if not metrics['caption']:
                                metrics['caption'] = _pick_caption_from_mobile(item)
                            if not metrics['duration_seconds']:
                                vd = item.get('video_duration')
                                metrics['duration_seconds'] = float(vd) if vd else None
                            logger.info(f"API метрики: views={metrics['views']}, likes={metrics['likes']}, author=@{metrics['author_username']}")
                            if metrics['views'] > 0 or metrics['likes'] > 0:
                                return metrics
                except Exception as e:
                    logger.warning(f"API метод не сработал: {e}")

            # Метод 1.5: oEmbed API (публичный, возвращает базовые данные)
            try:
                oembed_url = f"https://www.instagram.com/api/v1/oembed/?url=https://www.instagram.com/reel/{shortcode}/"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                }
                proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
                response = requests.get(oembed_url, headers=headers, proxies=proxies, timeout=10)
                if response.status_code == 200:
                    # oEmbed не даёт метрик, но подтверждает что рилс существует
                    logger.debug("oEmbed: рилс доступен")
            except Exception as e:
                logger.debug(f"oEmbed не сработал: {e}")

            # Метод 2: Selenium fallback
            if not self.driver:
                raise Exception("Selenium не инициализирован")

            self.driver.get("https://www.instagram.com/")
            time.sleep(2)

            if account:
                for name, value in account['cookies'].items():
                    if name in ['sessionid', 'csrftoken', 'ds_user_id', 'mid', 'ig_did', 'rur']:
                        try:
                            self.driver.add_cookie({
                                'name': name, 'value': value,
                                'domain': '.instagram.com', 'path': '/'
                            })
                        except:
                            pass

            reel_url = f"https://www.instagram.com/reel/{shortcode}/"
            self.driver.get(reel_url)
            time.sleep(5)

            page_source = self.driver.page_source

            # Паттерны для поиска в JSON внутри page_source
            patterns = {
                'views': [r'"video_view_count":(\d+)', r'"play_count":(\d+)', r'"view_count":(\d+)', r'"ig_play_count":(\d+)'],
                'likes': [r'"like_count":(\d+)', r'"edge_media_preview_like":\{"count":(\d+)'],
                'comments': [r'"comment_count":(\d+)', r'"edge_media_to_comment":\{"count":(\d+)'],
                'shares': [r'"reshare_count":(\d+)', r'"share_count":(\d+)'],
            }

            for metric_name, metric_patterns in patterns.items():
                if metrics[metric_name] > 0:
                    continue
                for pattern in metric_patterns:
                    match = re.search(pattern, page_source)
                    if match:
                        metrics[metric_name] = int(match.group(1))
                        logger.info(f"Найдено {metric_name}={metrics[metric_name]} через паттерн {pattern}")
                        break

            # Метод 3: Поиск в DOM элементах (Instagram показывает views/plays визуально)
            if metrics['views'] == 0:
                try:
                    # Instagram показывает просмотры рядом с видео
                    view_selectors = [
                        'span[class*="views"]',
                        'span[class*="play"]',
                        'div[class*="views"] span',
                        'section span',  # Часто views в секции под видео
                    ]
                    for selector in view_selectors:
                        try:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                            for el in elements:
                                text = el.text.strip()
                                # Ищем текст типа "1.2M views" или "1,234 plays"
                                if text and any(kw in text.lower() for kw in ['view', 'play', 'просмотр']):
                                    num = self._parse_metric_text(text.split()[0])
                                    if num > 0:
                                        metrics['views'] = num
                                        logger.info(f"Найдено views={num} через DOM selector {selector}")
                                        break
                        except:
                            continue
                        if metrics['views'] > 0:
                            break
                except Exception as e:
                    logger.debug(f"DOM поиск views не сработал: {e}")

            # Поиск лайков в DOM если не нашли в JSON
            if metrics['likes'] == 0:
                try:
                    like_selectors = [
                        'section span[class*="like"]',
                        'button[aria-label*="like"] span',
                        'span[class*="like"]',
                    ]
                    for selector in like_selectors:
                        try:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                            for el in elements:
                                text = el.text.strip()
                                if text and re.match(r'^[\d,.KMB]+$', text, re.IGNORECASE):
                                    num = self._parse_metric_text(text)
                                    if num > 0:
                                        metrics['likes'] = num
                                        logger.info(f"Найдено likes={num} через DOM")
                                        break
                        except:
                            continue
                        if metrics['likes'] > 0:
                            break
                except Exception as e:
                    logger.debug(f"DOM поиск likes не сработал: {e}")

            # Ищем в __additionalDataLoaded или другие скрипты с данными
            if metrics['views'] == 0:
                try:
                    script_patterns = [
                        r'video_view_count["\s:]+(\d+)',
                        r'playCount["\s:]+(\d+)',
                        r'"viewCount"["\s:]+(\d+)',
                        r'views["\s:]+(\d+)',
                    ]
                    for pattern in script_patterns:
                        matches = re.findall(pattern, page_source, re.IGNORECASE)
                        if matches:
                            # Берём максимальное значение (реальные просмотры обычно больше)
                            max_views = max(int(m) for m in matches)
                            if max_views > metrics['views']:
                                metrics['views'] = max_views
                                logger.info(f"Найдено views={max_views} через расширенный regex")
                                break
                except Exception as e:
                    logger.debug(f"Расширенный поиск views не сработал: {e}")

            if metrics['views'] > 0 or metrics['likes'] > 0:
                logger.info(f"Instagram метрики: views={metrics['views']}, likes={metrics['likes']}, comments={metrics['comments']}, shares={metrics['shares']}")
                return metrics
            else:
                logger.warning("Instagram: не удалось получить метрики")
                return None

        except Exception as e:
            logger.error(f"Ошибка парсинга Instagram: {e}")
            return None

    def parse_tiktok(self, url):
        """Парсинг TikTok"""
        try:
            logger.info(f"Парсинг TikTok: {url}")
            if not self.driver:
                raise Exception("Selenium не инициализирован")
            self.driver.get(url)
            time.sleep(5)
            metrics = {
                'views': self._extract_tiktok_metric('view'),
                'likes': self._extract_tiktok_metric('like'),
                'comments': self._extract_tiktok_metric('comment'),
                'shares': self._extract_tiktok_metric('share'),
                'timestamp': datetime.now().isoformat()
            }
            logger.info(f"TikTok метрики: {metrics}")
            return metrics
        except Exception as e:
            logger.error(f"Ошибка парсинга TikTok: {e}")
            return None

    def _extract_tiktok_metric(self, metric_type):
        """Извлечение метрики из TikTok"""
        try:
            selectors = {
                'view': ['[data-e2e="video-views"]', '[data-e2e="browse-video-views"]'],
                'like': ['[data-e2e="like-count"]', '[data-e2e="browse-like-count"]'],
                'comment': ['[data-e2e="comment-count"]', '[data-e2e="browse-comment-count"]'],
                'share': ['[data-e2e="share-count"]', '[data-e2e="browse-share-count"]']
            }
            for selector in selectors.get(metric_type, []):
                try:
                    element = self.driver.find_element(By.CSS_SELECTOR, selector)
                    return self._parse_metric_text(element.text.strip())
                except:
                    continue
            return 0
        except:
            return 0

    def parse_youtube_shorts(self, url):
        """Парсинг YouTube Shorts"""
        try:
            logger.info(f"Парсинг YouTube Shorts: {url}")
            if not self.driver:
                raise Exception("Selenium не инициализирован")
            self.driver.get(url)
            time.sleep(3)
            metrics = {
                'views': self._extract_youtube_views(),
                'likes': self._extract_youtube_likes(),
                'comments': self._extract_youtube_comments(),
                'shares': 0,
                'timestamp': datetime.now().isoformat()
            }
            logger.info(f"YouTube метрики: {metrics}")
            return metrics
        except Exception as e:
            logger.error(f"Ошибка парсинга YouTube: {e}")
            return None

    def _extract_youtube_views(self):
        try:
            for selector in ['span.view-count', 'yt-formatted-string.ytd-video-view-count-renderer']:
                try:
                    element = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if 'view' in element.text.lower():
                        return self._parse_metric_text(element.text.split()[0])
                except:
                    continue
            return 0
        except:
            return 0

    def _extract_youtube_likes(self):
        try:
            like_button = self.driver.find_element(By.CSS_SELECTOR, 'button[aria-label*="like"]')
            text = like_button.get_attribute('aria-label')
            numbers = re.findall(r'\d+', text)
            return int(numbers[0]) if numbers else 0
        except:
            return 0

    def _extract_youtube_comments(self):
        try:
            comments_section = self.driver.find_element(By.CSS_SELECTOR, 'h2#count yt-formatted-string')
            return self._parse_metric_text(comments_section.text.split()[0])
        except:
            return 0

    def parse_vk(self, url):
        """Парсинг VK Клипов"""
        try:
            logger.info(f"Парсинг VK: {url}")
            if not self.driver:
                raise Exception("Selenium не инициализирован")
            self.driver.get(url)
            time.sleep(4)
            metrics = {
                'views': self._extract_vk_metric('views'),
                'likes': self._extract_vk_metric('likes'),
                'comments': self._extract_vk_metric('comments'),
                'shares': self._extract_vk_metric('shares'),
                'timestamp': datetime.now().isoformat()
            }
            logger.info(f"VK метрики: {metrics}")
            return metrics
        except Exception as e:
            logger.error(f"Ошибка парсинга VK: {e}")
            return None

    def _extract_vk_metric(self, metric_type):
        try:
            selectors = {
                'views': ['.VideoCard__views', '.views_count'],
                'likes': ['.VideoCard__likes', '.like_count'],
                'comments': ['.VideoCard__comments', '.comments_count'],
                'shares': ['.VideoCard__shares', '.share_count']
            }
            for selector in selectors.get(metric_type, []):
                try:
                    element = self.driver.find_element(By.CSS_SELECTOR, selector)
                    return self._parse_metric_text(element.text)
                except:
                    continue
            return 0
        except:
            return 0

    def _parse_metric_text(self, text):
        """Преобразование текста метрики в число (1.2M -> 1200000)"""
        try:
            text = text.strip().upper().replace(',', '').replace(' ', '')
            multipliers = {'K': 1000, 'M': 1000000, 'B': 1000000000}
            for suffix, multiplier in multipliers.items():
                if suffix in text:
                    number = float(text.replace(suffix, ''))
                    return int(number * multiplier)
            return int(re.sub(r'[^\d]', '', text))
        except:
            return 0

    def parse_reel(self, url, platform):
        """Универсальный метод парсинга"""
        platform = platform.lower()
        if platform == 'instagram':
            return self.parse_instagram(url)
        elif platform == 'tiktok':
            return self.parse_tiktok(url)
        elif platform == 'youtube':
            return self.parse_youtube_shorts(url)
        elif platform == 'vk':
            return self.parse_vk(url)
        else:
            logger.error(f"Неизвестная платформа: {platform}")
            return None

    # ────────────────────────────────────────────────────────────────
    # ACCOUNT / PROFILE PARSING
    # ────────────────────────────────────────────────────────────────

    def fetch_instagram_profile(self, username):
        """Получить данные профиля Instagram по username.

        Возвращает dict или None. Поля: instagram_user_id, full_name, profile_pic_url,
        bio, followers_count, following_count, posts_count.
        """
        username = (username or '').strip().lstrip('@')
        if not username:
            return None

        # Запомним username — пригодится Selenium-fallback'у fetch_instagram_reels_list
        self._last_profile_username = username

        logger.info(f"Загружаю профиль Instagram @{username}")
        url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6.1 Mobile/15E148 Safari/604.1',
            'Accept': '*/*',
            'X-IG-App-ID': '936619743392459',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'https://www.instagram.com/{username}/',
        }
        proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None

        # Попытка 1: публичный web_profile_info
        try:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=20)
            if response.status_code == 200:
                data = response.json().get('data', {}).get('user', {}) or {}
                if data:
                    return {
                        'instagram_user_id': str(data.get('id') or ''),
                        'username': data.get('username') or username,
                        'full_name': data.get('full_name'),
                        'profile_pic_url': data.get('profile_pic_url_hd') or data.get('profile_pic_url'),
                        'bio': data.get('biography'),
                        'followers_count': (data.get('edge_followed_by') or {}).get('count'),
                        'following_count': (data.get('edge_follow') or {}).get('count'),
                        'posts_count': (data.get('edge_owner_to_timeline_media') or {}).get('count'),
                    }
        except Exception as e:
            logger.warning(f"web_profile_info не сработал: {e}")

        # Попытка 2: итерация по куки-аккаунтам (до 5 штук)
        for _ in range(min(5, len(self.accounts) or 0)):
            account = self.get_next_account()
            if not account:
                break
            try:
                cookies = account['cookies']
                response = requests.get(
                    url, headers=headers,
                    cookies={k: v for k, v in cookies.items() if k in ['sessionid', 'csrftoken', 'ds_user_id', 'rur', 'mid']},
                    proxies=proxies, timeout=20
                )
                if response.status_code == 200:
                    data = response.json().get('data', {}).get('user', {}) or {}
                    if data:
                        logger.info(f"Профиль @{username} получен через web_profile_info + cookies")
                        return {
                            'instagram_user_id': str(data.get('id') or ''),
                            'username': data.get('username') or username,
                            'full_name': data.get('full_name'),
                            'profile_pic_url': data.get('profile_pic_url_hd') or data.get('profile_pic_url'),
                            'bio': data.get('biography'),
                            'followers_count': (data.get('edge_followed_by') or {}).get('count'),
                            'following_count': (data.get('edge_follow') or {}).get('count'),
                            'posts_count': (data.get('edge_owner_to_timeline_media') or {}).get('count'),
                        }
            except Exception as e:
                logger.warning(f"web_profile_info (с куки) не сработал: {e}")
                continue

        # Попытка 3: HTML-страница профиля (извлечь instagram_user_id из инлайн-JSON)
        # Пробуем несколько UA — IG отдаёт разный HTML
        page_url = f"https://www.instagram.com/{username}/"
        ua_variants = [
            'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        ]
        html = None
        for ua in ua_variants:
            try:
                html_headers = {
                    'User-Agent': ua,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-User': '?1',
                    'Sec-Fetch-Dest': 'document',
                }
                r = requests.get(page_url, headers=html_headers, proxies=proxies, timeout=20, allow_redirects=True)
                logger.info(f"HTML scrape @{username}: UA={ua[:40]}... → status={r.status_code} len={len(r.text or '')}")
                if r.status_code == 200 and r.text:
                    if '"user_id":"' in r.text or '"profile_id":"' in r.text or 'og:image' in r.text:
                        html = r.text
                        break
            except Exception as e:
                logger.warning(f"HTML scrape UA={ua[:30]}: {e}")
                continue

        if html:
            try:
                import re as _re
                m_id = _re.search(r'"profile_id":"(\d+)"', html) or _re.search(r'"user_id":"(\d+)"', html) or _re.search(r'"id":"(\d{6,})"', html)
                m_img = _re.search(r'<meta property="og:image" content="([^"]+)"', html)
                m_title = _re.search(r'<meta property="og:title" content="([^"]+)"', html)
                m_desc = _re.search(r'<meta property="og:description" content="([^"]+)"', html)
                if m_id:
                    full_name = None
                    if m_title:
                        raw = _decode_html(m_title.group(1))
                        full_name = _re.sub(r'\s*\(@[^)]+\).*', '', raw).strip() or None
                    followers = None
                    if m_desc:
                        mf = _re.search(r'([\d,.]+[KMB]?)\s+Followers', m_desc.group(1))
                        if mf:
                            followers = _parse_count_str(mf.group(1))
                    logger.info(f"Профиль @{username} получен через HTML (id={m_id.group(1)})")
                    return {
                        'instagram_user_id': m_id.group(1),
                        'username': username,
                        'full_name': full_name,
                        'profile_pic_url': m_img.group(1) if m_img else None,
                        'bio': None,
                        'followers_count': followers,
                        'following_count': None,
                        'posts_count': None,
                    }
            except Exception as e:
                logger.warning(f"HTML profile parse не сработал: {e}")

        # Попытка 4: Selenium — открыть страницу профиля и извлечь HTML
        if self.driver:
            try:
                logger.info(f"Пробую Selenium для профиля @{username}")
                self.driver.get(page_url)
                time.sleep(3)
                html = self.driver.page_source or ''
                import re as _re
                m_id = _re.search(r'"profile_id":"(\d+)"', html) or _re.search(r'"user_id":"(\d+)"', html) or _re.search(r'"id":"(\d{6,})"', html)
                m_img = _re.search(r'<meta property="og:image" content="([^"]+)"', html)
                m_title = _re.search(r'<meta property="og:title" content="([^"]+)"', html)
                m_desc = _re.search(r'<meta property="og:description" content="([^"]+)"', html)
                if m_id:
                    full_name = None
                    if m_title:
                        raw = _decode_html(m_title.group(1))
                        full_name = _re.sub(r'\s*\(@[^)]+\).*', '', raw).strip() or None
                    followers = None
                    if m_desc:
                        mf = _re.search(r'([\d,.]+[KMB]?)\s+Followers', m_desc.group(1))
                        if mf:
                            followers = _parse_count_str(mf.group(1))
                    logger.info(f"Профиль @{username} получен через Selenium (id={m_id.group(1)})")
                    return {
                        'instagram_user_id': m_id.group(1),
                        'username': username,
                        'full_name': full_name,
                        'profile_pic_url': m_img.group(1) if m_img else None,
                        'bio': None,
                        'followers_count': followers,
                        'following_count': None,
                        'posts_count': None,
                    }
            except Exception as e:
                logger.warning(f"Selenium profile не сработал: {e}")

        logger.warning(f"Не удалось получить профиль @{username}")
        return None

    def fetch_profile_via_apify(self, username, apify_token):
        """Получить профиль (full_name, profile_pic_url, followers и т.д.) через
        apify~instagram-profile-scraper. Возвращает dict как fetch_instagram_profile,
        либо None.
        """
        if not apify_token or not username:
            return None
        username = username.strip().lstrip('@')

        actor_id = "apify~instagram-profile-scraper"
        start_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?token={apify_token}"
        payload = {"usernames": [username]}

        try:
            logger.info(f"Apify: стартую {actor_id} для @{username}")
            start_resp = requests.post(start_url, json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
            if start_resp.status_code >= 400:
                logger.warning(f"Apify profile start вернул {start_resp.status_code}: {start_resp.text[:200]}")
                return None
            data = start_resp.json().get('data', {}) or {}
            run_id = data.get('id')
            dataset_id = data.get('defaultDatasetId')
            if not run_id or not dataset_id:
                return None

            status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={apify_token}"
            import time as _t
            deadline = _t.time() + 60
            final_status = None
            while _t.time() < deadline:
                _t.sleep(2)
                try:
                    sr = requests.get(status_url, timeout=10)
                    if sr.status_code != 200:
                        continue
                    st = sr.json().get('data', {}).get('status')
                    if st in ('SUCCEEDED', 'FAILED', 'TIMED-OUT', 'ABORTED'):
                        final_status = st
                        break
                except Exception:
                    continue
            if final_status != 'SUCCEEDED':
                logger.warning(f"Apify profile run: {final_status}")
                return None

            items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={apify_token}&clean=true&format=json"
            ir = requests.get(items_url, timeout=30)
            if ir.status_code != 200:
                return None
            items = ir.json() or []
            if not items:
                return None
            it = items[0]
            return {
                'instagram_user_id': str(it.get('id') or ''),
                'username': it.get('username') or username,
                'full_name': it.get('fullName'),
                'profile_pic_url': it.get('profilePicUrlHD') or it.get('profilePicUrl'),
                'bio': it.get('biography'),
                'followers_count': it.get('followersCount'),
                'following_count': it.get('followsCount'),
                'posts_count': it.get('postsCount'),
            }
        except Exception as e:
            logger.warning(f"Apify profile запрос не сработал: {e}")
            return None

    def fetch_reels_via_apify(self, username, apify_token, results_limit=100):
        """Получить рилсы аккаунта через Apify actor apify/instagram-post-scraper.

        Async-флоу: запускаем run → polling статуса → получаем dataset items.
        Возвращает список dict того же формата, что fetch_instagram_reels_list.
        """
        if not apify_token or not username:
            return []
        username = username.strip().lstrip('@')

        actor_id = "apify~instagram-post-scraper"
        start_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?token={apify_token}"
        payload = {
            "username": [username],
            "resultsLimit": int(results_limit),
        }

        try:
            logger.info(f"Apify: стартую {actor_id} для @{username} (limit={results_limit})")
            start_resp = requests.post(start_url, json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
            if start_resp.status_code >= 400:
                logger.warning(f"Apify start вернул {start_resp.status_code}: {start_resp.text[:300]}")
                return []
            run_info = start_resp.json().get('data', {}) or {}
            run_id = run_info.get('id')
            dataset_id = run_info.get('defaultDatasetId')
            if not run_id or not dataset_id:
                logger.warning(f"Apify: нет run_id/dataset_id: {run_info}")
                return []
            logger.info(f"Apify: run {run_id} стартовал, dataset={dataset_id}")

            # Polling статуса: максимум 120 секунд с короткими интервалами
            status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={apify_token}"
            import time as _t
            deadline = _t.time() + 120
            final_status = None
            while _t.time() < deadline:
                _t.sleep(3)
                try:
                    sr = requests.get(status_url, timeout=10)
                    if sr.status_code != 200:
                        continue
                    st = sr.json().get('data', {}).get('status')
                    if st in ('SUCCEEDED', 'FAILED', 'TIMED-OUT', 'ABORTED'):
                        final_status = st
                        logger.info(f"Apify: run {run_id} → {st}")
                        break
                except Exception:
                    continue
            if final_status != 'SUCCEEDED':
                logger.warning(f"Apify run не завершился успешно: {final_status}")
                return []

            # Получаем items
            items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={apify_token}&clean=true&format=json"
            ir = requests.get(items_url, timeout=30)
            if ir.status_code != 200:
                logger.warning(f"Apify items вернул {ir.status_code}")
                return []
            items = ir.json() or []
            logger.info(f"Apify отдал {len(items)} item'ов")

            results = []
            for it in items:
                # Типичная схема apify/instagram-scraper:
                # type=Video|Image|Sidecar, shortCode, url, displayUrl, videoViewCount, videoPlayCount,
                # likesCount, commentsCount, timestamp, videoDuration, caption, ownerUsername, ownerFullName, productType
                sc = it.get('shortCode') or it.get('code')
                if not sc:
                    continue
                # Берём только видео/рилсы
                product_type = (it.get('productType') or '').lower()
                is_reel = product_type == 'clips' or it.get('type') == 'Video' or it.get('videoUrl')
                if not is_reel:
                    continue

                ts = it.get('timestamp')
                pub_iso = None
                if ts:
                    try:
                        # timestamp бывает ISO-строкой
                        if isinstance(ts, str):
                            pub_iso = datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None).isoformat()
                        else:
                            pub_iso = datetime.utcfromtimestamp(int(ts)).isoformat()
                    except Exception:
                        pub_iso = None

                results.append({
                    'shortcode': sc,
                    'url': it.get('url') or f"https://www.instagram.com/reel/{sc}/",
                    'thumbnail_url': it.get('displayUrl') or it.get('thumbnailSrc'),
                    'caption': it.get('caption'),
                    'views': int(it.get('videoPlayCount') or it.get('videoViewCount') or 0),
                    'likes': int(it.get('likesCount') or 0),
                    'comments': int(it.get('commentsCount') or 0),
                    'published_at': pub_iso,
                    'duration_seconds': float(it.get('videoDuration')) if it.get('videoDuration') else None,
                    # Прямая ссылка .mp4 на IG-CDN. Протухает за часы — если хотим
                    # скачать в наш R2, дёргаем сразу при sync (см. media_service).
                    'video_url': it.get('videoUrl') or it.get('video_url'),
                })
            logger.info(f"Apify: отфильтровано {len(results)} рилсов")
            return results
        except Exception as e:
            logger.warning(f"Apify запрос не сработал: {e}")
            return []

    def fetch_instagram_reels_list(self, instagram_user_id, max_pages=5, username=None):
        """Получить список рилсов/видео с аккаунта через clips/user API.

        Возвращает список dict с полями: shortcode, url, thumbnail_url, caption, views,
        likes, comments, published_at (iso), duration_seconds.

        max_pages — сколько страниц подгрузить (каждая ~12 роликов).
        """
        if not instagram_user_id:
            return []

        logger.info(f"Загружаю рилсы аккаунта user_id={instagram_user_id}")
        results = []
        seen_shortcodes = set()

        # Источник 1: /api/v1/clips/user/ (только reels)
        url = "https://i.instagram.com/api/v1/clips/user/"
        max_id = None

        for page in range(max_pages):
            payload = {
                'target_user_id': str(instagram_user_id),
                'page_size': '12',
                'include_feed_video': 'true',
            }
            if max_id:
                payload['max_id'] = max_id

            headers = {
                'User-Agent': 'Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100)',
                'X-IG-App-ID': '567067343352427',
                'X-IG-Connection-Type': 'WIFI',
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None

            account = self.get_next_account()
            cookies = {}
            if account:
                cookies = {k: v for k, v in account['cookies'].items() if k in ['sessionid', 'csrftoken', 'ds_user_id', 'rur', 'mid']}

            try:
                response = requests.post(url, data=payload, headers=headers, cookies=cookies or None, proxies=proxies, timeout=25)
                if response.status_code != 200:
                    logger.warning(f"clips/user вернул {response.status_code} на странице {page}")
                    break

                data = response.json()
                items = data.get('items', []) or []
                if not items:
                    break

                for it in items:
                    media = it.get('media') or it
                    sc = media.get('code') or media.get('shortcode')
                    if not sc or sc in seen_shortcodes:
                        continue
                    seen_shortcodes.add(sc)

                    # Обложка
                    iv = media.get('image_versions2') or {}
                    cands = iv.get('candidates') or []
                    thumb = cands[0].get('url') if cands else None

                    # Caption
                    cap_obj = media.get('caption') or {}
                    caption = cap_obj.get('text') if isinstance(cap_obj, dict) else None

                    # Метрики (могут быть 0 — обновим на отдельном парсинге)
                    views = media.get('play_count') or media.get('ig_play_count') or media.get('view_count') or 0
                    likes = media.get('like_count') or 0
                    comments = media.get('comment_count') or 0

                    # Дата публикации
                    taken_at = media.get('taken_at')
                    pub_iso = None
                    if taken_at:
                        try:
                            pub_iso = datetime.utcfromtimestamp(int(taken_at)).isoformat()
                        except Exception:
                            pass

                    vd = media.get('video_duration')
                    duration = float(vd) if vd else None

                    results.append({
                        'shortcode': sc,
                        'url': f"https://www.instagram.com/reel/{sc}/",
                        'thumbnail_url': thumb,
                        'caption': caption,
                        'views': int(views or 0),
                        'likes': int(likes or 0),
                        'comments': int(comments or 0),
                        'published_at': pub_iso,
                        'duration_seconds': duration,
                    })

                # Пагинация
                paging = data.get('paging_info') or {}
                if paging.get('more_available'):
                    max_id = paging.get('max_id')
                    if not max_id:
                        break
                else:
                    break

            except Exception as e:
                logger.warning(f"clips/user не сработал (page {page}): {e}")
                break

        # Fallback: публичный feed через GraphQL (user_timeline_media)
        if not results:
            logger.info("Пробую fallback: GraphQL edge_owner_to_timeline_media")
            try:
                query_hash = "472f257a40c653c64c666ce877d59d2b"
                gql_url = "https://www.instagram.com/graphql/query/"
                end_cursor = ""
                for page in range(max_pages):
                    variables = {"id": str(instagram_user_id), "first": 50}
                    if end_cursor:
                        variables["after"] = end_cursor
                    params = {"query_hash": query_hash, "variables": json.dumps(variables)}
                    h = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'X-IG-App-ID': '936619743392459',
                        'Accept': '*/*',
                    }
                    proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
                    resp = requests.get(gql_url, params=params, headers=h, proxies=proxies, timeout=25)
                    if resp.status_code != 200:
                        logger.warning(f"GraphQL timeline вернул {resp.status_code}")
                        break
                    d = resp.json()
                    tl = d.get('data', {}).get('user', {}).get('edge_owner_to_timeline_media', {}) or {}
                    edges = tl.get('edges', []) or []
                    if not edges:
                        break
                    for e in edges:
                        node = e.get('node') or {}
                        # Фильтр: только видео/рилсы
                        if not (node.get('is_video') or node.get('product_type') == 'clips'):
                            continue
                        sc = node.get('shortcode')
                        if not sc or sc in seen_shortcodes:
                            continue
                        seen_shortcodes.add(sc)
                        thumb = node.get('thumbnail_src') or node.get('display_url')
                        caption_edges = (node.get('edge_media_to_caption') or {}).get('edges', []) or []
                        cap = (caption_edges[0].get('node', {}) or {}).get('text') if caption_edges else None
                        ts = node.get('taken_at_timestamp')
                        pub_iso = None
                        if ts:
                            try:
                                pub_iso = datetime.utcfromtimestamp(int(ts)).isoformat()
                            except Exception:
                                pass
                        results.append({
                            'shortcode': sc,
                            'url': f"https://www.instagram.com/reel/{sc}/",
                            'thumbnail_url': thumb,
                            'caption': cap,
                            'views': node.get('video_view_count') or 0,
                            'likes': (node.get('edge_media_preview_like') or {}).get('count', 0) or 0,
                            'comments': (node.get('edge_media_to_comment') or {}).get('count', 0) or 0,
                            'published_at': pub_iso,
                            'duration_seconds': node.get('video_duration'),
                        })
                    page_info = tl.get('page_info') or {}
                    if page_info.get('has_next_page'):
                        end_cursor = page_info.get('end_cursor') or ''
                        if not end_cursor:
                            break
                    else:
                        break
            except Exception as e:
                logger.warning(f"GraphQL timeline fallback не сработал: {e}")

        # HTTP fallback: GET https://www.instagram.com/{username}/ — в HTML есть shortcodes /reel/
        if not results:
            username_hint = username or getattr(self, '_last_profile_username', None)
            if username_hint:
                try:
                    page_url = f"https://www.instagram.com/{username_hint}/"
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
                        'Accept': 'text/html',
                        'Accept-Language': 'en-US,en;q=0.9',
                    }
                    proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
                    r = requests.get(page_url, headers=headers, proxies=proxies, timeout=20)
                    if r.status_code == 200 and r.text:
                        import re as _re
                        raw = _re.findall(r'/reel/([A-Za-z0-9_-]+)', r.text)
                        seen = set()
                        for sc in raw:
                            if sc not in seen:
                                seen.add(sc)
                                results.append({
                                    'shortcode': sc,
                                    'url': f"https://www.instagram.com/reel/{sc}/",
                                    'thumbnail_url': None,
                                    'caption': None,
                                    'views': 0, 'likes': 0, 'comments': 0,
                                    'published_at': None,
                                    'duration_seconds': None,
                                })
                        logger.info(f"HTTP /username/ fallback: нашёл {len(results)} shortcodes на публичной странице @{username_hint}")
                except Exception as e:
                    logger.warning(f"HTTP /username/ fallback не сработал: {e}")

        # Selenium fallback: открыть /{username}/reels/ и выскрести shortcodes
        if not results and self.driver:
            username_hint = username or getattr(self, '_last_profile_username', None)
            # Нам нужен username для URL — возьмём из параметра (у вызывающего)
            # Если его нет, запросим из web_profile_info (не идеально) — пропустим
            if username_hint:
                try:
                    reels_url = f"https://www.instagram.com/{username_hint}/reels/"
                    logger.info(f"Selenium: открываю {reels_url}")
                    self.driver.get(reels_url)
                    time.sleep(5)
                    # Прокрутим несколько раз чтобы подгрузить
                    for _ in range(3):
                        self.driver.execute_script("window.scrollBy(0, 1500);")
                        time.sleep(2)
                    html = self.driver.page_source or ''
                    import re as _re
                    shortcodes = list(dict.fromkeys(_re.findall(r'/reel/([A-Za-z0-9_-]+)/?', html)))
                    logger.info(f"Selenium: нашёл {len(shortcodes)} shortcodes на /reels/")
                    for sc in shortcodes[:60]:  # ограничим 60 последними
                        results.append({
                            'shortcode': sc,
                            'url': f"https://www.instagram.com/reel/{sc}/",
                            'thumbnail_url': None,
                            'caption': None,
                            'views': 0, 'likes': 0, 'comments': 0,
                            'published_at': None,
                            'duration_seconds': None,
                        })
                except Exception as e:
                    logger.warning(f"Selenium reels scrape не сработал: {e}")

        logger.info(f"Загружено {len(results)} рилсов для user_id={instagram_user_id}")
        return results

    def close(self):
        """Закрытие браузера"""
        if self.driver:
            self.driver.quit()
            logger.info("Браузер закрыт")
