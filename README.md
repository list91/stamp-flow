# stamp-flow

Автоотклики на hh.ru с ревью в Telegram. Работает фоном и зовёт тебя только когда нужен человек: письмо, анкета, подозрительная вакансия, капча.

Это надстройка над [hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool) (автор s3rgeym). Код апстрима в этом репозитории не лежит: он ставится как зависимость на закреплённом коммите, без изменений его файлов. Условия апстрима: [NOTICE.md](NOTICE.md).

> Автоматизация откликов нарушает правила hh.ru: аккаунт могут заблокировать, риск на тебе. Проект не аффилирован ни с hh.ru, ни с автором hh-applicant-tool.

## Как работает

1. cron каждые N минут запускает обход: по каждому твоему резюме берёт новые вакансии, между откликами случайные паузы.
2. Детектор prompt-инъекций (эвристики и LLM) и оценка релевантности LLM.
3. Чистые вакансии: отклик без письма автоматически.
4. Нужно письмо или анкета: LLM пишет черновик в твоём стиле (`config/style.md`), в Telegram приходит карточка: апрув, эдит или реджект.
5. Второй бот только пингует, когда нужен ты (необязателен).

Подробно: [docs/review.md](docs/review.md).

## Что заполнить

Файлов с резюме нет: текст резюме берётся с hh через API.

| Что | Куда | Как |
|---|---|---|
| Опубликованное резюме | hh.ru | чем больше резюме, тем больше кругов ротации |
| Переменные окружения | `.env` | копия `.env.example`: `HH_SSH_DIR`, `HH_SSH_KEY`, `TZ`, на Linux `APP_UID`/`APP_GID` |
| Telegram-бот | `config/config.json`: `review.telegram` | @BotFather; `chat_id`: напиши боту и открой `https://api.telegram.org/bot<TOKEN>/getUpdates`; нажми Start у бота |
| Второй бот (необязательно) | `config/config.json`: секция `review.notify` с `bot_token` и `chat_id` | в образце её нет: добавь, только когда создал второго бота; он шлёт уведомления «нужно участие» и ошибки |
| Хост с OpenCode | `config/config.json`: `llm` | любая Linux или macOS машина с `sshd`, `python3` и запущенным `opencode serve`. Поля: `host`, `user`, `key_file` (ключ без пароля, в контейнере `~/.ssh/<имя из HH_SSH_KEY>`), `model` в виде `provider/model`; необязательно `port` SSH (22) и `api_url` (по умолчанию `http://127.0.0.1:4096` на самом хосте) |
| Стиль писем и анкет | `config/style.md` | образец `examples/style.example.md`; единственный полностью личный файл |
| Токены hh и куки | `config/` | создаёт `auth` |
| Параметры обхода | `conf.json` | `search.work_format`, `rotation`, `timeouts_sec`, `limits.max_per_hour`, `relevance.apply_from` |
| Предпочтения по роли | `config/config.json`: `review.relevance_profile` | необязательно, свободный текст |

## Хост с OpenCode

Нужна машина (VPS, домашний сервер, NAS), на которую stamp-flow заходит по SSH и где работает [OpenCode](https://opencode.ai):

1. Установи OpenCode, подключи провайдера и выбери модель. Её id вида `provider/model` пойдёт в `llm.model`.
2. Держи запущенным `opencode serve --port 4096` (systemd или аналог). Сервер слушает `127.0.0.1`, наружу порт открывать не нужно: stamp-flow обращается к нему через SSH. Пароль сервера (`OPENCODE_SERVER_PASSWORD`) драйвер не поддерживает.
3. Создай ключ без пароля: `ssh-keygen -t ed25519 -N "" -f id_ed25519`. Публичную часть добавь в `~/.ssh/authorized_keys` на хосте, приватную положи в каталог `HH_SSH_DIR`.
4. Проверь связку командой `llm --meta "ping"` (см. «Запуск»).

## Запуск

```sh
git clone https://github.com/list91/stamp-flow && cd stamp-flow
cp .env.example .env                                  # заполнить
cp examples/config.example.json config/config.json    # заполнить
cp examples/style.example.md config/style.md          # заполнить
docker compose build
docker compose run --rm -u app cron python -m stamp_flow -vv auth -k          # вход в hh
docker compose run --rm -u app cron python -m stamp_flow llm --meta "ping"    # проверка LLM
docker compose run --rm -u app cron python -m stamp_flow apply-review --dry-run --max-responses 3
python run.py                                         # по conf.json: пишет config/crontab, поднимает cron и бот
```

Если при входе в `auth` hh покажет капчу, её нужно вывести в терминал: `-k` для kitty, `-s` для sixel. Режимы `-m` и `-n` открывают окно браузера и в контейнере не работают: для них запусти `auth` на хосте (`pip install -e .`, затем `python -m stamp_flow --config-dir config auth -m`), токены и куки лягут в тот же `config/`.

Нужны Docker с Compose v2 и Python 3.11+ на хосте (для `run.py`). Если в `conf.json` стоит `"loop": false`, обход идёт на хосте, тогда сначала `pip install -e .`. Не запускай обход на хосте и в контейнере одновременно: общая БД и lock-файлы через bind-mount ненадёжны на Windows и macOS, там оставь `"loop": true`. На Linux каталог `config/` должен принадлежать пользователю с `APP_UID`/`APP_GID`.

## Заметки

- LLM-драйвер один: `opencode_ssh` (SSH на хост с OpenCode). Свой драйвер: класс с `from_config` и `complete(system, user, *, temperature=None)`, регистрация в `DRIVERS` (`src/stamp_flow/review/llm.py`).
- Резюме, тексты вакансий и личные факты из `style.md` уходят в LLM и в Telegram. Журнал (`config/data`, `config/review-log/`) бессрочный и хранит письма, промпты и ответы моделей. Каталог `config/` не коммить.
- Главный риск блокировки hh: жалобы работодателей. Держи узкий профиль, лимиты и ручное ревью писем.
- `llm.delete_session` по умолчанию `true`: сессии OpenCode удаляются после ответа.
- Не публикуй собранные Docker-образы: в них лежит код апстрима.

## Лицензия

Код этого репозитория распространяется на условиях MIT, см. [LICENSE](LICENSE). hh-applicant-tool в него не входит и действует на условиях его автора: см. [NOTICE.md](NOTICE.md).

## Разработка

```sh
pip install -e ".[dev]"
pytest
```