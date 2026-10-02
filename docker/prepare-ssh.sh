#!/bin/sh
# Готовит ~/.ssh текущего пользователя для LLM-драйвера opencode_ssh.
# Каталог ключей хоста монтируется read-only в /keys (с Windows права 0777 — ssh такие
# отвергает), поэтому копируем с правами 600.
set -eu

SRC=/keys
DST="$HOME/.ssh"

mkdir -p "$DST"
chmod 700 "$DST"

# Копируем только нужный ключ (имя — как в llm.key_file config.json)
KEY="${HH_SSH_KEY:-id_ed25519}"
if [ -f "$SRC/$KEY" ]; then
    # CRLF с Windows ломает ключ
    tr -d '\r' < "$SRC/$KEY" > "$DST/$KEY"
    chmod 600 "$DST/$KEY"
else
    echo "prepare-ssh: нет ключа $SRC/$KEY — LLM-драйвер opencode_ssh не подключится" >&2
fi

if [ ! -f "$DST/config" ]; then
    printf 'Host *\n    StrictHostKeyChecking accept-new\n    UserKnownHostsFile %s/known_hosts\n' "$DST" > "$DST/config"
    chmod 600 "$DST/config"
fi
