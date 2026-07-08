# Studio: постоянная девушка (persona)

**Дата:** 2026-07-08. **Одобрено Ником** («Вот она выглядит естественно… можно ли добавить чтобы она осталась» + «Да»).

## Проблема

Каждый job генерит портрет с нуля → каждый рилс с новой девушкой. Нику
понравилась девушка из job#4 — она должна оставаться во всех следующих
рилсах, с возможностью менять позу/одежду и, при желании, вернуться к
генерации нового лица.

## Ключевое ограничение

Портрет всегда содержит продукт в руке (label facing camera), поэтому
«переиспользовать девушку» ≠ скопировать байты портрета: для нового
продукта нужен новый nano-banana pass. Решение — identity-референс:
портрет персоны идёт ПЕРВЫМ image_input, фото продукта ВТОРЫМ (приём,
проверенный катавеями и v36). Стоимость портрета остаётся $0.15/job.

## Дизайн

### Хранение
- `users.studio_persona_key VARCHAR(512) NULL` — R2-ключ канонического
  портрета персоны. Один слот на юзера. Миграция lightweight.
- `studio_jobs.use_persona BOOLEAN NOT NULL DEFAULT TRUE`
- `studio_jobs.look_prompt TEXT NULL` — описание нового образа
  («белый топ, волосы собраны»), только при use_persona.

### Семантика персоны (анти-дрифт)
- Канон НЕ обновляется при обычных persona-job'ах — каждый рилс
  референсится на один и тот же исходный портрет, деградации
  копия-с-копии нет.
- Канон обновляется автоматически ТОЛЬКО когда job с use_persona +
  look_prompt успешно сгенерил портрет (новый образ = новый канон).
- Явное сохранение: POST /api/studio/persona {job_id} — «сделать
  девушку из этого job'а постоянной» (так же сидируем job#4).
  Job должен быть свой и иметь portrait_key.
- «Новое лицо» (use_persona=false) канон НЕ трогает — эксперимент не
  теряет сохранённую девушку; понравилась — сохраняешь кнопкой.

### Пайплайн (worker, стадия PORTRAIT)
- use_persona и у юзера есть persona_key → generate_persona_portrait
  (image_input=[persona, product]): SAME woman as first reference —
  same face and hair; либо «keep hairstyle, outfit, room and lighting
  exactly as in the first reference», либо «change her look:
  {look_prompt}» + прежние требования к этикетке/тексту + 9:16.
- use_persona, но персоны нет → фолбэк на текущий generate_studio_portrait
  (не фейлим job).
- use_persona=false → текущий путь без изменений.
- Остальной пайплайн не меняется: липсинк и катавеи уже берут
  j.portrait_key → та же девушка везде автоматически.

### API
- `GET /api/studio/persona` → `{persona_key: str|null}`
- `POST /api/studio/persona` body `{job_id}` → сохранить, вернуть key
- `POST /api/studio/jobs/`: + `use_persona: bool = Form(True)`,
  `look_prompt: Optional[str] = Form(None)`
- media allowlist: + `User.studio_persona_key == key` (retry чистит
  portrait_key джоба, ключ персоны должен оставаться валидным).

### UI (static/studio.html)
- Радио «Девушка»: «та же» (дефолт; disabled если персоны нет) /
  «сменить образ» (+ текстовое поле) / «новое лицо».
- На карточке job'а с portrait_key — кнопка «сохранить девушку».

## Вне скоупа
- Несколько персон на юзера, галерея образов, удаление персоны.
