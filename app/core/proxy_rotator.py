"""
Ротатор прокси — миграция из proxy_rotator.py
Убран ProxyConfig, main(). Оставлен класс ProxyRotator.
"""

import random
import logging
import requests
from itertools import cycle

logger = logging.getLogger(__name__)


class ProxyRotator:
    def __init__(self, proxy_list=None):
        """
        Args:
            proxy_list: Список прокси в формате ['host:port:user:pass']
        """
        self.proxy_list = proxy_list or []
        self.current_proxy = None
        self.proxy_cycle = None
        self.failed_proxies = set()

        if self.proxy_list:
            self.proxy_cycle = cycle(self.proxy_list)
            logger.info(f"Загружено {len(self.proxy_list)} прокси")

    def get_next_proxy(self):
        """Получить следующий прокси из списка"""
        if not self.proxy_cycle:
            return None

        max_attempts = len(self.proxy_list)
        for _ in range(max_attempts):
            proxy = next(self.proxy_cycle)
            if proxy not in self.failed_proxies:
                self.current_proxy = proxy
                return proxy

        logger.warning("Все прокси помечены как неработающие, сбрасываем список")
        self.failed_proxies.clear()
        self.current_proxy = next(self.proxy_cycle)
        return self.current_proxy

    def get_random_proxy(self):
        """Получить случайный прокси"""
        if not self.proxy_list:
            return None
        available = [p for p in self.proxy_list if p not in self.failed_proxies]
        if not available:
            self.failed_proxies.clear()
            available = self.proxy_list
        self.current_proxy = random.choice(available)
        return self.current_proxy

    def mark_proxy_failed(self, proxy=None):
        """Пометить прокси как неработающий"""
        proxy = proxy or self.current_proxy
        if proxy:
            self.failed_proxies.add(proxy)
            logger.warning(f"Прокси помечен как неработающий: {proxy}")

    def test_proxy(self, proxy, timeout=10):
        """Проверка работоспособности прокси"""
        try:
            proxy_dict = self._parse_proxy(proxy)
            response = requests.get('http://httpbin.org/ip', proxies=proxy_dict, timeout=timeout)
            if response.status_code == 200:
                logger.info(f"Прокси работает: {proxy}")
                return True
            return False
        except Exception as e:
            logger.warning(f"Ошибка проверки прокси {proxy}: {e}")
            return False

    def _parse_proxy(self, proxy_string):
        """Парсинг строки прокси в словарь для requests"""
        if '://' in proxy_string:
            return {'http': proxy_string, 'https': proxy_string}
        parts = proxy_string.split(':')
        if len(parts) == 2:
            proxy_url = f"http://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else:
            raise ValueError(f"Неверный формат прокси: {proxy_string}")
        return {'http': proxy_url, 'https': proxy_url}

    def rotate(self):
        """Переключиться на следующий прокси"""
        old_proxy = self.current_proxy
        new_proxy = self.get_next_proxy()
        if new_proxy != old_proxy:
            logger.info(f"Прокси изменен: {old_proxy} → {new_proxy}")
        return new_proxy
