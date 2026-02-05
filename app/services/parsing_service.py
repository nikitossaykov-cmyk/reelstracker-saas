"""
Сервис очереди парсинга: создание задач, получение статуса
"""

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.models.reel import Reel
from app.models.parsing import ParseJob, JobStatus
from app.services.tariff_service import get_parse_interval, get_priority


def create_parse_job(db: Session, user: User, reel: Reel) -> ParseJob:
    """Создать задачу парсинга в очередь"""

    # Проверяем, нет ли уже pending/running задачи для этого рилса
    existing = db.query(ParseJob).filter(
        ParseJob.reel_id == reel.id,
        ParseJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
    ).first()

    if existing:
        return existing  # Уже в очереди

    job = ParseJob(
        reel_id=reel.id,
        user_id=user.id,
        status=JobStatus.PENDING,
        priority=get_priority(user),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_parse_jobs_for_user(db: Session, user: User) -> list:
    """Поставить все активные рилсы юзера в очередь"""
    reels = db.query(Reel).filter(
        Reel.user_id == user.id,
        Reel.enabled == True,
    ).all()

    jobs = []
    for reel in reels:
        job = create_parse_job(db, user, reel)
        jobs.append(job)

    return jobs


def get_parse_status(db: Session, user: User) -> dict:
    """Статус парсинга для юзера"""

    # Pending задачи
    pending_count = db.query(func.count(ParseJob.id)).filter(
        ParseJob.user_id == user.id,
        ParseJob.status == JobStatus.PENDING,
    ).scalar()

    # Running задачи
    running_count = db.query(func.count(ParseJob.id)).filter(
        ParseJob.user_id == user.id,
        ParseJob.status == JobStatus.RUNNING,
    ).scalar()

    # Последняя завершённая
    last_completed = db.query(ParseJob).filter(
        ParseJob.user_id == user.id,
        ParseJob.status == JobStatus.COMPLETED,
    ).order_by(ParseJob.completed_at.desc()).first()

    # Можно ли ставить в очередь (прошёл ли интервал)
    interval = get_parse_interval(user)
    next_allowed = None
    can_parse = True

    if last_completed and last_completed.completed_at:
        next_time = last_completed.completed_at + timedelta(minutes=interval)
        if datetime.utcnow() < next_time:
            can_parse = False
            next_allowed = next_time.isoformat()

    return {
        "pending": pending_count,
        "running": running_count,
        "last_completed": last_completed.completed_at.isoformat() if last_completed and last_completed.completed_at else None,
        "parse_interval_minutes": interval,
        "can_parse": can_parse,
        "next_allowed": next_allowed,
    }


def get_next_pending_job(db: Session) -> Optional[ParseJob]:
    """Взять следующую задачу из очереди (для worker)"""
    job = db.query(ParseJob).filter(
        ParseJob.status == JobStatus.PENDING,
    ).order_by(
        ParseJob.priority.desc(),
        ParseJob.created_at.asc(),
    ).with_for_update(skip_locked=True).first()

    if job:
        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        db.commit()
        db.refresh(job)

    return job


def complete_job(db: Session, job: ParseJob, views: int, likes: int, comments: int, shares: int):
    """Завершить задачу с результатом"""
    job.status = JobStatus.COMPLETED
    job.completed_at = datetime.utcnow()
    job.result_views = views
    job.result_likes = likes
    job.result_comments = comments
    job.result_shares = shares
    db.commit()


def fail_job(db: Session, job: ParseJob, error_message: str):
    """Пометить задачу как неудавшуюся"""
    job.status = JobStatus.FAILED
    job.completed_at = datetime.utcnow()
    job.error_message = error_message
    db.commit()
