"""
Friday voice mode for Termux: talk hands-free while using ANY app (YouTube, Claude, ...).
Needs: Termux:API installed, the Friday app running (python private_agent_web.py), and `pkg install termux-api`.
Run it from a Termux:Float window or a Termux:Widget shortcut. Say "stop" (or stay silent) to end.
"""
import fcntl, json, os, re, subprocess, sys, time, urllib.error, urllib.request

try:
    cfg = json.load(open(os.path.expanduser("~/.friday.json")))
except Exception:
    subprocess.run(["termux-tts-speak", "The Friday app is not running. Start it in Termux first."])
    sys.exit(0)
BASE = f"http://127.0.0.1:{cfg['port']}"
HDR = {"Content-Type": "application/json", "X-Token": cfg["token"], "X-Voice": cfg["voice"]}


def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=HDR), timeout=60).read() or b"{}")


def speak(text):
    subprocess.run(["termux-tts-speak", re.sub(r"[*_`#]", "", text)[:1500]])


def listen():
    try:
        r = subprocess.run(["termux-speech-to-text"], capture_output=True, text=True, timeout=45)
        return r.stdout.strip()
    except Exception:
        return ""


def wait_reply(n):
    warned = False
    for _ in range(240):  # up to ~96 seconds
        time.sleep(0.4)
        s = api("/api/state")
        if s.get("need_key"):
            return "My AI key hit its limit. Open the app and paste a new key."
        if s.get("pending") and not warned:
            warned = True
            speak("Please approve that on your screen.")
        if not s["busy"] and len(s["chat"]) >= n + 2 and s["chat"][-1]["role"] == "assistant":
            return s["chat"][-1]["text"]
    return "That took too long."


def main():
    lock = open(os.path.expanduser("~/.friday.lock"), "w")  # only one voice session at a time
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    try:
        api("/api/state")
    except Exception:
        subprocess.run(["termux-tts-speak", "The Friday app is not running. Start it in Termux first."])
        return
    speak("Friday here.")
    quiet = 0
    while True:
        text = listen()
        if not text:
            quiet += 1
            if quiet >= 2:
                speak("I'll go quiet. Start me again when you need me.")
                return
            continue
        quiet = 0
        if text.lower().strip(" .!?") in ("stop", "bye", "goodbye", "exit", "quit", "that's all", "thats all"):
            speak("Okay, bye.")
            return
        try:
            n = len(api("/api/state")["chat"])
            api("/api/chat", {"message": text})
            speak(wait_reply(n))
        except urllib.error.HTTPError as e:
            speak("I'm still busy, one moment." if e.code == 409 else "Something went wrong.")
        except Exception:
            speak("I lost the connection to the app.")
            return


if __name__ == "__main__":
    main()
