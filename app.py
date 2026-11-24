#!/usr/bin/env python3
"""CamRec : enregistrement des caméras (RTMP/RTSP) via ffmpeg -c copy, avec UI web.

Aucune dépendance Python externe ; nécessite ffmpeg.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("CAMREC_PORT", "8090"))
CONFIG_DIR = Path(os.environ.get("CAMREC_CONFIG", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.json"
ROOTS = [r.rstrip("/") or "/" for r in os.environ.get("RECORD_ROOTS", "/media,/mnt,/recordings").split(",") if r.strip()]
HERE = Path(__file__).resolve().parent
THUMB_DIR = Path(os.environ.get("CAMREC_THUMBS", "/tmp/camrec-thumbs"))
THUMB_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "cameras": [
        {"id": "cam1", "name": "Cam L (jardin)", "url": "rtmp://10.37.3.31:1935/live/av0"},
        {"id": "cam2", "name": "Cam régie", "url": "rtmp://10.37.3.32:1935/live/av0"},
        {"id": "cam3", "name": "Cam R (cour)", "url": "rtmp://10.37.3.33:1935/live/av0"},
    ],
    "segment_time": 1800,
    "last": {"project": "", "dest": "", "cams": ["cam1", "cam2", "cam3"]},
}

lock = threading.RLock()


# ---------------------------------------------------------------- config
def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(cfg)
    except Exception as e:  # fichier corrompu : on repart des valeurs par défaut sans l'écraser
        print(f"[config] lecture impossible ({e}), valeurs par défaut utilisées")
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except OSError as e:
        print(f"[config] écriture impossible : {e}")


config = load_config()


# ---------------------------------------------------------------- emplacements
def _unescape_mount(p):
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), p)


def locations():
    """Points de montage sous les racines autorisées + les racines elles-mêmes."""
    paths = []
    for r in ROOTS:
        if os.path.isdir(r):
            paths.append(r)
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = _unescape_mount(parts[1])
                if any(mp == r or mp.startswith(r + "/") for r in ROOTS) and mp not in paths:
                    paths.append(mp)
    except OSError:
        pass
    out = []
    for p in sorted(set(paths)):
        try:
            du = shutil.disk_usage(p)
        except OSError:
            continue
        out.append({
            "path": p,
            "total": du.total,
            "free": du.free,
            "writable": os.access(p, os.W_OK),
            "mount": os.path.ismount(p),
        })
    return out


def safe_name(s):
    s = re.sub(r"[^\w\-. ]+", "_", (s or "").strip(), flags=re.UNICODE).strip(" .")
    return s[:80]


# ---------------------------------------------------------------- enregistreur
class Recorder:
    def __init__(self, cam, out_dir, segment_time):
        self.cam = cam
        self.out_dir = Path(out_dir)
        self.segment_time = int(segment_time)
        self.prefix = safe_name(cam["id"]) or "cam"
        self.proc = None
        self.state = "starting"
        self.error = ""
        self.started_at = time.time()
        self.restarts = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.log_path = self.out_dir / f"{self.prefix}.log"

    def start(self):
        self.thread.start()

    def _next_index(self):
        n = -1
        for f in self.out_dir.glob(f"{self.prefix}_*.mp4"):
            m = re.fullmatch(rf"{re.escape(self.prefix)}_(\d+)\.mp4", f.name)
            if m:
                n = max(n, int(m.group(1)))
        return n + 1

    def _cmd(self):
        url = self.cam["url"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
        # vignette : on ne décode que les images clés (quasi gratuit), la copie n'est pas touchée
        cmd += ["-skip_frame", "nokey"]
        if url.startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
        else:
            cmd += ["-rw_timeout", "5000000"]
        cmd += [
            "-i", url,
            "-map", "0", "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_time),
            "-segment_format", "mp4",
            "-segment_start_number", str(self._next_index()),
            "-reset_timestamps", "1",
            str(self.out_dir / f"{self.prefix}_%03d.mp4"),
            "-map", "0:v:0?", "-an", "-vf", "fps=1/5,scale=480:-2", "-q:v", "6",
            "-f", "image2", "-update", "1", str(THUMB_DIR / f"{self.prefix}.jpg"),
        ]
        return cmd

    def _run(self):
        while not self._stop.is_set():
            try:
                log = open(self.log_path, "a", encoding="utf-8")
                log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} démarrage ({self.cam['url']})\n")
                log.flush()
                self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                             stderr=log)
            except Exception as e:
                self.state, self.error = "error", str(e)
                return
            self.state = "recording"
            t0 = time.time()
            rc = self.proc.wait()
            log.close()
            if self._stop.is_set():
                break
            # coupure inattendue : on relance (nouveau fichier, numérotation continue)
            self.error = self._last_log_line() or f"ffmpeg terminé (code {rc})"
            self.state = "reconnecting"
            self.restarts += 1
            delay = 2 if time.time() - t0 > 30 else 5
            self._stop.wait(delay)
        self.state = "stopped"

    def _last_log_line(self):
        try:
            lines = [l.strip() for l in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            return lines[-1][:200] if lines else ""
        except OSError:
            return ""

    def stop(self):
        self._stop.set()
        p = self.proc
        if p and p.poll() is None:
            try:  # 'q' = arrêt propre, l'en-tête MP4 est finalisé
                p.stdin.write(b"q")
                p.stdin.flush()
            except Exception:
                pass

    def finish(self, timeout=15):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.thread.join(timeout=2)
        if self.state != "error":
            self.state = "stopped"

    def files(self):
        out = []
        for f in sorted(self.out_dir.glob(f"{self.prefix}_*.mp4")):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime >= self.started_at - 1:
                out.append({"name": f.name, "size": st.st_size})
        return out


session = None  # {"project","dir","started_at","recorders":{id:Recorder}}


def start_session(project, dest, cam_ids, segment_time):
    global session
    with lock:
        if session and any(r.state in ("starting", "recording", "reconnecting") for r in session["recorders"].values()):
            raise ValueError("Un enregistrement est déjà en cours.")
        valid = {l["path"]: l for l in locations()}
        if dest not in valid:
            raise ValueError("Emplacement inconnu : choisis un disque dans la liste.")
        if not valid[dest]["writable"]:
            raise ValueError(f"Pas de droit d'écriture sur {dest}.")
        cams = {c["id"]: c for c in config["cameras"]}
        cam_ids = [c for c in cam_ids if c in cams]
        if not cam_ids:
            raise ValueError("Aucune caméra sélectionnée.")
        project = safe_name(project) or datetime.now().strftime("rec_%Y-%m-%d_%H%M")
        out_dir = Path(dest) / project
        out_dir.mkdir(parents=True, exist_ok=True)
        segment_time = max(60, int(segment_time or config["segment_time"]))
        recs = {cid: Recorder(cams[cid], out_dir, segment_time) for cid in cam_ids}
        for r in recs.values():
            r.start()
        session = {"project": project, "dir": str(out_dir), "started_at": time.time(), "recorders": recs}
        config["last"] = {"project": project, "dest": dest, "cams": cam_ids}
        config["segment_time"] = segment_time
        save_config(config)
        return session


def stop_session():
    with lock:
        s = session
    if not s:
        return
    recs = list(s["recorders"].values())
    for r in recs:
        r.stop()
    for r in recs:
        r.finish()
    with lock:
        s["stopped_at"] = time.time()


def status():
    with lock:
        s = session
        cams = []
        for c in config["cameras"]:
            r = s["recorders"].get(c["id"]) if s else None
            files = r.files() if r else []
            cams.append({
                "id": c["id"], "name": c["name"], "url": c["url"],
                "state": r.state if r else "idle",
                "error": r.error if r else "",
                "restarts": r.restarts if r else 0,
                "files": files,
                "bytes": sum(f["size"] for f in files),
            })
        active = bool(s) and any(r.state in ("starting", "recording", "reconnecting") for r in s["recorders"].values())
        free = None
        if s:
            try:
                free = shutil.disk_usage(s["dir"]).free
            except OSError:
                pass
        return {
            "recording": active,
            "project": s["project"] if s else None,
            "dir": s["dir"] if s else None,
            "started_at": s["started_at"] if s else None,
            "stopped_at": s.get("stopped_at") if s else None,
            "free": free,
            "now": time.time(),
            "cameras": cams,
            "segment_time": config["segment_time"],
            "last": config["last"],
        }


# ---------------------------------------------------------------- vignettes
_thumbs = {}
_thumb_lock = threading.Lock()


def thumbnail(cam_id):
    cam = next((c for c in config["cameras"] if c["id"] == cam_id), None)
    if not cam:
        return None
    # pendant l'enregistrement : vignette écrite par l'ffmpeg qui enregistre (pas de 2e connexion)
    f = THUMB_DIR / f"{safe_name(cam_id)}.jpg"
    with lock:
        rec = session["recorders"].get(cam_id) if session else None
    if rec and rec.state in ("recording", "reconnecting"):
        try:
            if time.time() - f.stat().st_mtime < 30:
                return f.read_bytes()
        except OSError:
            pass
        return None
    with _thumb_lock:
        hit = _thumbs.get(cam_id)
        if hit and time.time() - hit[0] < 5:
            return hit[1]
    url = cam["url"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd += ["-rtsp_transport", "tcp"] if url.startswith("rtsp") else ["-rw_timeout", "5000000"]
    cmd += ["-i", url, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    try:
        data = subprocess.run(cmd, capture_output=True, timeout=12).stdout
    except subprocess.TimeoutExpired:
        data = b""
    if not data:
        return None
    with _thumb_lock:
        _thumbs[cam_id] = (time.time(), data)
    return data


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "CamRec"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, status())
        if path == "/api/locations":
            return self._send(200, locations())
        if path == "/api/config":
            return self._send(200, {"cameras": config["cameras"], "segment_time": config["segment_time"]})
        m = re.fullmatch(r"/api/thumb/([\w\-]+)", path)
        if m:
            data = thumbnail(m.group(1))
            return self._send(200, data, "image/jpeg") if data else self._send(504, {"error": "pas d'image"})
        self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = self.path.split("?")[0]
        data = self._json()
        if path == "/api/start":
            last = config["last"]
            try:
                start_session(
                    data.get("project", last.get("project", "")) if "project" in data else last.get("project", ""),
                    data.get("dest") or last.get("dest", ""),
                    data.get("cams") or last.get("cams", []),
                    data.get("segment_time") or config["segment_time"],
                )
            except ValueError as e:
                return self._send(409, {"error": str(e)})
            return self._send(200, status())
        if path == "/api/stop":
            threading.Thread(target=stop_session, daemon=True).start()
            return self._send(200, {"ok": True})
        if path == "/api/config":
            if status()["recording"]:
                return self._send(409, {"error": "Arrête l'enregistrement avant de modifier les caméras."})
            cams, seen = [], set()
            for c in data.get("cameras", []):
                cid = safe_name(c.get("id", "")).replace(" ", "_")
                url = (c.get("url") or "").strip()
                if not cid or cid in seen or not re.match(r"^(rtmp|rtmps|rtsp|srt|http|https|udp)://", url):
                    return self._send(400, {"error": f"Caméra invalide : {c.get('id') or '(sans id)'}"})
                seen.add(cid)
                cams.append({"id": cid, "name": (c.get("name") or cid).strip()[:60], "url": url})
            with lock:
                config["cameras"] = cams
                if data.get("segment_time"):
                    config["segment_time"] = max(60, int(data["segment_time"]))
                save_config(config)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "introuvable"})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def shutdown(*_):
        print("[camrec] arrêt : finalisation des fichiers…")
        stop_session()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"[camrec] http://0.0.0.0:{PORT}  racines : {', '.join(ROOTS)}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
