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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

logger = logging.getLogger(__name__)


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
        if accounts_file:
            self.load_accounts(accounts_file)
        self.setup_selenium()

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

            # Ищем chromedriver в PATH или стандартных местах
            chromedriver_path = shutil.which('chromedriver') or '/usr/local/bin/chromedriver'
            chrome_binary = shutil.which('google-chrome') or shutil.which('google-chrome-stable') or '/usr/bin/google-chrome'

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
                'timestamp': datetime.now().isoformat()
            }

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
                            logger.info(f"API метрики: views={metrics['views']}, likes={metrics['likes']}")
                            if metrics['views'] > 0 or metrics['likes'] > 0:
                                return metrics
                except Exception as e:
                    logger.warning(f"API метод не сработал: {e}")

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
            patterns = {
                'views': [r'"video_view_count":(\d+)', r'"play_count":(\d+)', r'"view_count":(\d+)'],
                'likes': [r'"like_count":(\d+)', r'"edge_media_preview_like":\{"count":(\d+)'],
                'comments': [r'"comment_count":(\d+)', r'"edge_media_to_comment":\{"count":(\d+)'],
            }

            for metric_name, metric_patterns in patterns.items():
                if metrics[metric_name] > 0:
                    continue
                for pattern in metric_patterns:
                    match = re.search(pattern, page_source)
                    if match:
                        metrics[metric_name] = int(match.group(1))
                        break

            if metrics['views'] > 0 or metrics['likes'] > 0:
                logger.info(f"Instagram метрики: views={metrics['views']}, likes={metrics['likes']}")
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

    def close(self):
        """Закрытие браузера"""
        if self.driver:
            self.driver.quit()
            logger.info("Браузер закрыт")
