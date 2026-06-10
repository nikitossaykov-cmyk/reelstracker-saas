"""
Voice clone API — XTTS-v2 на MSI.
"""

from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter()


class VoiceCloneResponse(BaseModel):
    storage_key: str
    media_url: str
    text_length: int
    language: str


@router.post("/clone", response_model=VoiceCloneResponse)
async def voice_clone_endpoint(
    text: str = Form(...),
    language: str = Form("ru"),
    ref_voice: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Клонировать голос: текст + референс (≥3 сек wav/mp3) → wav в R2.

    Использует MSI XTTS-v2 (RTX 3070). Бесплатно. Работает только когда
    приложение крутится на хосте с Tailscale-доступом к MSI (см. PR #14
    caveat).
    """
    if len(text.strip()) == 0:
        raise HTTPException(400, detail="text пустой")
    if len(text) > 4000:
        raise HTTPException(400, detail="text слишком длинный (>4000 символов)")
    if language not in ("ru", "en", "es", "fr", "de", "it", "pt", "pl", "tr",
                        "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko"):
        raise HTTPException(400, detail=f"language '{language}' не поддерживается XTTS-v2")

    # Save uploaded ref to /tmp
    workdir = Path(tempfile.mkdtemp(prefix=f"vc_in_{current_user.id}_"))
    ref_path = workdir / f"ref{Path(ref_voice.filename or 'ref.wav').suffix}"
    ref_path.write_bytes(await ref_voice.read())

    from app.services.msi_voice_service import voice_clone_to_r2, XTTSError
    try:
        key, url = voice_clone_to_r2(text, ref_path, current_user.id, language=language)
    except XTTSError as e:
        raise HTTPException(503, detail=f"XTTS on MSI failed: {e}")
    finally:
        try:
            ref_path.unlink(missing_ok=True)
            workdir.rmdir()
        except OSError:
            pass

    return VoiceCloneResponse(
        storage_key=key, media_url=url,
        text_length=len(text), language=language,
    )
