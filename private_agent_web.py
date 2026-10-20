"""
Phone web UI for private_agent_pro.py: chat box, dashboard, Approve/Decline buttons.
Runs ONLY on your phone (127.0.0.1), protected by a random one-time link token.

Put this file in the same folder as private_agent_pro.py, then in Termux:
  pip install openai cryptography
  export LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...   (see private_agent_pro.py)
  python private_agent_web.py
Open the link it prints (it tries to open your browser automatically).
"""
import atexit, re, datetime, getpass, json, os, secrets, shlex, shutil, subprocess, sys, threading, time, urllib.parse, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from openai import OpenAI
import private_agent_pro as pro
try:
    import android_tools  # noqa: F401  (Android control tools)
    print("Android tools loaded.")
except Exception as e:
    print("Android tools NOT loaded:", e)

PORT = 8765
TOKEN = secrets.token_urlsafe(16)
VOICE = secrets.token_urlsafe(16)  # lets the Friday voice script talk to the app without the page PIN
PIN = os.getenv("APP_PIN", "")  # screen-lock PIN, set with: export APP_PIN=...
S = {"chat": [], "busy": False, "pending": None, "history": [],
     "unlocked": not PIN, "fails": 0, "until": 0, "last": 0, "need_key": None, "everywhere": False}


def is_locked():
    if not PIN:
        return False
    if S["unlocked"] and time.time() - S["last"] > 300:  # auto-lock after 5 min idle
        S["unlocked"] = False
    if S["unlocked"]:
        S["last"] = time.time()
    return not S["unlocked"]
LOCK = threading.Lock()
PRINT_BUF = []
VAULT = CLIENT = None


# ---- bridge: turn the terminal prompts of private_agent_pro into web approvals ----
def web_print(*a, **k):
    PRINT_BUF.append(" ".join(str(x) for x in a))


def ask(kind, text):
    p = {"id": uuid.uuid4().hex, "kind": kind, "text": text, "ev": threading.Event(), "answer": None}
    S["pending"] = p
    p["ev"].wait(300)  # no answer in 5 minutes = declined
    S["pending"] = None
    return p["answer"]


AUTO = os.getenv("APPROVE", "none") == "none"  # default: never ask. Set APPROVE=all (or fast/risky) to bring approvals back.


def web_confirm(prompt):
    if AUTO:
        return True
    return ask("yn", prompt) is True


def web_input(prompt=""):
    text = "\n".join(PRINT_BUF)
    PRINT_BUF.clear()
    if AUTO:  # large-purchase check: answer it automatically with the shown amount
        m = re.search(r"\(([\d.]+)\)", prompt)
        return m.group(1) if m else ""
    ans = ask("amount", (text + "\n" + prompt).strip())
    return ans if isinstance(ans, str) else ""


def work(msg):
    h0 = len(S["history"])
    try:
        PRINT_BUF.clear()
        reply = pro.run_agent(CLIENT, VAULT, msg, S["history"])
    except Exception as e:
        code = getattr(e, "status_code", None)
        if code in (401, 402, 403, 429):  # key rejected or usage limit reached: ask for a new key
            del S["history"][h0:]
            S["need_key"] = {"code": code, "msg": msg}
            S["busy"] = False
            return
        reply = f"Error ({type(e).__name__}). Check your LLM settings and try again."
    S["chat"].append({"role": "assistant", "text": reply or "(no reply)"})
    S["busy"] = False


def set_everywhere(on):
    """Show/hide a persistent notification with a Talk button, reachable from every app."""
    try:
        if on:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "friday_voice.py")
            cmd = f"{shlex.quote(sys.executable)} {shlex.quote(script)}"
            subprocess.run(["termux-notification", "--id", "friday", "--ongoing", "--priority", "max",
                            "--title", "Friday", "--content", "Tap Talk to speak to me from any app",
                            "--button1", "Talk", "--button1-action", cmd], capture_output=True, text=True, timeout=15)
        else:
            subprocess.run(["termux-notification-remove", "friday"], capture_output=True, timeout=15)
        S["everywhere"] = bool(on)
    except Exception:
        S["everywhere"] = False  # Termux:API missing or not allowed


def snapshot():
    v = VAULT.data
    now = datetime.datetime.now()
    month, today = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    spent = sum(p["amount"] for p in v["purchases"] if p["t"].startswith(month))
    acts = [{"t": a["t"], "text": f'{a["action"].replace("_", " ")}: {a["summary"]}'} for a in v["audit"]]
    acts += [{"t": p["t"], "text": f'purchase checkout: {p["item"]} (${p["amount"]:.2f})'} for p in v["purchases"]]
    acts.sort(key=lambda x: x["t"], reverse=True)
    week = []
    for i in range(6, -1, -1):
        d = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        week.append({"d": d[5:], "v": round(sum(x["amount"] for x in v["purchases"] if x["t"].startswith(d)), 2)})
    counts = {}
    for a in v["audit"]:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    p = S["pending"]
    return {
        "week": week, "counts": counts, "purchases": v["purchases"][-5:][::-1],
        "spent": round(spent, 2), "limit": pro.MONTHLY_LIMIT, "cap": pro.AUTO_CAP,
        "today": sum(1 for a in acts if a["t"].startswith(today)),
        "activity": acts[:20], "chat": S["chat"], "busy": S["busy"], "model": pro.MODEL,
        "need_key": {"code": S["need_key"]["code"]} if S["need_key"] else None,
        "everywhere": S["everywhere"],
        "pending": {"id": p["id"], "kind": p["kind"], "text": p["text"]} if p else None,
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def is_voice(self):
        return secrets.compare_digest(self.headers.get("X-Voice", "").encode(), VOICE.encode())

    def authed(self):
        return self.host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/" and self.host_ok():
            k = urllib.parse.parse_qs(u.query).get("k", [""])[0]
            if secrets.compare_digest(k, TOKEN):
                return self.send(200, PAGE, "text/html")
            return self.send(403, "Forbidden", "text/plain")
        if u.path == "/api/state" and self.authed():
            return self.send(200, json.dumps({"locked": True} if (is_locked() and not self.is_voice()) else snapshot()))
        self.send(404, "{}")

    def do_POST(self):
        global CLIENT
        if not self.authed():
            return self.send(403, "{}")
        n = int(self.headers.get("Content-Length", 0))
        if n > 20000:
            return self.send(413, "{}")
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/unlock":
            if time.time() < S["until"]:
                return self.send(429, json.dumps({"ok": False, "wait": int(S["until"] - time.time())}))
            if PIN and secrets.compare_digest(str(data.get("pin", "")).encode(), PIN.encode()):
                S.update(unlocked=True, fails=0, last=time.time())
                return self.send(200, '{"ok": true}')
            S["fails"] += 1
            if S["fails"] >= 5:
                S["until"] = time.time() + 60 * (S["fails"] - 4)
            return self.send(401, '{"ok": false}')
        if self.path == "/api/lock":
            S["unlocked"] = False
            return self.send(200, "{}")
        if is_locked() and not self.is_voice():
            return self.send(401, "{}")
        if self.path == "/api/chat":
            msg = str(data.get("message", "")).strip()[:4000]
            with LOCK:
                if not msg or S["busy"] or S["need_key"]:
                    return self.send(409, "{}")
                S["busy"] = True
            S["chat"].append({"role": "user", "text": msg})
            threading.Thread(target=work, args=(msg,), daemon=True).start()
        elif self.path == "/api/key":
            nk = S["need_key"]
            key = str(data.get("key", "")).strip()
            if nk and not S["busy"]:
                if key:
                    if len(key) < 10 or " " in key:
                        return self.send(400, "{}")
                    CLIENT = OpenAI(base_url=pro.BASE_URL, api_key=key)  # kept in memory only
                S["need_key"] = None
                S["busy"] = True
                threading.Thread(target=work, args=(nk["msg"],), daemon=True).start()
        elif self.path == "/api/everywhere":
            set_everywhere(bool(data.get("on")))
        elif self.path == "/api/approve":
            p = S["pending"]
            if p and p["id"] == data.get("id"):
                ok = bool(data.get("ok"))
                p["answer"] = (str(data.get("typed", "")) if ok else "") if p["kind"] == "amount" else ok
                p["ev"].set()
        self.send(200, "{}")


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "private_agent_ui.html"), encoding="utf-8").read()


if __name__ == "__main__":
    VAULT = pro.Vault(pro.VAULT_PATH, getpass.getpass("Vault passphrase: "))
    CLIENT = OpenAI(base_url=pro.BASE_URL, api_key=pro.API_KEY)
    pro.confirm, pro.input, pro.print = web_confirm, web_input, web_print
    url = f"http://127.0.0.1:{PORT}/?k={TOKEN}"
    cfg = os.path.expanduser("~/.friday.json")  # read by friday_voice.py (private to Termux)
    with open(cfg, "w") as f:
        json.dump({"port": PORT, "token": TOKEN, "voice": VOICE}, f)
    os.chmod(cfg, 0o600)
    set_everywhere(False)  # start clean; turn it on from the Dashboard
    atexit.register(lambda: set_everywhere(False))
    print("Open this link in your phone browser (valid until you stop the app):\n" + url)
    if shutil.which("termux-open-url"):
        subprocess.Popen(["termux-open-url", url])
    else:
        webbrowser.open(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
