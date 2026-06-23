#!/bin/bash
# launchd ラッパー: 10秒おきに呼ばれる。
# state.json はローカルだけで永続化 (git 同期しない → 重複通知防止)
set -e
cd "$(dirname "$0")"

# === 排他制御 (atomic な mkdir でロック) ===
# launchdが何らかの理由で同時起動した場合・前回が遅延して並行になった場合に、
# state.jsonへの同時アクセスで重複通知が起きるのを防ぐ。
LOCK_DIR="/tmp/p-notifier-mac.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # stale lock (5分以上前のロック) は強制削除
    if [ -d "$LOCK_DIR" ]; then
        LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
        if [ "$LOCK_AGE" -gt 300 ]; then
            rmdir "$LOCK_DIR" 2>/dev/null
            mkdir "$LOCK_DIR" 2>/dev/null || exit 0
        else
            exit 0
        fi
    else
        exit 0
    fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM

# .env 読み込み & monitor.py 実行
set -a
. ./.env
set +a
exec /opt/homebrew/bin/python3 monitor.py
