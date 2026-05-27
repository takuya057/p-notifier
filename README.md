# ガチャステーション 課金通知ボット

`tool.gacha-station.com/admin/payment-history` の新規課金を Discord に通知する自動監視ボット。

## 通知の種類

| 種類 | 条件 | 見た目 | スマホPush |
|---|---|---|---|
| 💰 **通常通知** | 新規課金あり | 緑枠 | 通常通知 |
| 🚨 **高額課金アラート** | `price >= ¥30,000` | **赤枠 + @everyone** | **強制Push** |
| ⚡ **連続課金アラート** | 1時間以内に同メールが10件以上 | **赤枠 + @everyone** | **強制Push** |

連続課金アラートのクールダウン: 同ユーザーは1時間に1回まで。

## アーキテクチャ（ハイブリッド構成）

```
┌─ Mac launchd (10秒おき・メイン稼働)
│   └─ monitor.py
│       ├─ /admin/payment-history/get を叩く
│       ├─ 419/401時に /user/login/post で自動再ログイン
│       └─ state.json に updated_at と last_id を記録
│
├─ git push (5分に1回)
│   └─ state.json のみリポジトリに反映
│
└─ GitHub Actions (5分おき・バックアップ)
    └─ state.json.updated_at をチェック
        ├─ 10分以内に更新あり → skip (重複防止)
        └─ それ以外（Mac停止中）→ 自動引き継ぎ → state.json を push
```

**結果:** Mac起動中は10秒間隔・持ち歩き時は5分間隔・Cookieは自動再ログインで永久有効。

## ファイル

| パス | 役割 | git管理 |
|---|---|---|
| `monitor.py` | メインスクリプト | ✅ |
| `run.sh` | launchd用ラッパー | ✅ |
| `refresh-cookie.sh` | 手動Cookieリフレッシュ（緊急時用） | ✅ |
| `.github/workflows/notify.yml` | GitHub Actions cron | ✅ |
| `state.json` | last_id, updated_at, エラー状態 | ✅ |
| `.env` | EMAIL, PASSWORD, WEBHOOK URL | ❌ ローカルのみ |
| `cookies.txt` | セッションCookie自動保存 | ❌ ローカルのみ |
| `notify.log` / `notify.error.log` | ログ | ❌ |

## 必要な環境変数 / Secrets

| 名前 | 内容 |
|---|---|
| `GACHA_EMAIL` | 管理画面ログインメールアドレス |
| `GACHA_PASSWORD` | 管理画面ログインパスワード |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL |
| `GACHA_COOKIE`（オプション） | 初期Cookie。EMAIL/PASSWORD があれば自動ログインで上書きされる |
| `FAILOVER_GRACE_SECONDS`（オプション） | この秒数以内に他インスタンスが state.json を更新していれば skip。GitHub Actions側で `600` 設定 |

## 運用コマンド

```bash
# 状態確認
launchctl print gui/$(id -u)/com.gachastation.payment-notifier | grep -E "(state =|run interval)"

# ログ
tail -f ~/gacha-payment-notifier/notify.log

# 停止
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.gachastation.payment-notifier.plist

# 再開
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gachastation.payment-notifier.plist

# 手動実行
~/gacha-payment-notifier/run.sh

# 緊急時のCookie手動更新（自動ログインが何度も失敗する場合）
~/gacha-payment-notifier/refresh-cookie.sh
```

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| Discord `⚠️ 監視エラー (login_failed)` | パスワード変更されたかも → `.env` の `GACHA_PASSWORD` を更新 |
| Discord `⚠️ 監視エラー (csrf_or_session_expired)` | 自動ログインも失敗。`.env` の認証情報を確認 |
| Discord `⚠️ 監視エラー (network)` | 一過性ネットワーク問題。続くようなら接続環境を確認 |
| 何も通知されない | `notify.error.log` を確認 |
