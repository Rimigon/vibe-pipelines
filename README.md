# vibe-pipelines

**Устойчивые мультимодельные генеративные пайплайны поверх Agent API Вайб-Маркетолог.**

Это ответ на тестовое задание вакансии *AI-разработчик / LLM Engineer*
(Вайб-Маркетолог). Задание открытое — «предложите любую полезную функцию для
нашего API». Я сделал не идею, а работающую библиотеку, которая закрывает
задокументированную боль API, плюс продуктовое предложение, которое можно
забрать в платформу.

> TL;DR: API даёт «кирпичи» (`/generate`, `/estimate`, `/upload-media`, webhooks),
> но не «здание». Мультимодельный сценарий («постер → видео → озвучка → музыка»)
> сегодня — ручная склейка асинхронных шагов на клиенте, с передачей протухающих
> промежуточных URL и ловушкой имён полей, которая **молча списывает деньги**.
> `vibe-pipelines` делает эту склейку надёжной и декларативной.

---

## Что закрывает (каждая боль — из документации)

### 1. Footgun имён полей — молчаливая плата за неправильное поле

Документация прямо предупреждает:

> «Самая частая ошибка агентов — слать `image_input` для видео. Видео-модели
> его не читают → чистый text-to-video, а деньги спишутся.»

Разные видео-модели читают исходную картинку под **разными** именами:
`image_urls` (Veo/Kling/Grok), `first_frame_url` (Seedance), `image_url`
(Omnihuman, singular), `character_image_url` (Motion Control), `image_input`
(только `type=image`). Ошибка в имени → деньги за неверный результат.

**Решение:** автор пайплайна пишет только **логические** входы —
`source_image`, `source_audio`, `source_video`. Библиотека тянет
`GET /capabilities`, строит схему каждой модели и маппит логический вход на то
физическое поле, которое модель реально принимает (см. `vibe/capabilities.py`,
тест `test_capabilities.py`). Перед запуском — `strict=true` валидация.
Невозможно случайно слить деньги на text-to-video вместо image-to-video.

### 2. Нет абстракции пайплайна — ручная склейка async-шагов

Генерация асинхронна (видео — до 30 мин). Многошаговый сценарий требует:
поллинг/вебхук каждого шага → передачу промежуточного `display_url` (живёт 7
дней) в следующий → обработку ошибок и refunds. Всё это на клиенте.

**Решение:** декларативный DAG. Независимые шаги идут параллельно
(`asyncio.gather`), зависимые стартуют после `complete` зависимостей, и им
автоматически подставляется свежий `display_url` (см. `vibe/pipeline.py`).

### 3. Бюджет — first-class concern, но без умного оркестратора

Рубли, дневные лимиты, refunds — но «считать себестоимость сценария» разработчик
должен сам.

**Решение:** перед запуском весь сценарий прогоняется через бесплатный
`/generate/estimate`. Если сумма > `budget_rub` — `BudgetExceeded` с
пошаговым breakdown и **ни одного вызова `/generate`**. Во время выполнения
учитывается *net*-spend (gross − refunded). См. `vibe/budget.py`,
`test_budget_gate_blocks_run_before_any_charge`.

### 4. Маршрутизация моделей и fallback

170+ моделей — выбор по цена/качество/скорость ложится на каждого заново.

**Решение:** `vibe/router.py` строит fallback-цепочку по тиру
(`economy`/`balanced`/`quality`) и бюджету; при retryable-сбое шаг
перепробует следующую модель в цепочке (multi-provider routing на стороне агента
— дополнение к внутреннему резерву платформы).

### 5. Восстановление после сбоя + идемпотентность

Упавший процесс посередине сценария = потерянные деньги и прогресс.

**Решение:** каждый шаг получает стабильный `idempotency_key` (`run_id:step_id`);
состояние чекпоинтится в JSON после каждого перехода. При resume завершённые
шаги пропускаются (их URL переиспользуется), in-flight — переполлингиваются
(платформа вернёт `replayed:true` без нового списания). См. `vibe/state.py`,
`test_resume_skips_completed_step`.

### 6. Наблюдаемость

Каждый шаг пишет JSONL-запись: `step, model, type, request_id, generation_id,
status, cost, refunded, attempts, error, duration`. Это «журналирование шагов /
трассировка» из вакансии — основа для evals и постмортем. См. `vibe/journal.py`.

### 7. Webhooks

`vibe/webhooks.py` — верификация HMAC-SHA256 (и modern `webhook_secret`, и
legacy `sha256(raw_token)`) на **raw bytes до JSON-парсинга**, плюс мини
aiohttp-приёмник. Опционально заменяет поллинг.

---

## Установка

```bash
pip install -e ".[dev]"          # httpx, typer, pydantic, pyyaml + тесты
export VIBE_TOKEN=oc_...         # ключ со страницы lk.vibemarketolog.ru/#agent
```

## Python API

```python
import asyncio
from vibe import Pipeline, Step, VibeClient

async def main():
    p = Pipeline(budget_rub=480)

    poster = Step(id="poster", type="image", model="seedream-5-pro",
                  prompt="баннер салона красоты, неон",
                  params={"aspect_ratio": "1:1", "quality": "high"})
    jingle = Step(id="jingle", type="music", model="suno-v5.5-instrumental",
                  prompt="upbeat pop-джингл, 15 сек")
    clip = Step(id="clip", type="video", model="seedance-2-mini",
                prompt="камера медленно наезжает на светящийся неоновый баннер",
                inputs={"source_image": "${poster}"},          # ← логический вход
                params={"duration": 4, "aspect_ratio": "9:16", "resolution": "480p"}).depends_on("poster")

    p.add(poster, jingle, clip)
    async with VibeClient(VIBE_TOKEN) as client:
        outputs = await p.run(client)
        for sid, url in outputs.items():
            print(sid, url)

asyncio.run(main())
```

## CLI (для маркетологов — без кода)

```bash
vibe run examples/marketing_reel.yaml
vibe estimate examples/marketing_reel.yaml   # смета без списания
vibe models --type video                      # каталог, сортировка по цене
vibe balance
```

## Тесты

```bash
pytest -q        # 38 тестов, моки из документации, ничего не тратит
```

Тесты покрывают: маппинг полей под все видео-семейства (footgun-киллер),
бюджетный шлюз (ни одного `/generate` при превышении), порядок зависимостей,
resume после сбоя, retry с `retry_after`, идемпотентность, long-voiceover,
верификацию вебхуков (modern + legacy, tamper-detection).

---

## Структура

```
vibe/
  client.py          # async httpx-обёртка: retry/backoff/retry_after, идемпотентность, типизированные ошибки
  capabilities.py    # реестр моделей + маппер логических→физических полей (киллер footgun'а)
  router.py          # маршрутизатор моделей + fallback-цепочка
  steps.py           # Step: логические входы, ссылки ${step_id}
  pipeline.py        # DAG-исполнитель: topo-порядок, параллельность, бюджет, журнал, resume
  budget.py          # estimate-before-run + refund-aware net spend
  journal.py         # JSONL-трассировка шагов
  state.py           # чекпоинт + resume после сбоя
  webhooks.py        # HMAC-верификация + мини-приёмник
  cli.py             # typer: run / estimate / models / balance
docs/
  proposal-pipeline-endpoint.md   # продуктовое предложение: server-side /generate/pipeline
  api-review.md                    # мнение про API (ответ на вопрос №2 из вакансии)
examples/  marketing_reel.{py,yaml}
demo/      RUNBOOK.md              # как прогнать живьём на балансе HH500
tests/     38 тестов
```

## Продуктовое предложение

`docs/proposal-pipeline-endpoint.md` — предложение server-side
`POST /generate/pipeline`: DAG на стороне платформы, одна оплата, один вебхук по
завершению всего, промежуточные файлы не протухают. Эта библиотека —
референс-реализация того же паттерна сегодня, доказывающая спрос и UX.

## Честные ограничения

- Я не регистрировался и не тратил чужой баланс: код писался по документации,
  тесты — на моках с реальными формами ответов. Живой прогон делаете вы на
  балансе HH500 (см. `demo/RUNBOOK.md`).
- pyrightconfig.json указывает на локальный venv только для статического
  анализа; для работы он не нужен (`pip install -e .[dev]` ставит всё).

## Лицензия

MIT — забирайте в продукт.
