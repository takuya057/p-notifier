#!/bin/bash
# Cookieを更新する半自動スクリプト。
#
# 使い方:
#   1. ブラウザで管理画面にログイン or ページリロード
#   2. F12 → Network → get?page=... をクリック
#   3. "Request Headers" の Cookie 行 全体、または
#      "Response Headers" の Set-Cookie 3つの全体 をコピー
#      （pbpaste すれば何でもOK、Cookie名=値 のペアを自動抽出）
#   4. ./refresh-cookie.sh を実行 → Enter
#   5. 完了
set -e
cd "$(dirname "$0")"

PLIST="$HOME/Library/LaunchAgents/com.gachastation.payment-notifier.plist"

echo "📋 ブラウザの DevTools から Cookie or Set-Cookie をコピーしておいてください。"
read -p "    クリップボードに入れたら Enter ▶ "

INPUT=$(pbpaste)
if [[ -z "$INPUT" ]]; then
  echo "❌ クリップボードが空です。コピーしてから再実行してください。"
  exit 1
fi

# Python でパース（Set-Cookie ヘッダーでも Cookie ヘッダーでも対応）
COOKIE=$(echo "$INPUT" | /opt/homebrew/bin/python3 - <<'PYEOF'
import re
import sys

text = sys.stdin.read()
# "set-cookie" や "cookie:" のヘッダー名を除去
text = re.sub(r'(?im)^\s*(set-cookie|cookie)\s*:?\s*', '', text)

attrs = {'expires', 'max-age', 'path', 'domain', 'secure', 'httponly', 'samesite'}
seen = {}
# 改行とカンマでcookie間を区切り、セミコロンで属性を区切る
for line in re.split(r'[\n,]', text):
    for bit in line.split(';'):
        bit = bit.strip()
        if '=' not in bit:
            continue
        name, value = bit.split('=', 1)
        name = name.strip()
        value = value.strip()
        if name.lower() in attrs or not name or not value:
            continue
        seen[name] = value

# 必須: XSRF-TOKEN と laravel_session があるかチェック
required = {'XSRF-TOKEN', 'laravel_session'}
missing = required - set(seen)
if missing:
    sys.stderr.write(f"❌ 必須Cookieが見つかりません: {', '.join(missing)}\n")
    sys.stderr.write(f"   抽出されたCookie名: {', '.join(seen.keys()) or '(なし)'}\n")
    sys.exit(2)

print('; '.join(f'{n}={v}' for n, v in seen.items()))
PYEOF
)

if [[ -z "$COOKIE" ]]; then
  echo "❌ Cookie抽出失敗"
  exit 1
fi

# WebhookURL を保持して .env を再生成
WEBHOOK_LINE=$(grep '^DISCORD_WEBHOOK_URL=' .env || echo "")
if [[ -z "$WEBHOOK_LINE" ]]; then
  echo "❌ .env に DISCORD_WEBHOOK_URL がありません"
  exit 1
fi

cat > .env <<EOF
GACHA_COOKIE='$COOKIE'
$WEBHOOK_LINE
EOF
chmod 600 .env
echo "✅ .env 更新完了"

# 抽出した Cookie 名を表示（値は伏せる）
echo "   抽出: $(echo "$COOKIE" | tr ';' '\n' | awk -F'=' '{gsub(/^ /, "", $1); printf "%s ", $1}')"

# cookies.txt キャッシュをクリア
rm -f cookies.txt
echo "✅ cookies.txt 削除"

# launchd 再起動
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✅ launchd 再起動"

# 動作確認（12秒待って最新ログを確認）
echo ""
echo "⏳ 12秒後に動作確認..."
sleep 12

echo ""
echo "=== notify.log (最新3行) ==="
tail -3 notify.log 2>/dev/null || echo "(まだログなし)"

if [[ -s notify.error.log ]]; then
  echo ""
  echo "⚠️  notify.error.log にエラーがあります:"
  tail -5 notify.error.log
else
  echo ""
  echo "✅ エラーなし、正常稼働中。Cookieは10秒おきに自動更新されます。"
fi
