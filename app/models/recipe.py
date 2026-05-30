"""
ContentRecipe — структурированный «рецепт» виральной механики, извлечённый
из проанализированного рилса (PR #4 Analyzer output).

Recipe — переиспользуемый шаблон: один рецепт «POV-свидание с парфюмом»
можно реализовать N раз с разными ароматами / лицами / голосами.
Remake worker (PR #6) принимает recipe + remake_params → собирает финал.
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Text, JSON, Index,
)
from sqlalchemy.orm import relationship
from app.database import Base


class ContentRecipe(Base):
    """Извлечённый рецепт виральной механики."""

    __tablename__ = "content_recipes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    # Откуда экстрагирован — может быть NULL если пользователь сам написал.
    source_reel_id = Column(Integer, ForeignKey("reels.id", ondelete="SET NULL"),
                            nullable=True, index=True)

    # Tabular fields — для быстрого поиска / фильтров.
    name = Column(String(255), nullable=False)            # короткое имя
    hook_type = Column(String(64), nullable=True)         # POV / REACTION / ...
    duration_sec = Column(Integer, nullable=True)
    language = Column(String(8), nullable=True)           # ru / en

    # Структурированные поля (free-form JSON чтобы не плодить колонки).
    hook = Column(JSON, nullable=True)
    # {"text": "...", "type": "POV", "duration_sec": 3,
    #  "delivery": "voice"|"text overlay"|"both"}

    structure = Column(JSON, nullable=True)
    # [{"sec": 0, "action": "selfie close-up", "voice": "...", "overlay": "..."},
    #  {"sec": 3, "action": "show bottle", ...}, ...]

    visual_motifs = Column(JSON, nullable=True)
    # ["close-up bottle on marble", "soft window light", "9:16 vertical",
    #  "muted warm grade"]

    audio_strategy = Column(JSON, nullable=True)
    # {"type": "original_dialogue"|"trending_sound"|"voiceover"|"ambient",
    #  "sound_id": optional, "voice_description": optional}

    cta = Column(JSON, nullable=True)
    # {"brand_mention_count": 1, "marketplace": "wildberries",
    #  "tone": "casual"|"hard sell", "placement": "0:14"}

    # Промпт-готовый markdown — то что отдают composer'у/Veo/Omni.
    canonical_prompt = Column(Text, nullable=True)

    # Сырой LLM-ответ для дебага / повторного использования.
    raw_extractor_response = Column(Text, nullable=True)
    extractor_model = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False)

    user = relationship("User", back_populates="content_recipes")
    source_reel = relationship("Reel")

    __table_args__ = (
        Index("ix_content_recipes_user_created", "user_id", "created_at"),
    )

    def __repr__(self):
        return f"<ContentRecipe #{self.id} {self.name} ({self.hook_type})>"
