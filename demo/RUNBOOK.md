# RUNBOOK: живой прогон на балансе HH500

В этом репозитории код писался по документации, тесты — на моках (ничего не
тратят). Чтобы получить реальный результат и ссылки на файлы, прогон делает
владелец аккаунта на своём балансе. Промокод **HH500** даёт 500 ₽ — демо
укладывается в ~200 ₽ (реальный прогон — 195 ₽).

## 1. Подготовка (один раз)

1. Зарегистрируйтесь на `vibemarketolog.ru`, активируйте промокод **HH500**
   (500 ₽ на баланс).
2. Создайте API-ключ на `lk.vibemarketolog.ru/#agent` (раздел «API-ключи»).
   - Ключ показывается **один раз** — сохраните сразу.
   - По умолчанию у нового ключа права `read` + `generate` — этого достаточно.
   - Запишите `webhook_secret` (нужен только если будете тестировать вебхуки).
3. Проверьте ключ:

   ```bash
   curl -s -H "Authorization: Bearer $VIBE_TOKEN" https://lk.vibemarketolog.ru/api/agent/me
   ```

## 2. Установка библиотеки

```bash
cd vibe-pipelines
python -m pip install -e ".[dev]"
export VIBE_TOKEN=oc_...        # ваш ключ
```

## 3. Смета без списания (dry-run)

```bash
vibe estimate examples/marketing_reel.yaml
```

Ожидаемо: poster (SeeDream 5 Pro) + jingle (Suno V5.5) + clip (Seedance 2 Mini,
4с, 480p) ≈ 195 ₽. Если смета > вашего остатка — поднимите `budget_rub` или
понизьте `duration`/тир.

## 4. Живой прогон

```bash
vibe run examples/marketing_reel.yaml
```

Что произойдёт (по шагам, с трассировкой в `vibe-run.jsonl`):

1. `GET /capabilities` — схема моделей.
2. `POST /generate/estimate` × 3 — смета, проверка бюджета (не списывает).
3. `POST /generate` для `poster` и `jingle` параллельно (idempotency_key на
   каждый).
4. Поллинг `/generation/{id}/status` каждые ~12с до `complete`.
5. `POST /generate` для `clip` — `source_image` автоматически маппится в
   `first_frame_url` (Seedance), туда подставляется `display_url` постера.
6. Вывод трёх подписанных ссылок (работают без логина 7 дней).

## 5. Проверка устойчивости

- **Упало посередине?** Перезапустите с тем же `run_id` (он в выводе):
  `vibe run examples/marketing_reel.yaml --run-id <id>`.
  Завершённые шаги пропустятся, in-flight переполнятся, деньги не спишутся
  дважды (идемпотентность).
- **Превышение бюджета?** Поставьте `budget_rub: 10` — получите
  `BudgetExceeded` с breakdown и ни одного вызова `/generate`.

## 6. Что приложить в ответ на задание

- Ссылку на этот репозиторий.
- Вывод `vibe estimate ...` (смета).
- Вывод `vibe run ...` (три `display_url`).
- (опц.) Скриншоты/ссылки на готовые файлы в галерее ЛК.

## Честно

- Я не могу прогнать это за вас: нужны ваш аккаунт, промокод и ключ.
- Если живой прогон выявит расхождение с докой (например, поле модели или
  форма ответа отличаются) — это ценно; приложите вывод, я поправлю маппинг
  по реальному `/capabilities`.

---

## Результат живого прогона (HH500, 2026-07-28)

Реальный прогон `examples/marketing_reel.yaml` на балансе промокода HH500:

```
$ vibe run examples/marketing_reel.yaml --run-id 8dc0ec9c384c
run_id=8dc0ec9c384c  (resume: ...)
• run_id=8dc0ec9c384c budget=480.0₽ steps=['poster', 'jingle', 'clip']
• estimate: 56.0₽ (budget gate)
▶ clip       seedance-2-mini …
✓ clip       seedance-2-mini  56.0₽
• done: net_cost=195.0₽
poster: https://lk.vibemarketolog.ru/files/generation/25862?expires=…&signature=…
jingle: https://lk.vibemarketolog.ru/files/generation/25863?expires=…&signature=…
clip:   https://lk.vibemarketolog.ru/files/generation/25893?expires=…&signature=…
```

**Что произошло по шагам:**

1. Первый прогон упал на `clip` с ошибкой Seedance `captions are not enough or
   empty` (слишком короткий промпт). `poster` (40₽) и `jingle` (99₽) успели
   завершиться и были сохранены в state-файл `.vibe-state/8dc0ec9c384c.json`.
   Упавший клип (56₽) вернулся (`refunded`).
2. Промпт клипа переписан в более детальный, библиотека обновлена (свежий
   idempotency-key на повтор упавшего шага + in-flight resume).
3. `vibe run --run-id 8dc0ec9c384c` возобновил прогон: `poster` и `jingle`
   **пропущены без повторной оплаты**, перезапущен только `clip` — с новым
   промптом и новым ключом (без 409-конфликта). Успех, 56₽.

**Итог:** 195₽ из 500₽, три готовых файла (image + music + video), ни одной
лишней генерации. Это и есть «восстановление состояния после сбоя» и
«идемпотентность» на живом API.
