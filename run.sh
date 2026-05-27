#!/bin/bash
# launchd ラッパー: 10秒おきに呼ばれる。
# state.json を GitHub と双方向同期して、GitHub Actions と協調動作する。
set -e
cd "$(dirname "$0")"

SYNC_INTERVAL=300  # 5分に1回 git pull/push（heartbeat）
LAST_SYNC_FILE=".last_sync"
NOW=$(date +%s)
LAST=$(cat "$LAST_SYNC_FILE" 2>/dev/null || echo 0)
DO_SYNC=0
if (( NOW - LAST >= SYNC_INTERVAL )); then
    DO_SYNC=1
fi

# 同期タイミングなら git pull でリモートを取り込む
if (( DO_SYNC == 1 )); then
    git pull --rebase --autostash >/dev/null 2>&1 || true
fi

# .env 読み込み
set -a
. ./.env
set +a

# monitor.py 実行
/opt/homebrew/bin/python3 monitor.py

# 同期タイミングかつ state.json に変更があれば push
if (( DO_SYNC == 1 )) && [[ -n "$(git status --porcelain state.json)" ]]; then
    git config user.name "takuya057"
    git config user.email "takuya057@users.noreply.github.com"
    git add state.json
    git commit -m "chore: Mac heartbeat" -q
    git push -q 2>/dev/null || true
    echo "$NOW" > "$LAST_SYNC_FILE"
elif (( DO_SYNC == 1 )); then
    # pull だけして変更なし、でも次回判定のため記録
    echo "$NOW" > "$LAST_SYNC_FILE"
fi
