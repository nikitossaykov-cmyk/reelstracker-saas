"""
Worker парсинга — берёт задачи из очереди и выполняет
"""

import logging
import time
from datetime import datetime
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.reel import Reel, ReelHistory
from app.models.parsing import ParseJob, JobStatus
from app.services.parsing_service import get_next_pending_job, complete_job, fail_job
from app.services.telegram_service import get_user_telegram
from app.core.reels_parser import ReelsParser
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Глобальный экземпляр парсера (один на воркер)
_parser_instance = None


def get_parser() -> ReelsParser:
    """Получить или создать экземпляр парсера"""
    global _parser_instance
    if _parser_instance is None:
        proxy = settings.PROXY_LIST if settings.PROXY_ENABLED else None
        accounts_file = None  # TODO: сделать загрузку аккаунтов из настроек юзера
        _parser_instance = ReelsParser(proxy=proxy, accounts_file=accounts_file)
    return _parser_instance


async def send_telegram_notification(user, reel, metrics, old_views):
    """Отправить Telegram уведомление если настроено"""
    try:
        tg = get_user_telegram(user)
        if not tg:
            return

        # Уведомление о завершении парсинга
        if user.telegram_notify_complete:
            growth = metrics['views'] - old_views if old_views else 0
            msg = (
                f"📊 <b>{reel.title}</b>\n"
                f"👁 Просмотры: {metrics['views']:,}"
            )
            if growth > 0:
                msg += f" (+{growth:,})"
            msg += (
                f"\n❤️ Лайки: {metrics['likes']:,}\n"
                f"💬 Комменты: {metrics['comments']:,}\n"
                f"🔄 Репосты: {metrics['shares']:,}"
            )
            await tg.send_message(msg)

        # Уведомление о виральности (быстрый рост)
        if user.telegram_notify_viral and old_views:
            growth = metrics['views'] - old_views
            if growth > user.telegram_threshold_views:
                await tg.send_message(
                    f"🔥 <b>VIRAL!</b> {reel.title}\n"
                    f"Рост: +{growth:,} просмотров за цикл!"
                )
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")


def process_one_job(db: Session) -> bool:
    """
    Обработать одну задачу из очереди.
    Возвращает True если задача была обработана, False если очередь пуста.
    """
    job = get_next_pending_job(db)
    if not job:
        return False

    logger.info(f"🔄 Обрабатываю задачу #{job.id}: reel_id={job.reel_id}")

    try:
        # Получаем рилс и юзера
        reel = db.query(Reel).filter(Reel.id == job.reel_id).first()
        if not reel:
            fail_job(db, job, "Рилс не найден")
            return True

        # Запоминаем старые просмотры для сравнения
        old_views = reel.views or 0

        # Парсим
        parser = get_parser()

        # Определяем полный URL
        url = reel.url
        if reel.platform == 'instagram' and not url.startswith('http'):
            url = f"https://www.instagram.com/reel/{url}/"

        metrics = parser.parse_reel(url, reel.platform)

        if metrics is None:
            fail_job(db, job, "Не удалось получить метрики")
            return True

        views = metrics.get('views', 0)
        likes = metrics.get('likes', 0)
        comments = metrics.get('comments', 0)
        shares = metrics.get('shares', 0)

        # Обновляем текущие метрики рилса
        reel.views = views
        reel.likes = likes
        reel.comments = comments
        reel.shares = shares
        reel.last_parsed_at = datetime.utcnow()

        # Сохраняем в историю
        history_entry = ReelHistory(
            reel_id=reel.id,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            parsed_at=datetime.utcnow(),
        )
        db.add(history_entry)

        # Завершаем задачу
        complete_job(db, job, views, likes, comments, shares)

        logger.info(f"✅ Задача #{job.id} завершена: views={views}, likes={likes}")

        # Telegram уведомления (async в sync контексте)
        import asyncio
        try:
            from app.models.user import User
            user = db.query(User).filter(User.id == job.user_id).first()
            if user:
                asyncio.run(send_telegram_notification(user, reel, metrics, old_views))
        except Exception as e:
            logger.error(f"Telegram notification error: {e}")

        return True

    except Exception as e:
        logger.error(f"❌ Ошибка задачи #{job.id}: {e}")
        fail_job(db, job, str(e))
        return True


def run_worker_loop(poll_interval: int = 5):
    """
    Основной цикл воркера — непрерывно берёт задачи из очереди.

    Args:
        poll_interval: интервал проверки очереди (секунды)
    """
    logger.info("🚀 Parser Worker запущен")

    while True:
        db = SessionLocal()
        try:
            processed = process_one_job(db)
            if not processed:
                # Очередь пуста — ждём
                time.sleep(poll_interval)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            time.sleep(poll_interval)
        finally:
            db.close()
