"""Download a stretch of video from a Tapo camera's memory card.

Usage (run with Sit Pretty's .venv python):
  sdclip.py list <ip> <YYYYMMDD>
  sdclip.py get  <ip> <start epoch> <end epoch> <output .mp4>
The Tapo app password is read from sitpretty-secrets.txt (tapo_app_password=).
Prints one JSON line with the result; errors never include the password.
"""
import asyncio, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def secret(key):
    for ln in open(os.path.join(HERE, "sitpretty-secrets.txt"), encoding="utf8"):
        ln = ln.strip()
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].strip()
    return ""


def connect(ip):
    from pytapo import Tapo
    pw = secret("tapo_app_password")
    if not pw:
        raise RuntimeError("Tapo app password missing: fill in tapo_app_password= in sitpretty-secrets.txt")
    # memory-card playback signs in as the camera's built-in "admin" with the Tapo app password
    return Tapo(ip, "admin", pw, pw, printWarnInformation=False)


def main():
    cmd, ip = sys.argv[1], sys.argv[2]
    tapo = connect(ip)
    if cmd == "list":
        recs = tapo.getRecordings(sys.argv[3])
        out = []
        for r in recs:
            for v in r.values():
                out.append([int(v["startTime"]), int(v["endTime"])])
        print(json.dumps({"ok": True, "recordings": sorted(out)}))
        return
    if cmd == "get":
        from pytapo.media_stream.downloader import Downloader
        start, end, out = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
        d, name = os.path.split(out)
        os.makedirs(d, exist_ok=True)
        dl = Downloader(tapo, start, end, tapo.getTimeCorrection(), outputDirectory=d + os.sep,
                        fileName=name, padding=0, overwriteFiles=True)
        status = asyncio.run(dl.downloadFile())
        ok = os.path.isfile(out) and os.path.getsize(out) > 10000
        print(json.dumps({"ok": ok, "file": out, "status": status.get("currentAction")}))
        return
    raise SystemExit("unknown command")


if __name__ == "__main__":
    ffbin = os.path.dirname(json.load(open(os.path.join(HERE, "config.json"), encoding="utf8"))["ffmpeg"])
    os.environ["PATH"] = ffbin + os.pathsep + os.environ.get("PATH", "")
    try:
        main()
    except Exception as e:
        msg = str(e)
        pw = ""
        try:
            pw = secret("tapo_app_password")
        except Exception:
            pass
        if pw:
            msg = msg.replace(pw, "***")
        print(json.dumps({"ok": False, "error": msg[:300]}))
