#!/usr/bin/env python3
"""ガチャステーション 課金通知ボット.

定期的に /admin/payment-history/get を叩き、新規課金があれば Discord Webhook に通知する。
Cookie jar でレスポンスの Set-Cookie を自動更新するため、初回 .env で渡すだけで
セッションは定期実行が続く限り永続化される。
"""
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
API_URL = f"{BASE_URL}/admin/payment-history/get?page=1&length=20"
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
        }
    data = json.loads(STATE_FILE.read_text())
    data.setdefault("last_error_at", 0)
    data.setdefault("last_error_kind", None)
    data.setdefault("updated_at", 0)
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
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PaymentNotifierBot/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT):
        pass


def notify_payment(webhook_url: str, payment: dict) -> None:
    method = PAYMENT_METHOD_LABELS.get(
        payment.get("payment_method", ""), payment.get("payment_method", "不明")
    )
    status = PAYMENT_STATUS_LABELS.get(
        payment.get("payment_status"), str(payment.get("payment_status"))
    )
    embed = {
        "title": f"💰 新規課金 ¥{payment.get('price', 0):,}",
        "color": 0x2ECC71,
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
    post_discord(webhook_url, {"username": "課金通知Bot", "embeds": [embed]})


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

    new_rows = [r for r in rows if r["id"] > last_id]
    print(f"last_id={last_id}, max_id={max_id}, new={len(new_rows)}")

    for row in new_rows:
        notify_payment(webhook, row)
        print(f"  notified payment_id={row['id']}")

    if new_rows:
        state["last_id"] = max_id
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
