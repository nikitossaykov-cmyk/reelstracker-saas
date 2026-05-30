"""
Модель Instagram-аккаунта для массового импорта рилсов
"""

from datetime import datetime
from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, ForeignKey, UniqueConstraint, Text, JSON
from sqlalchemy.orm import relationship
from app.database import Base


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Identifiers
    instagram_username = Column(String(255), nullable=False)
    instagram_user_id = Column(String(64), nullable=True)  # числовой id из Instagram

    # Meta
    full_name = Column(String(255), nullable=True)
    profile_pic_url = Column(String(1024), nullable=True)
    bio = Column(Text, nullable=True)
    followers_count = Column(Integer, nullable=True)
    following_count = Column(Integer, nullable=True)
    posts_count = Column(Integer, nullable=True)

    # Sync
    sync_enabled = Column(Boolean, default=True, nullable=False)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_error = Column(Text, nullable=True)

    # Content Forge — если True, при каждом sync скачиваем медиа всех новых
    # рилсов этого аккаунта в наш R2. Включать только для аккаунтов-targets
    # из которых будем делать ремейки (не для своих, не для broad-tracking).
    auto_download_media = Column(Boolean, default=False, nullable=False)

    # PR #10 — auto-pipeline: при каждом sync скачанному рилсу автоматически
    # запускается ANALYZE_REEL → ContentRecipe. Включать вместе с
    # auto_download_media для target-аккаунтов.
    auto_analyze_media = Column(Boolean, default=False, nullable=False)

    # PR #10 — full auto-remake: на виральный alert (growth > threshold за
    # window) автоматически запускается chain download → analyze → recipe
    # → remake → uniqify → publish (если auto_publish).
    auto_remake_enabled = Column(Boolean, default=False, nullable=False)
    auto_uniqify = Column(Boolean, default=True, nullable=False)
    auto_publish = Column(Boolean, default=False, nullable=False)  # сначала на review!

    # Триггер виральности — рост x раз за N часов.
    viral_growth_threshold = Column(Float, default=2.0, nullable=False)
    viral_window_hours = Column(Integer, default=12, nullable=False)

    # Параметры подмены для remake. JSON: {brand, product_description,
    # face_description, voice_description, location, outfit, palette,
    # extra_instructions}.
    default_remake_params = Column(JSON, nullable=True)

    # Куда автоматически публиковать (FK на posting_targets).
    auto_posting_target_id = Column(Integer, ForeignKey("posting_targets.id",
                                    ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")
    reels = relationship("Reel", back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        # Один username на одного юзера
        UniqueConstraint("user_id", "instagram_username", name="uq_user_instagram_username"),
    )

    def __repr__(self):
        return f"<InstagramAccount @{self.instagram_username}>"
