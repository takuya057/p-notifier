#!/usr/bin/env python3
"""ガチャステーション 課金通知ボット.

定期的に /admin/payment-history/get を叩き、新規課金があれば Discord Webhook に通知する。
Cookie jar でレスポンスの Set-Cookie を自動更新するため、初回 .env で渡すだけで
セッションは定期実行が続く限り永続化される。
"""
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://tool.gacha-station.com"
API_URL = f"{BASE_URL}/admin/payment-history/get?page=1&length=100"
LOGIN_PAGE_URL = f"{BASE_URL}/user/login"
LOGIN_POST_URL = f"{BASE_URL}/user/login/post"
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
COOKIE_JAR_FILE = BASE_DIR / "cookies.txt"
TIMEOUT = 30
RETRY_COUNT = 3
RETRY_WAIT = 3
ERROR_NOTIFY_INTERVAL = 3600  # 同種エラーの通知は最低1時間あける
RECOVERY_MIN_DOWNTIME = 60  # この時間以上ダウンしていた場合のみ復旧通知

# 重要通知の閾値
HIGH_AMOUNT_THRESHOLD = 30000  # この金額以上で高額アラート
RAPID_WINDOW_SECONDS = 3600  # 連続課金の集計時間枠 (1時間)
RAPID_COUNT_THRESHOLD = 10  # 連続課金とみなす件数
RAPID_ALERT_COOLDOWN = 3600  # 同ユーザーの連続課金アラートのクールダウン (1時間)
JST = datetime.timezone(datetime.timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HINT_BY_KIND = {
    "csrf_or_session_expired": (
        "→ 自動ログインも失敗しました。`.env` の `GACHA_EMAIL` / `GACHA_PASSWORD` を確認、"
        "ターミナルで `~/gacha-payment-notifier/refresh-cookie.sh` を実行してください。"
    ),
    "unauthorized": (
        "→ 自動ログインも失敗しました。`.env` の認証情報を確認してください。"
    ),
    "login_failed": (
        "→ 自動ログインに失敗しました。`.env` の `GACHA_EMAIL` / `GACHA_PASSWORD` "
        "が正しいか確認してください。"
    ),
    "server_error": "→ サイト側の一時的な問題の可能性があります。続くようなら委託先に確認を。",
    "network": "→ ネットワークの一時的な問題の可能性があります。3回リトライ後も失敗しました。",
    "other": "→ `notify.error.log` を確認してください。",
}

PAYMENT_METHOD_LABELS = {
    "user_register_card_and_payment": "登録カード",
    "credit_card": "クレジットカード",
    "convenience_store": "コンビニ",
    "bank_transfer": "銀行振込",
    "paypay": "PayPay",
}

PAYMENT_STATUS_LABELS = {
    1: "決済中",
    5: "完了",
}


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"環境変数 {name} が設定されていません")
    return v


def env_opt(name: str) -> str:
    return os.environ.get(name, "")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "last_id": None,
            "last_error_at": 0,
            "last_error_kind": None,
            "updated_at": 0,
            "alert_history": {},
        }
    data = json.loads(STATE_FILE.read_text())
    data.setdefault("last_error_at", 0)
    data.setdefault("last_error_kind", None)
    data.setdefault("updated_at", 0)
    data.setdefault("alert_history", {})
    return data


def save_state(state: dict) -> None:
    state["updated_at"] = time.time()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def build_jar(initial_cookie_header: str) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(COOKIE_JAR_FILE))
    if COOKIE_JAR_FILE.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    if not list(jar) and initial_cookie_header:
        for pair in initial_cookie_header.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=name.strip(),
                    value=value.strip(),
                    port=None,
                    port_specified=False,
                    domain="tool.gacha-station.com",
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )
    return jar


def xsrf_header(jar: http.cookiejar.CookieJar) -> str:
    for c in jar:
        if c.name == "XSRF-TOKEN":
            return urllib.parse.unquote(c.value)
    raise RuntimeError("Cookie jar に XSRF-TOKEN がありません")


def login(jar: http.cookiejar.CookieJar, email: str, password: str) -> None:
    """ /user/login をGETしてCSRFトークン取得 → /user/login/post でログイン.
    成功時 jar が認証済みCookieに更新される。失敗時は例外。
    """
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # 1. ログインページGET（CSRFトークン入りのHTMLと初期セッションCookie取得）
    req = urllib.request.Request(LOGIN_PAGE_URL, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ログインページ取得失敗 HTTP {e.code}")

    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    if not m:
        raise RuntimeError("ログインページから CSRF token を取得できません")
    csrf_token = m.group(1)

    # 2. ログイン送信 (application/x-www-form-urlencoded)
    body = urllib.parse.urlencode(
        {"username": email, "password": password}
    ).encode("utf-8")
    req = urllib.request.Request(
        LOGIN_POST_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-TOKEN": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": LOGIN_PAGE_URL,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        snippet = e.read()[:200].decode("utf-8", errors="replace")
        raise RuntimeError(f"ログイン失敗 HTTP {e.code}: {snippet}")

    role = data.get("role")
    if role not in ("admin", "staff"):
        raise RuntimeError(
            f"ログインしたが管理者権限がありません role={role!r}, response={data}"
        )
    # jar には認証済みCookieが入っている


def fetch_payments(
    jar: http.cookiejar.CookieJar,
    email: str = "",
    password: str = "",
    _already_relogged_in: bool = False,
) -> dict:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    last_net_exc = None
    for attempt in range(RETRY_COUNT):
        try:
            headers = {
                "X-XSRF-TOKEN": xsrf_header(jar),
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": f"{BASE_URL}/admin/payment-history",
                "User-Agent": USER_AGENT,
            }
        except RuntimeError:
            # jar が空 (cookies.txt なし or 認証情報なし) → ログイン試行
            if email and password and not _already_relogged_in:
                login(jar, email, password)
                return fetch_payments(jar, email, password, _already_relogged_in=True)
            raise

        req = urllib.request.Request(API_URL, data=b"{}", headers=headers, method="POST")
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 419/401/403 → 自動ログインしてリトライ
            if (
                e.code in (419, 401, 403)
                and email
                and password
                and not _already_relogged_in
            ):
                e.read()  # consume
                # 古いCookie捨てて、新規ログイン
                jar.clear()
                try:
                    login(jar, email, password)
                except Exception as login_exc:
                    raise RuntimeError(f"自動ログイン失敗: {login_exc}")
                return fetch_payments(jar, email, password, _already_relogged_in=True)
            snippet = e.read()[:200].decode("utf-8", errors="replace")
            raise RuntimeError(f"API HTTP {e.code}: {snippet}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_net_exc = e
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_WAIT)
                continue
    raise RuntimeError(f"ネットワークエラー(リトライ{RETRY_COUNT}回失敗): {last_net_exc}")


def post_discord(webhook_url: str, payload: dict) -> None:
    """Discord webhook に POST. 429 (rate limit) は Retry-After で再試行."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "PaymentNotifierBot/1.0",
    }
    for attempt in range(3):
        req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = float(e.headers.get("Retry-After", "5"))
                # Discord は通常 0.5秒〜数秒、念のため最大10秒
                time.sleep(min(retry_after + 0.5, 10))
                continue
            raise
    # 全て失敗してもエラーを上に投げる
    raise RuntimeError("Discord webhook 3回試行失敗 (429リトライ後も)")


def notify_payment(webhook_url: str, payment: dict, high_amount: bool = False) -> None:
    method = PAYMENT_METHOD_LABELS.get(
        payment.get("payment_method", ""), payment.get("payment_method", "不明")
    )
    status = PAYMENT_STATUS_LABELS.get(
        payment.get("payment_status"), str(payment.get("payment_status"))
    )
    price = payment.get("price", 0)
    if high_amount:
        title = f"🚨 高額課金 ¥{price:,} 🚨"
        color = 0xE74C3C  # 赤
    else:
        title = f"💰 新規課金 ¥{price:,}"
        color = 0x2ECC71  # 緑
    embed = {
        "title": title,
        "color": color,
        "fields": [
            {
                "name": "ユーザー",
                "value": str(payment.get("email") or payment.get("username") or "不明"),
                "inline": True,
            },
            {"name": "獲得Pt", "value": f"{payment.get('point', 0):,} pt", "inline": True},
            {"name": "決済方法", "value": method, "inline": True},
            {"name": "ステータス", "value": status, "inline": True},
            {
                "name": "残ポイント",
                "value": f"{payment.get('remain_point', 0):,} pt",
                "inline": True,
            },
            {"name": "日時", "value": str(payment.get("created_at", "")), "inline": False},
        ],
        "footer": {"text": f"payment_id: {payment.get('id')}"},
    }
    payload = {"username": "課金通知Bot", "embeds": [embed]}
    if high_amount:
        payload["content"] = "@everyone 🚨 **高額課金検知**"
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    post_discord(webhook_url, payload)


def notify_rapid_charge(webhook_url: str, email: str, payments: list) -> None:
    """過去1時間に同ユーザーが10件以上課金 → 連続課金アラート."""
    total = sum(p.get("price", 0) for p in payments)
    total_pt = sum(p.get("point", 0) for p in payments)
    times = [p.get("created_at", "") for p in payments]
    embed = {
        "title": f"⚡ 連続課金検知 ({len(payments)}件 / 1時間以内)",
        "color": 0xE74C3C,  # 赤
        "description": f"同一ユーザーが過去1時間に **{len(payments)}件 ¥{total:,}** 課金しています。",
        "fields": [
            {"name": "ユーザー", "value": str(email), "inline": False},
            {"name": "件数", "value": f"{len(payments)} 件", "inline": True},
            {"name": "合計金額", "value": f"¥{total:,}", "inline": True},
            {"name": "合計Pt", "value": f"{total_pt:,} pt", "inline": True},
            {"name": "最初", "value": str(times[-1]) if times else "?", "inline": True},
            {"name": "最後", "value": str(times[0]) if times else "?", "inline": True},
        ],
        "footer": {"text": "同ユーザーへの連続課金アラートは1時間に1回まで"},
    }
    post_discord(
        webhook_url,
        {
            "username": "課金通知Bot",
            "content": "@everyone ⚡ **連続課金検知**",
            "embeds": [embed],
            "allowed_mentions": {"parse": ["everyone"]},
        },
    )


def parse_jst(s: str) -> "datetime.datetime | None":
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    except Exception:
        return None


def detect_rapid_charges(
    rows: list, alert_history: dict, now_ts: float
) -> list:
    """rows 全体（直近100件）を見て、過去1時間に同メールが10件以上のユーザーを抽出.
    クールダウン中は除外する。
    Returns: [(email, [payments...]), ...]
    """
    cutoff = datetime.datetime.now(JST) - datetime.timedelta(seconds=RAPID_WINDOW_SECONDS)
    by_email: dict[str, list] = {}
    for r in rows:
        created = parse_jst(r.get("created_at", ""))
        if created is None or created < cutoff:
            continue
        email = r.get("email") or r.get("username") or ""
        if not email:
            continue
        by_email.setdefault(email, []).append(r)

    alerts = []
    for email, ps in by_email.items():
        if len(ps) < RAPID_COUNT_THRESHOLD:
            continue
        last_alert = alert_history.get(email, 0)
        if now_ts - last_alert < RAPID_ALERT_COOLDOWN:
            continue
        # 新しい順にソート（最新が先頭）
        ps.sort(key=lambda r: r.get("id", 0), reverse=True)
        alerts.append((email, ps))
    return alerts


def notify_error_throttled(webhook_url: str, state: dict, kind: str, message: str) -> None:
    now = time.time()
    last_at = state.get("last_error_at", 0)
    last_kind = state.get("last_error_kind")
    state.setdefault("error_first_at", 0)
    if last_kind != kind:
        state["error_first_at"] = now
    if last_kind == kind and (now - last_at) < ERROR_NOTIFY_INTERVAL:
        # throttle 中は last_error_at を更新しない（バグ修正：
        # 更新すると "最後の通知から1時間" ではなく "最後の検知から1時間" になり永久にthrottle継続）
        return
    hint = HINT_BY_KIND.get(kind, HINT_BY_KIND["other"])
    try:
        post_discord(
            webhook_url,
            {
                "username": "課金通知Bot",
                "content": (
                    f"⚠️ **監視エラー** ({kind})\n"
                    f"```\n{message[:1500]}\n```\n"
                    f"{hint}\n"
                    "（同種エラーは1時間に1回まで通知）"
                ),
            },
        )
    except Exception:
        pass
    state["last_error_at"] = now
    state["last_error_kind"] = kind


def kind_of(exc_message: str) -> str:
    lower = exc_message.lower()
    if "自動ログイン失敗" in exc_message or "ログイン失敗" in exc_message:
        return "login_failed"
    if "419" in exc_message or "csrf" in lower:
        return "csrf_or_session_expired"
    if "401" in exc_message or "403" in exc_message:
        return "unauthorized"
    if "timed out" in lower or "timeout" in lower or "ネットワークエラー" in exc_message:
        return "network"
    if any(c in exc_message for c in ("500", "502", "503", "504")):
        return "server_error"
    return "other"


def main() -> int:
    cookie_initial = env_opt("GACHA_COOKIE")
    email = env_opt("GACHA_EMAIL")
    password = env_opt("GACHA_PASSWORD")
    webhook = env("DISCORD_WEBHOOK_URL")

    if not cookie_initial and not (email and password):
        sys.exit("GACHA_COOKIE か GACHA_EMAIL/GACHA_PASSWORD のいずれかが必要です")

    state = load_state()

    # フェイルオーバー機能: FAILOVER_GRACE_SECONDS が設定されており、
    # 他のインスタンスが直近にstate.jsonを更新していれば、何もせず終了。
    # GitHub Actions 側に env で設定する想定（Mac側は未設定）。
    grace_str = os.environ.get("FAILOVER_GRACE_SECONDS", "").strip()
    if grace_str:
        try:
            grace = int(grace_str)
        except ValueError:
            grace = 0
        if grace > 0:
            since = time.time() - state.get("updated_at", 0)
            if since < grace:
                print(
                    f"Skip: 他のインスタンスが{int(since)}秒前に更新済み"
                    f"（FAILOVER_GRACE_SECONDS={grace}）"
                )
                return 0

    last_id = state.get("last_id")
    jar = build_jar(cookie_initial)

    try:
        result = fetch_payments(jar, email, password)
    except Exception as e:
        msg = str(e)
        notify_error_throttled(webhook, state, kind_of(msg), msg)
        save_state(state)
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1
    finally:
        try:
            jar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass

    if state.get("last_error_kind"):
        downtime = time.time() - state.get("error_first_at", 0)
        if downtime >= RECOVERY_MIN_DOWNTIME:
            try:
                post_discord(
                    webhook,
                    {
                        "username": "課金通知Bot",
                        "content": f"✅ 監視復旧しました（ダウン時間 {int(downtime)}秒）。",
                    },
                )
            except Exception:
                pass
        state["last_error_kind"] = None
        state["last_error_at"] = 0
        state["error_first_at"] = 0

    rows = result.get("data", [])
    if not rows:
        print("No payments returned")
        save_state(state)
        return 0

    rows.sort(key=lambda r: r["id"])
    max_id = rows[-1]["id"]

    if last_id is None:
        print(f"Initial run. last_id={max_id}. Not sending notifications.")
        state["last_id"] = max_id
        save_state(state)
        return 0

    # 通知対象を抽出 (¥0 payment は除外、管理者登録など内部処理を除く)
    new_rows = [r for r in rows if r["id"] > last_id]
    new_rows.sort(key=lambda r: r["id"])
    notify_targets = [r for r in new_rows if (r.get("price", 0) or 0) > 0]
    skipped_zero = len(new_rows) - len(notify_targets)
    print(
        f"last_id={last_id}, max_id={max_id}, "
        f"new={len(new_rows)}, notify={len(notify_targets)}, skipped_zero={skipped_zero}"
    )

    # 1. 個別通知（高額判定込み、エラー耐性、逐次state更新）
    for row in notify_targets:
        is_high = (row.get("price", 0) or 0) >= HIGH_AMOUNT_THRESHOLD
        try:
            notify_payment(webhook, row, high_amount=is_high)
            tag = "🚨HIGH" if is_high else "💰"
            print(f"  {tag} notified payment_id={row['id']} ¥{row.get('price', 0):,}")
        except Exception as e:
            print(f"  ⚠️ notify failed payment_id={row['id']}: {e}", file=sys.stderr)
        # 失敗しても last_id を進める (無限再通知の防止)
        state["last_id"] = row["id"]
        save_state(state)

    # last_id を確実に max_id に揃える（¥0スキップ分も飛ばす）
    if new_rows:
        state["last_id"] = max_id
        save_state(state)

    # 2. 連続課金検知（直近100件全体を見て1時間以内の集計、¥0除外）
    alert_history = state.get("alert_history", {})
    now_ts = time.time()
    rapid_rows = [r for r in rows if (r.get("price", 0) or 0) > 0]
    rapid = detect_rapid_charges(rapid_rows, alert_history, now_ts)
    for email, payments in rapid:
        try:
            notify_rapid_charge(webhook, email, payments)
            alert_history[email] = now_ts
            print(f"  ⚡RAPID notified email={email} count={len(payments)}")
        except Exception as e:
            print(f"  ⚠️ rapid alert failed {email}: {e}", file=sys.stderr)

    # 3. 24時間以上前のalert_history をクリーンアップ
    alert_history = {
        k: v for k, v in alert_history.items() if now_ts - v < 86400
    }
    state["alert_history"] = alert_history
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
