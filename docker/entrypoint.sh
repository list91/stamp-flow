#!/bin/sh
# Любая команда контейнера сначала получает рабочий SSH-ключ в домашнем каталоге пользователя app.
set -eu

if [ "$(id -u)" -eq 0 ]; then
    su app -s /bin/sh -c 'sh /app/docker/prepare-ssh.sh' || true
else
    sh /app/docker/prepare-ssh.sh || true
fi

exec "$@"