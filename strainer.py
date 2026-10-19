#!/usr/bin/env python3
"""mail-strainer: gws로 Gmail을 읽고 TypeSafe Jev(OpenRouter Decisions API)로 스팸 점수를 매겨 SQLite에 저장한다.

사용법:
    mail-strainer setup                 # OpenRouter 키 저장
    mail-strainer doctor                # 환경 진단
    mail-strainer fetch [--query "newer_than:3d in:inbox"] [--max 100] [--move-spam 0.8]
    mail-strainer reclassify            # 저장된 메일 전체 다시 분류 (기준을 바꾼 뒤)
    mail-strainer move-spam [--min-confidence 0.8] [--dry-run] [--yes]
    mail-strainer serve [--port 8765] [--open]

데이터(키, DB)는 ~/.config/mail-strainer 에 저장된다. MAIL_STRAINER_HOME 으로 바꿀 수 있다.
"""
import argparse
import base64
import html as htmllib
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.2.0"

ROOT = Path(__file__).resolve().parent  # ui.html 위치
HOME = Path(os.environ.get("MAIL_STRAINER_HOME", Path.home() / ".config" / "mail-strainer"))
DB_PATH = HOME / "strainer.db"
ENV_PATH = HOME / ".env"
MODEL = "typesafe/jev-1.14"  # TypeSafe Jev: 스팸 확률(0~1)만 돌려주는 판정 모델
BATCH_SIZE = 30               # 한 Decisions 요청에 묻는 메일 수
BODY_CHARS = 8000             # 판정에 넣는 본문 길이
SPAM_THRESHOLD = 0.6          # 스팸 점수가 이 이상이면 스팸
DEFAULT_QUERY = "newer_than:3d in:inbox"

DEFAULT_CRITERIA = """## 내 소개 / 진행 중인 프로젝트
- (여기에 지금 하고 있는 프로젝트, 관심 분야, 거래처 등을 적어주세요)

## 스팸으로 볼 것
- 내가 요청하지 않은 영업/마케팅/광고 메일
- 웨비나·이벤트 홍보, 뉴스레터 중 프로젝트와 무관한 것
- 콜드메일(아웃소싱, SEO, 리드 생성 제안 등)

## 스팸이 아닌 것
- 내 프로젝트와 관련된 메일, 실제 사람이 보낸 업무 메일
- 보안 알림, 결제/영수증, 계정 관련 알림
- 내가 쓰는 서비스의 중요한 공지
"""


def load_env():
    # 우선순위: 환경변수 > ~/.config/mail-strainer/.env > 현재 폴더 .env
    for env_file in (ENV_PATH, Path.cwd() / ".env"):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key = os.environ.get("OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OpenRouter 키가 없어요. `mail-strainer setup`으로 저장하거나 OPENROUTER_KEY 환경변수를 설정하세요.")
    os.environ["OPENROUTER_KEY"] = key


def doctor():
    """환경 진단: gws 버전, Gmail 응답에 헤더가 들어오는지, 키 유무."""
    try:
        v = subprocess.run(["gws", "--version"], capture_output=True, text=True).stdout.splitlines()
        print("gws:", v[0] if v else "?")
    except FileNotFoundError:
        sys.exit("gws가 없어요: npm install -g @googleworkspace/cli")
    ids = list_message_ids("in:inbox", 1)
    if not ids:
        print("받은편지함에 메일이 없어요")
    else:
        for fmt in ("metadata", "full"):
            res = gws("gmail", "users", "messages", "get", "--params",
                      json.dumps({"userId": "me", "id": ids[0], "format": fmt}))
            h = _headers(res)
            print(f"format={fmt}: 최상위 키={sorted(res)} / 헤더 {len(h)}개 / Subject={'있음' if 'subject' in h else '없음'}")
    try:
        load_env()
        print("OpenRouter 키: 있음")
    except RuntimeError as ex:
        print(ex)
    print("데이터 폴더:", HOME)


def setup():
    import getpass
    HOME.mkdir(parents=True, exist_ok=True)
    key = getpass.getpass("OpenRouter API 키: ").strip()
    if not key:
        sys.exit("키가 비어 있어요.")
    ENV_PATH.write_text(f"OPENROUTER_KEY={key}\n")
    ENV_PATH.chmod(0o600)
    print(f"저장됨: {ENV_PATH}")


# ---------------------------------------------------------------- DB

def db():
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            subject TEXT,
            sender TEXT,
            date TEXT,
            internal_date INTEGER,
            snippet TEXT,
            body TEXT,                   -- 판정에 쓴 본문 (BODY_CHARS까지)
            is_spam INTEGER,
            confidence REAL,             -- Jev 스팸 점수 (0~1)
            reason TEXT,
            user_label INTEGER,          -- 사용자가 수동으로 정정한 값 (NULL이면 미정정)
            classified_at TEXT,
            moved_at TEXT                -- 스팸함으로 옮긴 시각 (NULL이면 안 옮김)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS prompt_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value TEXT,
            saved_at TEXT
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    if "moved_at" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN moved_at TEXT")
    if "body" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN body TEXT")
    if conn.execute("SELECT 1 FROM settings WHERE key='criteria'").fetchone() is None:
        conn.execute("INSERT INTO settings VALUES ('criteria', ?)", (DEFAULT_CRITERIA,))
        conn.commit()
    return conn


def get_criteria(conn):
    return conn.execute("SELECT value FROM settings WHERE key='criteria'").fetchone()[0]


def set_criteria(conn, value):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO prompt_history (value, saved_at) VALUES (?, ?)", (get_criteria(conn), now))
    conn.execute("UPDATE settings SET value=? WHERE key='criteria'", (value,))
    conn.commit()


# ---------------------------------------------------------------- Gmail (gws)

def gws(*args):
    out = subprocess.run(["gws", *args], capture_output=True, text=True, check=True).stdout
    # gws가 stdout 앞에 안내 문구를 찍는 경우가 있어 첫 '{'부터 파싱.
    # batchModify처럼 성공 시 응답 본문이 비어 있는 API도 있다.
    start = out.find("{")
    return json.loads(out[start:]) if start >= 0 else {}


def list_message_ids(query, max_results):
    ids, token = [], None
    while len(ids) < max_results:
        params = {"userId": "me", "q": query, "maxResults": min(100, max_results - len(ids))}
        if token:
            params["pageToken"] = token
        res = gws("gmail", "users", "messages", "list", "--params", json.dumps(params))
        ids += [m["id"] for m in res.get("messages", [])]
        token = res.get("nextPageToken")
        if not token:
            break
    return ids


NO_SUBJECT = "(제목 없음)"


def _headers(res):
    return {h["name"].lower(): h["value"] for h in (res.get("payload") or {}).get("headers", [])}


def _collect_parts(part, found):
    mime = part.get("mimeType", "")
    data = (part.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html") and mime not in found:
        found[mime] = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    for p in part.get("parts") or []:
        _collect_parts(p, found)


def body_text(payload):
    """text/plain이 있으면 그걸, 없으면 HTML에서 태그를 걷어낸 글자를 쓴다."""
    found = {}
    _collect_parts(payload or {}, found)
    text = found.get("text/plain", "")
    if not text.strip() and "text/html" in found:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", found["text/html"])
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:BODY_CHARS]


def get_message(mid):
    # metadataHeaders 같은 배열 파라미터는 gws 0.13.2 이하에서 깨지므로 쓰지 않는다.
    res = gws("gmail", "users", "messages", "get", "--params",
              json.dumps({"userId": "me", "id": mid, "format": "full"}))
    headers = _headers(res)
    return {
        "id": res["id"],
        "thread_id": res.get("threadId"),
        "subject": headers.get("subject") or NO_SUBJECT,
        "sender": headers.get("from", ""),
        "date": headers.get("date", ""),
        "internal_date": int(res.get("internalDate", 0)),
        "snippet": res.get("snippet", ""),
        "body": body_text(res.get("payload")) or res.get("snippet", ""),
    }


def modify_labels(ids, add, remove):
    for i in range(0, len(ids), 1000):  # batchModify는 한 번에 최대 1000개
        gws("gmail", "users", "messages", "batchModify", "--params", json.dumps({"userId": "me"}),
            "--json", json.dumps({"ids": ids[i:i + 1000], "addLabelIds": add, "removeLabelIds": remove}))


def spam_candidates(conn, min_confidence):
    """스팸함으로 옮길 대상: 스팸 판정 + 아직 안 옮김. 사용자가 '스팸으로' 정정한 건 점수와 무관하게 포함."""
    return conn.execute("""
        SELECT * FROM emails
        WHERE moved_at IS NULL AND COALESCE(user_label, is_spam) = 1
          AND (user_label = 1 OR confidence >= ?)
        ORDER BY internal_date DESC""", (min_confidence,)).fetchall()


def move_to_spam(conn, ids):
    if not ids:
        return 0
    modify_labels(ids, ["SPAM"], ["INBOX"])
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany("UPDATE emails SET moved_at=? WHERE id=?", [(now, i) for i in ids])
    conn.commit()
    return len(ids)


def restore_to_inbox(conn, ids):
    if not ids:
        return 0
    modify_labels(ids, ["INBOX"], ["SPAM"])
    conn.executemany("UPDATE emails SET moved_at=NULL WHERE id=?", [(i,) for i in ids])
    conn.commit()
    return len(ids)


# ---------------------------------------------------------------- Jev (OpenRouter Decisions API)

SPAM_CRITERIA = {
    "true": "Unsolicited sales, marketing, cold outreach, promotional newsletters, "
            "or anything user_rules or user_corrections treat as spam.",
    "false": "Work mail, mail from real people the user deals with, security/account alerts, receipts, "
             "important service notices, or anything user_rules or user_corrections treat as not spam.",
}
_local = threading.local()


def correction_examples(conn, limit=20):
    """사용자가 직접 정정한 사례. Jev state에 참고 자료로 넣는다."""
    rows = conn.execute("""
        SELECT subject, sender, user_label FROM emails
        WHERE user_label IS NOT NULL AND user_label != is_spam
        ORDER BY classified_at DESC LIMIT ?""", (limit,)).fetchall()
    return [{"from": r["sender"], "subject": r["subject"], "is_spam": bool(r["user_label"])} for r in rows]


def decisions(body):
    """OpenRouter Decisions API 호출. 스레드마다 연결을 재사용하고, 일시적 오류는 재시도한다."""
    for attempt in range(4):
        if not hasattr(_local, "conn"):
            _local.conn = http.client.HTTPSConnection("openrouter.ai", timeout=120)
        try:
            _local.conn.request("POST", "/api/alpha/decisions", json.dumps(body),
                                {"Authorization": f"Bearer {os.environ['OPENROUTER_KEY']}",
                                 "Content-Type": "application/json", "X-Title": "mail-strainer"})
            resp = _local.conn.getresponse()
            data = resp.read()
        except (http.client.HTTPException, OSError):
            del _local.conn  # 끊긴 연결은 버리고 다시 연결
            time.sleep(2 ** attempt)
            continue
        if resp.status == 200:
            return json.loads(data)
        if resp.status == 429 or resp.status >= 500:
            time.sleep(float(resp.getheader("Retry-After") or 2 ** attempt))
            continue
        raise RuntimeError(f"Jev {resp.status}: {data[:300].decode('utf-8', 'replace')}")
    raise RuntimeError("Jev 요청이 계속 실패했어요")


def classify_batch(emails, criteria, examples):
    """메일 여러 통을 한 요청에 넣고 메일마다 '스팸인가?'를 묻는다. {id: 스팸 점수} 반환."""
    keys = {f"m{i}": e for i, e in enumerate(emails)}
    state = {"user_rules": criteria,
             "emails": {k: {"from": e["sender"], "subject": e["subject"], "body": e["body"]} for k, e in keys.items()}}
    if examples:
        state["user_corrections"] = examples
    questions = {k: {"type": "noul", "criteria": SPAM_CRITERIA,
                     "instructions": f"Following user_rules, is email {k} (in emails.{k}) spam "
                                     f"that the user does not need to read?"}
                 for k in keys}
    res = decisions({"model": MODEL, "questions": questions, "state": state})
    return {e["id"]: res["answers"][k]["noul"] for k, e in keys.items() if k in res["answers"]}


# ---------------------------------------------------------------- fetch & classify

def run_fetch(query=DEFAULT_QUERY, max_results=100, reclassify=False, log=print, move_min_confidence=None,
              ids=None):
    load_env()
    conn = db()
    if ids is None:
        ids = list_message_ids(query, max_results)
    if not reclassify:
        # 헤더를 못 읽어 제목·보낸사람이 비어 저장된 메일은 다시 가져와 분류한다
        known = {r[0] for r in conn.execute(
            "SELECT id FROM emails WHERE is_spam IS NOT NULL AND NOT (subject = ? AND sender = '')", (NO_SUBJECT,))}
        ids = [i for i in ids if i not in known]
    log(f"새로 분류할 메일: {len(ids)}개")
    if not ids:
        return {"classified": 0, "spam": 0}

    t0 = time.time()
    def fetch_one(mid):
        try:
            return get_message(mid)
        except subprocess.CalledProcessError:  # 삭제된 메일 등
            return None

    with ThreadPoolExecutor(16) as pool:
        emails = [e for e in pool.map(fetch_one, ids) if e]
    log(f"메일 {len(emails)}개 가져옴 ({time.time() - t0:.1f}초)" +
        (f", {len(ids) - len(emails)}개 실패" if len(emails) < len(ids) else ""))

    criteria, examples = get_criteria(conn), correction_examples(conn)
    batches = [emails[i:i + BATCH_SIZE] for i in range(0, len(emails), BATCH_SIZE)]

    def run(batch):
        try:
            return batch, classify_batch(batch, criteria, examples)
        except Exception as ex:  # 배치 하나 실패해도 나머지는 진행
            log(f"분류 실패 ({len(batch)}개): {ex}")
            return batch, {}

    t0 = time.time()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(run, batches))
    now = datetime.now(timezone.utc).isoformat()
    classified = spam = 0
    for batch, scores in results:
        for e in batch:
            if e["id"] not in scores:
                continue
            score = float(scores[e["id"]])
            is_spam = 1 if score >= SPAM_THRESHOLD else 0
            conn.execute("""
                INSERT INTO emails (id, thread_id, subject, sender, date, internal_date, snippet, body,
                                    is_spam, confidence, reason, classified_at)
                VALUES (:id, :thread_id, :subject, :sender, :date, :internal_date, :snippet, :body,
                        :is_spam, :score, '', :now)
                ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, sender=excluded.sender,
                    date=excluded.date, snippet=excluded.snippet, body=excluded.body, is_spam=excluded.is_spam,
                    confidence=excluded.confidence, reason='', classified_at=excluded.classified_at""",
                {**e, "is_spam": is_spam, "score": score, "now": now})
            classified += 1
            spam += is_spam
    conn.commit()
    log(f"완료: {classified}개 분류 ({time.time() - t0:.1f}초), 스팸 {spam}개 (점수 {SPAM_THRESHOLD:.0%} 이상)")
    moved = 0
    if move_min_confidence is not None:
        moved = move_to_spam(conn, [r["id"] for r in spam_candidates(conn, move_min_confidence)])
        log(f"스팸함으로 이동: {moved}개 (스팸 점수 {move_min_confidence:.0%} 이상)")
    return {"classified": classified, "spam": spam, "moved": moved}


# ---------------------------------------------------------------- web UI

def serve(port, open_browser=False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    html = (ROOT / "ui.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, obj, status=200):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)

        def read_body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            elif url.path == "/api/emails":
                q = parse_qs(url.query)
                view = q.get("view", ["spam"])[0]
                where = {
                    "spam": "COALESCE(user_label, is_spam) = 1",
                    "ham": "COALESCE(user_label, is_spam) = 0",
                    "all": "1=1",
                }.get(view, "1=1")
                rows = db().execute(f"SELECT * FROM emails WHERE {where} ORDER BY internal_date DESC LIMIT 500")
                self.send_json([dict(r) for r in rows])
            elif url.path == "/api/criteria":
                self.send_json({"criteria": get_criteria(db())})
            elif url.path == "/api/stats":
                r = db().execute("""SELECT COUNT(*) total,
                    SUM(COALESCE(user_label, is_spam)) spam,
                    SUM(user_label IS NOT NULL AND user_label != is_spam) corrected,
                    SUM(moved_at IS NOT NULL) moved FROM emails""").fetchone()
                self.send_json(dict(r))
            elif url.path == "/api/move-preview":
                conf = float(parse_qs(url.query).get("min_confidence", ["0.8"])[0])
                self.send_json({"count": len(spam_candidates(db(), conf))})
            else:
                self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            try:
                self.handle_post()
            except Exception as ex:  # gws 실패 등은 UI에 오류로 표시
                self.send_json({"error": str(ex)}, 500)

        def handle_post(self):
            path, body = urlparse(self.path).path, self.read_body()
            conn = db()
            if path == "/api/criteria":
                set_criteria(conn, body["criteria"])
                self.send_json({"ok": True})
            elif path == "/api/label":
                conn.execute("UPDATE emails SET user_label=? WHERE id=?", (body["label"], body["id"]))
                conn.commit()
                row = conn.execute("SELECT moved_at FROM emails WHERE id=?", (body["id"],)).fetchone()
                if body["label"] == 0 and row and row["moved_at"]:
                    restore_to_inbox(conn, [body["id"]])
                self.send_json({"ok": True})
            elif path == "/api/move":
                if body.get("ids"):
                    ids = body["ids"]
                else:
                    ids = [r["id"] for r in spam_candidates(conn, float(body.get("min_confidence", 0.8)))]
                self.send_json({"moved": move_to_spam(conn, ids)})
            elif path == "/api/restore":
                self.send_json({"restored": restore_to_inbox(conn, body["ids"])})
            elif path == "/api/fetch":
                logs = []
                try:
                    result = run_fetch(body.get("query") or DEFAULT_QUERY, int(body.get("max") or 100),
                                       bool(body.get("reclassify")), log=logs.append)
                    self.send_json({**result, "logs": logs})
                except Exception as ex:
                    self.send_json({"error": str(ex), "logs": logs}, 500)
            else:
                self.send_json({"error": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    p = argparse.ArgumentParser(prog="mail-strainer", description="Gmail 스팸 분류기 (gws + OpenRouter Jev)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="OpenRouter API 키 저장")
    sub.add_parser("doctor", help="환경 진단 (gws 응답, 키)")
    f = sub.add_parser("fetch", help="메일 가져와서 분류")
    f.add_argument("--query", default=DEFAULT_QUERY)
    f.add_argument("--max", type=int, default=100)
    f.add_argument("--reclassify", action="store_true", help="이미 분류된 메일도 다시 분류")
    f.add_argument("--move-spam", type=float, metavar="MIN_CONF",
                   help="분류 후 스팸 점수 MIN_CONF(0~1) 이상인 메일을 스팸함으로 이동")
    m = sub.add_parser("move-spam", help="스팸으로 분류된 메일을 Gmail 스팸함으로 이동")
    m.add_argument("--min-confidence", type=float, default=0.8)
    m.add_argument("--yes", action="store_true", help="확인 없이 바로 이동")
    m.add_argument("--dry-run", action="store_true", help="대상만 보여주고 이동하지 않음")
    sub.add_parser("reclassify", help="저장된 메일 전체를 현재 기준으로 다시 분류")
    s = sub.add_parser("serve", help="로컬 웹 UI 실행")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="브라우저 자동으로 열기")
    a = p.parse_args()
    if a.cmd == "setup":
        setup()
    elif a.cmd == "doctor":
        doctor()
    elif a.cmd == "fetch":
        run_fetch(a.query, a.max, a.reclassify, move_min_confidence=a.move_spam)
    elif a.cmd == "reclassify":
        ids = [r[0] for r in db().execute("SELECT id FROM emails")]
        run_fetch(reclassify=True, ids=ids)
    elif a.cmd == "move-spam":
        conn = db()
        rows = spam_candidates(conn, a.min_confidence)
        for r in rows:
            print(f"  {r['confidence']:.0%}  {r['sender'][:30]:30}  {r['subject'][:60]}")
        print(f"이동 대상 {len(rows)}개 (스팸 점수 {a.min_confidence:.0%} 이상)")
        if not rows or a.dry_run:
            return
        if not a.yes and input("스팸함으로 옮길까요? [y/N] ").strip().lower() != "y":
            return
        print(f"{move_to_spam(conn, [r['id'] for r in rows])}개 이동 완료")
    else:
        serve(a.port, a.open)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as ex:
        sys.exit(str(ex))
    except subprocess.CalledProcessError as ex:
        sys.exit(f"gws 실행 실패: {ex.stderr or ex}")
