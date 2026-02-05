"""
Scheduler — периодически ставит рилсы в очередь на парсинг
по интервалу тарифа каждого юзера
"""

import logging
import time
import threading
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.user import User
from app.models.reel import Reel
from app.models.parsing import ParseJob, JobStatus
from app.services.tariff_service import get_parse_interval, get_priority
from app.services.parsing_service import create_parse_job

logger = logging.getLogger(__name__)


def schedule_user_reels(db: Session, user: User):
    """Проверить и поставить рилсы юзера в очередь если прошёл интервал"""
    interval = get_parse_interval(user)

    # Ищем последнюю завершённую задачу юзера
    last_job = db.query(ParseJob).filter(
        ParseJob.user_id == user.id,
        ParseJob.status == JobStatus.COMPLETED,
    ).order_by(ParseJob.completed_at.desc()).first()

    # Проверяем, прошёл ли интервал
    if last_job and last_job.completed_at:
        next_time = last_job.completed_at + timedelta(minutes=interval)
        if datetime.utcnow() < next_time:
            return 0  # Ещё рано

    # Проверяем, нет ли уже pending задач
    pending = db.query(ParseJob).filter(
        ParseJob.user_id == user.id,
        ParseJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
    ).count()
    if pending > 0:
        return 0  # Уже есть задачи в работе

    # Ставим все активные рилсы в очередь
    reels = db.query(Reel).filter(
        Reel.user_id == user.id,
        Reel.enabled == True,
    ).all()

    count = 0
    for reel in reels:
        create_parse_job(db, user, reel)
        count += 1

    if count > 0:
        logger.info(f"📋 Юзер {user.email}: поставлено {count} рилсов в очередь")

    return count


def scheduler_tick():
    """Один тик шедулера — проверяет всех активных юзеров"""
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active == True).all()
        total_scheduled = 0

        for user in users:
            reels_count = db.query(Reel).filter(
                Reel.user_id == user.id,
                Reel.enabled == True,
            ).count()
            if reels_count == 0:
                continue

            count = schedule_user_reels(db, user)
            total_scheduled += count

        if total_scheduled > 0:
            logger.info(f"📊 Scheduler: поставлено {total_scheduled} задач суммарно")

    except Exception as e:
        logger.error(f"Scheduler error: {e}")
    finally:
        db.close()


def run_scheduler_loop(check_interval: int = 30):
    """
    Основной цикл шедулера.

    Args:
        check_interval: как часто проверять (секунды)
    """
    logger.info("⏰ Scheduler запущен")

    while True:
        try:
            scheduler_tick()
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        time.sleep(check_interval)


def start_scheduler_thread(check_interval: int = 30):
    """Запустить шедулер в отдельном потоке (вызывается из main.py)"""
    thread = threading.Thread(
        target=run_scheduler_loop,
        args=(check_interval,),
        daemon=True,
        name="scheduler",
    )
    thread.start()
    logger.info("⏰ Scheduler thread запущен")
    return thread


def start_worker_thread(poll_interval: int = 5):
    """Запустить воркер парсинга в отдельном потоке"""
    from app.workers.parser_worker import run_worker_loop

    thread = threading.Thread(
        target=run_worker_loop,
        args=(poll_interval,),
        daemon=True,
        name="parser-worker",
    )
    thread.start()
    logger.info("🔧 Parser Worker thread запущен")
    return thread
