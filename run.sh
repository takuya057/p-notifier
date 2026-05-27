#!/bin/bash
# launchd ラッパー: 10秒おきに呼ばれる。
# state.json を GitHub と双方向同期して、GitHub Actions と協調動作する。
#
# 同期戦略 (autostash conflictを回避):
#   - 5分に1回だけ git ops を実行
#   - リモートが進んでいたら、ローカル変更を捨てて完全に合わせる
#     (GHAが進んでる = GHAが最新のpaymentを既に通知済み = Macは捨てて問題なし)
#   - ローカルだけ進んでいたら、push する
set -e
cd "$(dirname "$0")"

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
    LOCAL_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
    REMOTE_SHA=$(git rev-parse origin/main 2>/dev/null || echo "")

    if [ -n "$LOCAL_SHA" ] && [ -n "$REMOTE_SHA" ] && [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
        # リモートが進んでる → state.jsonだけリモート版に上書き
        # (run.sh等のコードはローカル優先で残す)
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
        # push 失敗時 (リモートが先に進んでた場合) は次回のfetchで取り込む
        git push -q 2>/dev/null || true
    fi
    echo "$NOW" > "$LAST_SYNC_FILE"
fi
