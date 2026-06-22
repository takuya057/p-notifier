#!/bin/bash
# launchd ラッパー: 10秒おきに呼ばれる。
# state.json を GitHub と双方向同期して、GitHub Actions と協調動作する。
set -e
cd "$(dirname "$0")"

# === 排他制御 (atomic な mkdir でロック) ===
# launchdが何らかの理由で同時起動した場合・前回が遅延して並行になった場合に、
# state.jsonへの同時アクセスで重複通知が起きるのを防ぐ。
LOCK_DIR="/tmp/p-notifier-mac.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # 既に他インスタンスが動いている → 静かに終了
    # ただし、stale lock (古いロック) チェック: 5分以上前のロックは強制削除
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

SYNC_INTERVAL=300  # 5分に1回 git pull/push
LAST_SYNC_FILE=".last_sync"
NOW=$(date +%s)
LAST=$(cat "$LAST_SYNC_FILE" 2>/dev/null || echo 0)
DO_SYNC=0
if (( NOW - LAST >= SYNC_INTERVAL )); then
    DO_SYNC=1
fi

if (( DO_SYNC == 1 )); then
    git fetch --quiet 2>/dev/null || true
    # ローカル(HEAD)が リモート(origin/main) の祖先 or 同じ場合のみ state.json を取り込む。
    # ローカルが進んでる場合は state.json を巻き戻さない (重複通知防止)。
    if git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
        git checkout origin/main -- state.json 2>/dev/null || true
    fi
fi

set -a
. ./.env
set +a
/opt/homebrew/bin/python3 monitor.py

# 同期タイミングで、state.json に変更があれば push
if (( DO_SYNC == 1 )); then
    if [[ -n "$(git status --porcelain state.json)" ]]; then
        git config user.name "takuya057"
        git config user.email "takuya057@users.noreply.github.com"
        git add state.json
        git commit -m "chore: Mac heartbeat" -q 2>/dev/null
        # push 失敗時はリモートが進んでたケース → rebase で取り込んで再push
        if ! git push -q 2>/dev/null; then
            git fetch --quiet 2>/dev/null
            git rebase --strategy-option=ours origin/main --quiet 2>/dev/null \
                || git rebase --abort 2>/dev/null
            git push -q 2>/dev/null || true
        fi
    fi
    echo "$NOW" > "$LAST_SYNC_FILE"
fi
