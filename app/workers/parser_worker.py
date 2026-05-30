"""
Worker парсинга — берёт задачи из очереди и выполняет
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import or_

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
_parser_errors = 0


def reset_parser():
    """Сбросить парсер (при ошибках)"""
    global _parser_instance, _parser_errors
    if _parser_instance:
        try:
            _parser_instance.close()
        except:
            pass
    _parser_instance = None
    _parser_errors = 0
    logger.info("Parser instance reset")


def get_parser() -> ReelsParser:
    """Получить или создать экземпляр парсера"""
    global _parser_instance
    if _parser_instance is None:
        proxy = settings.PROXY_LIST if settings.PROXY_ENABLED else None
        # Загружаем аккаунты Instagram из файла
        import os
        accounts_file = os.environ.get('INSTAGRAM_ACCOUNTS_FILE', 'accstg.txt')
        # Проверяем существует ли файл
        if not os.path.exists(accounts_file):
            accounts_file = None
            logger.warning("Файл аккаунтов Instagram не найден, парсинг без авторизации")
        else:
            logger.info(f"Используем файл аккаунтов: {accounts_file}")
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


_SHORTCODE_RE = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")


def _extract_shortcode(url: str) -> Optional[str]:
    """Извлечь shortcode из любого Instagram-URL: /reel/ABC, /p/ABC, /tv/ABC, ?utm_…"""
    if not url:
        return None
    m = _SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _canonical_reel_url(url: str) -> str:
    """Привести URL к канонической форме /reel/{shortcode}/ (если получилось вытащить shortcode)."""
    sc = _extract_shortcode(url)
    if not sc:
        return url
    return f"https://www.instagram.com/reel/{sc}/"


def _find_reel_by_shortcode(db: Session, user_id: int, shortcode: str):
    """Найти Reel по shortcode независимо от формы URL (/reel/, /p/, /tv/, c/без слеша)."""
    if not shortcode:
        return None
    patterns = [
        f"%/reel/{shortcode}/%",
        f"%/reel/{shortcode}",
        f"%/reels/{shortcode}/%",
        f"%/reels/{shortcode}",
        f"%/p/{shortcode}/%",
        f"%/p/{shortcode}",
        f"%/tv/{shortcode}/%",
        f"%/tv/{shortcode}",
    ]
    return db.query(Reel).filter(
        Reel.user_id == user_id,
        or_(*[Reel.url.like(p) for p in patterns]),
    ).order_by(Reel.created_at.asc()).first()


def _process_sync_account_job(db: Session, job) -> bool:
    """Импорт всех рилсов указанного Instagram-аккаунта"""
    from app.models.account import InstagramAccount
    acc = db.query(InstagramAccount).filter(InstagramAccount.id == job.account_id).first()
    if not acc:
        fail_job(db, job, "Аккаунт не найден")
        return True

    logger.info(f"🔄 SYNC_ACCOUNT #{job.id}: @{acc.instagram_username}")

    try:
        parser = get_parser()

        # Обновляем профиль (раз в синк) — IG-аватарки протухают за пару часов,
        # подписчиков/счётчики тоже хочется свежие.
        # Сначала пытаемся через Apify (если у юзера есть токен), потом direct.
        from app.models.user import User as _UU
        _owner_for_prof = db.query(_UU).filter(_UU.id == job.user_id).first()
        _apify_for_prof = _owner_for_prof.apify_token if _owner_for_prof else None
        profile = None
        if _apify_for_prof:
            try:
                profile = parser.fetch_profile_via_apify(acc.instagram_username, _apify_for_prof)
            except Exception as e:
                logger.warning(f"fetch_profile_via_apify упал: {e}")
                profile = None
        if not profile:
            try:
                profile = parser.fetch_instagram_profile(acc.instagram_username)
            except Exception as e:
                logger.warning(f"fetch_instagram_profile упал: {e}")
                profile = None
        if profile:
            acc.instagram_user_id = acc.instagram_user_id or (profile.get('instagram_user_id') or None)
            acc.full_name = profile.get('full_name') or acc.full_name
            # profile_pic_url ОБЯЗАТЕЛЬНО обновляем свежим — старый протух
            if profile.get('profile_pic_url'):
                acc.profile_pic_url = profile['profile_pic_url']
            acc.bio = profile.get('bio') or acc.bio
            acc.followers_count = profile.get('followers_count') or acc.followers_count
            acc.following_count = profile.get('following_count') or acc.following_count
            acc.posts_count = profile.get('posts_count') or acc.posts_count
            db.commit()

        if not acc.instagram_user_id:
            acc.last_sync_error = "Не удалось получить instagram_user_id профиля"
            db.commit()
            fail_job(db, job, acc.last_sync_error)
            return True

        # Получаем список рилсов — сначала через Apify (если юзер задал токен)
        reels_list = []
        from app.models.user import User as _User
        owner = db.query(_User).filter(_User.id == job.user_id).first()
        apify_token = owner.apify_token if owner else None
        if apify_token:
            reels_list = parser.fetch_reels_via_apify(acc.instagram_username, apify_token, results_limit=100)

        # Fallback на прямые методы (clips/user, GraphQL, Selenium, HTTP)
        if not reels_list:
            reels_list = parser.fetch_instagram_reels_list(acc.instagram_user_id, max_pages=10, username=acc.instagram_username)
        if not reels_list:
            acc.last_sync_error = "Рилсы не получены (возможно, аккаунт приватный или нет рилсов)"
            acc.last_synced_at = datetime.utcnow()
            db.commit()
            complete_job(db, job, 0, 0, 0, 0)
            return True

        # Сортируем по дате публикации (свежие — сверху), назначаем position_in_account
        def _parse_iso(s):
            if not s:
                return datetime.min
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return datetime.min

        reels_sorted = sorted(reels_list, key=lambda x: _parse_iso(x.get('published_at')), reverse=True)

        # Дедуп внутри одного ответа Apify по shortcode (бывает дублируется)
        seen_sc = set()
        unique_sorted = []
        for item in reels_sorted:
            sc = item.get('shortcode') or _extract_shortcode(item.get('url') or '')
            if not sc:
                # без shortcode не сможем дедупить — пропускаем (редко)
                continue
            if sc in seen_sc:
                continue
            seen_sc.add(sc)
            item['_shortcode'] = sc
            unique_sorted.append(item)
        reels_sorted = unique_sorted

        # Сбрасываем position_in_account у всех рилсов аккаунта — назначим заново
        db.query(Reel).filter(Reel.instagram_account_id == acc.id).update(
            {Reel.position_in_account: None}, synchronize_session=False
        )

        created = 0
        updated = 0
        for idx, item in enumerate(reels_sorted, start=1):
            sc = item.get('_shortcode') or item.get('shortcode')
            full_url = _canonical_reel_url(item.get('url') or f"https://www.instagram.com/reel/{sc}/")

            # Ищем существующий рилс — по shortcode (любые формы URL)
            existing = _find_reel_by_shortcode(db, job.user_id, sc)

            pub_dt = None
            if item.get('published_at'):
                try:
                    pub_dt = datetime.fromisoformat(item['published_at'])
                except Exception:
                    pub_dt = None

            if existing:
                existing.instagram_account_id = acc.id
                existing.position_in_account = idx
                # Наполним метаданные, если пусто
                existing.thumbnail_url = existing.thumbnail_url or item.get('thumbnail_url')
                existing.caption = existing.caption or item.get('caption')
                existing.duration_seconds = existing.duration_seconds or item.get('duration_seconds')
                existing.published_at = existing.published_at or pub_dt
                existing.author_username = existing.author_username or acc.instagram_username
                existing.author_full_name = existing.author_full_name or acc.full_name
                # Обновляем метрики свежими данными от Apify + история
                new_views = int(item.get('views') or 0)
                new_likes = int(item.get('likes') or 0)
                new_comments = int(item.get('comments') or 0)
                if new_views or new_likes or new_comments:
                    existing.views = new_views or existing.views
                    existing.likes = new_likes or existing.likes
                    existing.comments = new_comments or existing.comments
                    existing.last_parsed_at = datetime.utcnow()
                    db.add(ReelHistory(
                        reel_id=existing.id,
                        views=existing.views or 0,
                        likes=existing.likes or 0,
                        comments=existing.comments or 0,
                        shares=existing.shares or 0,
                        parsed_at=datetime.utcnow(),
                    ))
                updated += 1
            else:
                new_reel = Reel(
                    user_id=job.user_id,
                    instagram_account_id=acc.id,
                    position_in_account=idx,
                    title=(item.get('caption') or '').strip().split('\n')[0][:255] or f"Reel #{idx}",
                    platform='instagram',
                    url=full_url,
                    enabled=True,
                    views=item.get('views') or 0,
                    likes=item.get('likes') or 0,
                    comments=item.get('comments') or 0,
                    shares=0,
                    thumbnail_url=item.get('thumbnail_url'),
                    author_username=acc.instagram_username,
                    author_full_name=acc.full_name,
                    published_at=pub_dt,
                    caption=item.get('caption'),
                    duration_seconds=item.get('duration_seconds'),
                )
                db.add(new_reel)
                db.flush()  # чтобы получить new_reel.id
                if new_reel.views or new_reel.likes or new_reel.comments:
                    db.add(ReelHistory(
                        reel_id=new_reel.id,
                        views=new_reel.views or 0,
                        likes=new_reel.likes or 0,
                        comments=new_reel.comments or 0,
                        shares=0,
                        parsed_at=datetime.utcnow(),
                    ))
                new_reel.last_parsed_at = datetime.utcnow()
                created += 1

            # Content Forge — если аккаунт помечен для авто-скачивания, забираем
            # MP4 в наш R2 (IG-CDN URL протухает за часы, надо сразу). Делаем
            # после commit-а по reel-у, чтобы было reel.id.
            try:
                if acc.auto_download_media and item.get('video_url'):
                    target = existing or new_reel
                    if target and target.id and not target.media_storage_key:
                        from app.services.media_service import download_reel_media
                        # commit чтобы выдать reel.id для нового, и чтобы при
                        # ошибке download реальные изменения reel'а сохранились
                        db.commit()
                        download_reel_media(db, target, item['video_url'])
            except Exception as e:
                logger.warning(f"auto-download для reel {sc} упал: {e}")

        # Рилсы, которым не назначилась новая позиция (не нашлись в свежем Apify-ответе),
        # ОСТАВЛЯЕМ привязанными к аккаунту, но с position_in_account = NULL —
        # они отсортируются в конец списка через nullslast(). Так не теряем рилсы.
        orphans_count = db.query(Reel).filter(
            Reel.instagram_account_id == acc.id,
            Reel.position_in_account.is_(None),
        ).count()
        if orphans_count:
            logger.info(f"ℹ️ В @{acc.instagram_username} {orphans_count} рилсов без позиции (не было в свежем Apify-ответе) — оставлены в конце списка")

        acc.last_synced_at = datetime.utcnow()
        acc.last_sync_error = None
        db.commit()

        complete_job(db, job, len(reels_sorted), created, updated, 0)
        logger.info(f"✅ SYNC @{acc.instagram_username}: {len(reels_sorted)} рилсов (новых: {created}, обновлено: {updated})")
        return True

    except Exception as e:
        import traceback
        logger.error(f"SYNC_ACCOUNT упал: {e}\n{traceback.format_exc()}")
        acc.last_sync_error = str(e)[:500]
        db.commit()
        fail_job(db, job, str(e)[:500])
        return True


def process_one_job(db: Session) -> bool:
    """
    Обработать одну задачу из очереди.
    Возвращает True если задача была обработана, False если очередь пуста.
    """
    job = get_next_pending_job(db)
    if not job:
        return False

    # Диспатчер по типу job-а.
    from app.models.parsing import JobType
    if job.job_type == JobType.SYNC_ACCOUNT:
        return _process_sync_account_job(db, job)
    if job.job_type == JobType.GENERATE_VIDEO:
        from app.workers.generation_worker import process_generate_video_job
        return process_generate_video_job(db, job)
    # POST_TO_INSTAGRAM / OAUTH_REFRESH — приходят в следующих PR; пока
    # помечаем как failed, чтобы не зависали в RUNNING.
    if job.job_type in (JobType.POST_TO_INSTAGRAM, JobType.OAUTH_REFRESH):
        fail_job(db, job, f"job_type {job.job_type.value} ещё не имплементирован")
        return True

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

        # Метаданные (обложка/автор) — обновляем если пусто или если парсер принёс свежие
        thumb = metrics.get('thumbnail_url')
        author_username = metrics.get('author_username')
        author_full_name = metrics.get('author_full_name')
        if thumb and not reel.thumbnail_url:
            reel.thumbnail_url = thumb
        if author_username and not reel.author_username:
            reel.author_username = author_username
        if author_full_name and not reel.author_full_name:
            reel.author_full_name = author_full_name

        # Пост-метаданные: дата публикации, подпись, длительность
        published_at_str = metrics.get('published_at')
        if published_at_str and not reel.published_at:
            try:
                reel.published_at = datetime.fromisoformat(published_at_str)
            except Exception:
                pass
        caption = metrics.get('caption')
        if caption and not reel.caption:
            reel.caption = caption
        duration = metrics.get('duration_seconds')
        if duration and not reel.duration_seconds:
            reel.duration_seconds = duration

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

        logger.info(f"✅ Задача #{job.id} завершена: views={views}, likes={likes}, comments={comments}, shares={shares}")

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
    consecutive_errors = 0

    check_count = 0
    while True:
        db = None
        try:
            db = SessionLocal()
            check_count += 1
            if check_count % 12 == 1:  # Логируем каждую минуту (12 * 5 сек)
                from app.models.parsing import ParseJob, JobStatus
                pending = db.query(ParseJob).filter(ParseJob.status == JobStatus.PENDING).count()
                logger.info(f"📋 Проверка очереди #{check_count}: {pending} задач в ожидании")
            processed = process_one_job(db)
            consecutive_errors = 0  # Сброс счётчика ошибок при успехе
            if not processed:
                # Очередь пуста — ждём
                time.sleep(poll_interval)
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Worker error #{consecutive_errors}: {e}")

            # При ошибках БД — ждём дольше
            if "SSL" in str(e) or "connection" in str(e).lower() or "OperationalError" in str(e):
                wait_time = min(30, poll_interval * consecutive_errors)
                logger.warning(f"Database connection error, waiting {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                time.sleep(poll_interval)
        finally:
            if db:
                try:
                    db.rollback()  # Откатываем транзакцию перед закрытием
                except:
                    pass
                try:
                    db.close()
                except:
                    pass
