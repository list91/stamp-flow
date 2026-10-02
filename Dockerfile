FROM python:3.13-slim

ARG APP_UID=1000
ARG APP_GID=1000

# cron для расписания, git для установки апстрима, ssh-клиент для хоста с LLM
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron git openssh-client procps tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid app --create-home --shell /bin/bash app

WORKDIR /app

# stamp-flow и hh-applicant-tool (зависимость на закреплённом коммите, см. pyproject.toml)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Chromium для входа в hh и показа капчи; каталог браузеров общий для root и app
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright

COPY docker ./docker

ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
CMD ["sh", "/app/docker/cron-main.sh"]