"""
Модель Instagram-аккаунта для массового импорта рилсов
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, Text
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

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")
    reels = relationship("Reel", back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        # Один username на одного юзера
        UniqueConstraint("user_id", "instagram_username", name="uq_user_instagram_username"),
    )

    def __repr__(self):
        return f"<InstagramAccount @{self.instagram_username}>"
