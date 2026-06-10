"""
Сервис ремейка — связывает ContentRecipe + RemakeParams + provider →
создаёт GeneratedVideo + ParseJob(GENERATE_VIDEO) с rendered prompt.

Сам worker — тот же generation_worker (PR #2). Не дублируем код.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.recipe import ContentRecipe
from app.models.reel import Reel
from app.models.generation import (
    GeneratedVideo, GenerationStatus, VideoProvider,
)
from app.models.parsing import ParseJob, JobStatus, JobType
from app.services.tariff_service import get_priority
from app.core.composer import RemakeParams, render_prompt, build_prompt_from_recipe_dict

logger = logging.getLogger(__name__)


def create_remake_job(
    db: Session,
    user: User,
    *,
    recipe: Optional[ContentRecipe] = None,
    source_reel: Optional[Reel] = None,
    params: Optional[RemakeParams] = None,
    provider: VideoProvider = VideoProvider.RUNWAY,
    aspect_ratio: str = "9:16",
    duration_seconds: int = 5,
    model: Optional[str] = None,
    use_hybrid: bool = False,
) -> GeneratedVideo:
    """Создать GeneratedVideo с подменёнными плейсхолдерами + поставить в очередь.

    Хотя бы один из {recipe, source_reel} должен быть задан. Если оба —
    приоритет у recipe (он структурированный), source_reel становится
    ссылкой для трекинга.
    """
    if not recipe and not source_reel:
        raise ValueError("Нужен либо recipe, либо source_reel")
    params = params or RemakeParams()

    # Render prompt
    if recipe and recipe.canonical_prompt:
        prompt = render_prompt(recipe.canonical_prompt, params)
        logger.info(f"remake from recipe #{recipe.id} (canonical)")
    elif recipe:
        # Recipe есть, но canonical_prompt не сгенерён — собираем из полей.
        recipe_dict = {
            "name": recipe.name,
            "hook": recipe.hook,
            "structure": recipe.structure,
            "visual_motifs": recipe.visual_motifs,
            "audio_strategy": recipe.audio_strategy,
            "cta": recipe.cta,
        }
        prompt = build_prompt_from_recipe_dict(recipe_dict, params)
        logger.info(f"remake from recipe #{recipe.id} (synthesized prompt)")
    else:
        # Только source_reel — используем visual_summary как опору.
        base = (source_reel.visual_summary or source_reel.caption
                or source_reel.title or "viral short-form video")
        prompt = (
            f"Recreate the format of this video: {base}\n\n"
            f"Substitute the original person with [FACE], the product with "
            f"[PRODUCT], the brand with [BRAND]. Keep camera moves, "
            f"lighting and structure recognisable but produce a new clip."
        )
        prompt = render_prompt(prompt, params)
        logger.info(f"remake from raw reel #{source_reel.id} (no recipe)")

    provider_params = {
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds,
        "remake_params": {
            k: v for k, v in params.__dict__.items() if v is not None
        },
    }
    if model:
        provider_params["model"] = model
    # PR #20 — image-to-video: первый кадр источника проложен в generator
    if params.init_image_url:
        provider_params["init_image_url"] = params.init_image_url
        logger.info("remake using init_image_url for image-to-video")

    gv = GeneratedVideo(
        user_id=user.id,
        prompt=prompt,
        provider=provider,
        status=GenerationStatus.PENDING,
        provider_params=provider_params,
        source_reel_id=source_reel.id if source_reel else (recipe.source_reel_id if recipe else None),
        source_recipe_id=recipe.id if recipe else None,
    )
    db.add(gv)
    db.flush()

    job_type_to_use = JobType.REMAKE_VIDEO if use_hybrid else JobType.GENERATE_VIDEO
    job = ParseJob(
        reel_id=None,
        user_id=user.id,
        generated_video_id=gv.id,
        job_type=job_type_to_use,
        status=JobStatus.PENDING,
        priority=get_priority(user),
    )
    db.add(job)
    db.commit()
    db.refresh(gv)
    logger.info(f"✅ Remake gv #{gv.id} queued (job #{job.id})")
    return gv
