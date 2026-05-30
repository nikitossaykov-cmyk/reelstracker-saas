"""
Модели биллинга — кредитная система.

Юзер пополняет баланс (deposit), мы списываем за каждую генерацию/пост
по факту cost от провайдера + наша margin. Снимает проблему "не хватило
в этом месяце".

PR #1: только модель ledger. Tribute/ЮKassa webhooks + начисления — в
следующем PR.
"""

import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, ForeignKey, Enum, Text, Index,
)
from sqlalchemy.orm import relationship
from app.database import Base


class TransactionType(str, enum.Enum):
    DEPOSIT = "deposit"               # +balance, юзер пополнил
    REFUND = "refund"                 # +balance, мы вернули за ошибку
    PROMO = "promo"                   # +balance, бонусные кредиты
    GENERATION = "generation"         # -balance, списание за GeneratedVideo
    POST = "post"                     # -balance, списание за Post (если будет тариф)
    ADJUSTMENT = "adjustment"         # ±, ручная корректировка админом


class CreditTransaction(Base):
    """Append-only ledger всех движений по балансу юзера.

    Никогда не апдейтится — только INSERT. Текущий баланс хранится
    отдельно (users.credits_balance_kopecks) для быстрого чтения, но
    источник истины = SUM(amount_kopecks) WHERE user_id=X.

    Все суммы в копейках (BigInteger), signed: положительные = пополнение,
    отрицательные = списание.
    """
    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    amount_kopecks = Column(BigInteger, nullable=False)
    transaction_type = Column(Enum(TransactionType), nullable=False)

    # Snapshot баланса после применения этой транзакции (для аудита).
    balance_after_kopecks = Column(BigInteger, nullable=False)

    # На что списано / откуда пополнено. Свободный текст — не FK,
    # чтобы можно было ссылаться на разные таблицы (generated_video_id,
    # tribute_payment_id, и т.д.) без миграций при добавлении новых типов.
    reference_type = Column(String(64), nullable=True)
    reference_id = Column(String(255), nullable=True)

    description = Column(Text, nullable=True)

    # ID транзакции у провайдера платежей (Tribute/ЮKassa). Для DEPOSIT.
    # Уникальность по этому полю — guard от двойных webhook-ов.
    external_payment_id = Column(String(255), nullable=True, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User", back_populates="credit_transactions")

    __table_args__ = (
        Index("ix_credit_transactions_user_created", "user_id", "created_at"),
    )

    def __repr__(self):
        sign = "+" if self.amount_kopecks >= 0 else ""
        return (f"<CreditTransaction {self.id} user={self.user_id} "
                f"{sign}{self.amount_kopecks/100:.2f}₽ {self.transaction_type.value}>")
