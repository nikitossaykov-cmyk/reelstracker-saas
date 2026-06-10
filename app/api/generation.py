"""
API для генерации видео: POST /api/generation, GET список + детальная.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.generation import VideoProvider
from app.schemas.generation import (
    GenerationCreate,
    GeneratedVideoResponse,
    GenerationListResponse,
)
from app.services.generation_service import (
    create_generation_job,
    get_user_generations,
    get_generation_by_id,
)

router = APIRouter()


@router.post("", response_model=GeneratedVideoResponse, status_code=status.HTTP_201_CREATED)
def create_generation(
    data: GenerationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Поставить генерацию видео в очередь.

    Provider Runway требует наличия user.runway_api_key — иначе worker
    зафейлит задачу с понятной ошибкой. Чтобы не давать юзеру ставить
    заведомо-провальные задачи, проверяем ключ заранее.
    """
    if data.provider == VideoProvider.RUNWAY and not current_user.runway_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Для провайдера 'runway' нужно сначала сохранить runway_api_key "
                   "в настройках профиля.",
        )
    gv = create_generation_job(
        db=db,
        user=current_user,
        prompt=data.prompt,
        provider=data.provider,
        aspect_ratio=data.aspect_ratio,
        duration_seconds=data.duration_seconds,
        init_image_url=data.init_image_url,
        seed=data.seed,
        model=data.model,
    )
    return gv


@router.get("", response_model=GenerationListResponse)
def list_generations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список генераций юзера, новые сверху."""
    items, total = get_user_generations(db, current_user, limit=limit, offset=offset)
    return GenerationListResponse(items=items, total=total)


@router.get("/{generation_id}", response_model=GeneratedVideoResponse)
def get_generation(
    generation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Детальная информация об одной генерации (для polling из UI)."""
    gv = get_generation_by_id(db, generation_id, current_user)
    if gv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation #{generation_id} не найдена",
        )
    return gv


@router.post("/{generation_id}/face-swap", response_model=GeneratedVideoResponse)
async def face_swap_generation(
    generation_id: int,
    face_image: "UploadFile" = None,
    face_image_url: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Подменить лицо в сгенерированном видео через MSI ComfyUI ReActor.

    Бесплатно (RTX 3070 на ноуте через Tailscale). Альтернатива Runway/Veo
    когда нужно ТОЛЬКО поменять лицо в готовом видео (не пересоздавать его).

    Body: либо multipart с file 'face_image', либо JSON {face_image_url}.
    Результат пишется в uniq_storage_key (поскольку это та же сущность —
    модифицированная копия media). Оригинал остаётся в media_storage_key.
    """
    from pathlib import Path
    import tempfile
    gv = get_generation_by_id(db, generation_id, current_user)
    if gv is None:
        raise HTTPException(404, detail=f"Generation #{generation_id} не найдена")
    if not gv.media_storage_key:
        raise HTTPException(400, detail="Видео ещё не сгенерировано")

    # Сохранить face в /tmp
    workdir = Path(tempfile.mkdtemp(prefix=f"face_in_{gv.id}_"))
    face_path = workdir / "face.jpg"
    if face_image:
        data = await face_image.read()
        face_path.write_bytes(data)
    elif face_image_url:
        import requests
        r = requests.get(face_image_url, timeout=60)
        r.raise_for_status()
        face_path.write_bytes(r.content)
    else:
        raise HTTPException(400, detail="Нужно либо face_image (multipart), либо face_image_url")

    from app.services.msi_face_swap_service import face_swap_via_msi, MSINotReachable, ComfyWorkflowError
    try:
        face_swap_via_msi(db, gv, face_path)
    except MSINotReachable as e:
        raise HTTPException(503, detail=f"MSI ComfyUI недоступен: {e}")
    except ComfyWorkflowError as e:
        raise HTTPException(500, detail=f"MSI workflow упал: {e}")

    db.refresh(gv)
    return gv


@router.post("/{generation_id}/uniqify", response_model=GeneratedVideoResponse)
def uniqify_generation(
    generation_id: int,
    overwrite: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Применить anti-fingerprint preset (hue/scale/rotate/speed/noise/pitch)
    к media_storage_key. Создаёт отдельную uniq-копию в R2 (оригинал остаётся).

    Используется перед публикацией, чтобы перцептивный хэш и audio
    fingerprint не совпадали с источником.
    """
    gv = get_generation_by_id(db, generation_id, current_user)
    if gv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Generation #{generation_id} не найдена")
    if not gv.media_storage_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Видео ещё не сгенерировано (media_storage_key пуст)")
    from app.services.uniqify_service import uniqify_generated_video
    from app.core.uniqualizer import UniqifyError
    try:
        uniqify_generated_video(db, gv, overwrite=overwrite)
    except UniqifyError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    db.refresh(gv)
    return gv
