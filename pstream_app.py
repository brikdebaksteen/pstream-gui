"""
PSTREAM v2 - Flask backend
- /search   → zoekt via apibay, verrijkt met TMDb
- /files    → bestandslijst + seizoensboom
- /play     → webtorrent HTTP server + MPV met exacte bestand-URL
- /download → qBittorrent Web API (volledig / per seizoen / per aflevering)
- /status   → streaming status
- /stop     → stop actieve stream
"""

import subprocess, requests, os, shutil, threading, json, re, time
from flask import Flask, request, jsonify, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, template_folder=BASE_DIR)

# ── Configuratie ──────────────────────────────────────────────────────────
TMDB_API_KEY = "89d5a88de22ac69885df28f77028231f"
CACHE_DIR    = "/var/tmp/pstream"
QB_URL       = "http://localhost:8080"
QB_USER      = "admin"
QB_PASS      = "adminadmin"
DOWNLOAD_DIR = os.path.expanduser("~/Downloads/PSTREAM")

# ── Streaming state ────────────────────────────────────────────────────────
_state = {"wt": None, "mpv": None, "hash": None, "status": "idle", "speed": "", "peers": "", "url": None}
_lock  = threading.Lock()

def cleanup_cache():
    if os.path.exists(CACHE_DIR): shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)

def magnet(info_hash):
    tr = "&tr=".join([
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.tracker.cl:1337/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
        "udp://exodus.desync.com:6969/announce",
    ])
    return f"magnet:?xt=urn:btih:{info_hash}&tr={tr}"

# ── TMDb ──────────────────────────────────────────────────────────────────
def tmdb(title):
    try:
        d = requests.get("https://api.themoviedb.org/3/search/multi",
            params={"api_key": TMDB_API_KEY, "query": title}, timeout=5).json()
        if d.get('results'):
            item = d['results'][0]
            mt   = item.get('media_type', 'movie')
            det  = requests.get(f"https://api.themoviedb.org/3/{mt}/{item['id']}",
                params={"api_key": TMDB_API_KEY}, timeout=5).json()
            pp = item.get('poster_path')
            return {
                "poster":      f"https://image.tmdb.org/t/p/w500{pp}" if pp else None,
                "description": item.get('overview', ''),
                "duration":    det.get('runtime') or (det.get('episode_run_time') or [None])[0],
                "rating":      item.get('vote_average'),
                "year":        (item.get('release_date') or item.get('first_air_date') or "????")[:4],
                "type":        mt,
            }
    except: pass
    return None

# ── Bestandsboom ──────────────────────────────────────────────────────────
VID   = re.compile(r'\.(mkv|mp4|avi|mov|m4v|wmv|flv|ts|webm)$', re.I)
SRE   = re.compile(r'[Ss]eason\s*(\d+)|[Ss](\d{1,2})[Ee]\d{2}', re.I)
EPRE  = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})', re.I)

def build_tree(files):
    vids = [{"index": i, "name": os.path.basename(f["name"]), "path": f["name"], "size": f.get("size", 0)}
            for i, f in enumerate(files) if VID.search(f.get("name", ""))]

    seasons = {}
    orphans = []
    for vf in vids:
        sm = SRE.search(vf["path"])
        if sm:
            s = int(next(g for g in sm.groups() if g is not None))
            em = EPRE.search(vf["name"])
            vf["season"]  = s
            vf["episode"] = int(em.group(2)) if em else 0
            seasons.setdefault(s, []).append(vf)
        else:
            vf["season"] = vf["episode"] = None
            orphans.append(vf)

    if not seasons:
        return {"type": "flat", "files": sorted(vids, key=lambda x: x["name"])}

    tree = [{"season": s, "episodes": sorted(seasons[s], key=lambda x: x["episode"])}
            for s in sorted(seasons)]
    return {"type": "series", "seasons": tree, "orphans": orphans}

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    if not q: return jsonify([])
    try:
        torrents = requests.get("https://apibay.org/q.php", params={"q": q},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
    except Exception as e:
        print(f"Search error: {e}"); return jsonify([])

    if not isinstance(torrents, list) or not torrents: return jsonify([])
    if 'No results' in str(torrents[0].get('name', '')): return jsonify([])

    torrents = sorted([t for t in torrents if int(t.get('seeders', 0)) > 0],
                      key=lambda t: int(t.get('seeders', 0)), reverse=True)

    result = []
    for t in torrents[:15]:
        raw   = t.get('name', '').replace('.', ' ').replace('_', ' ')
        clean = re.split(r'1080p|720p|2160p|BluRay|WEB[-\s]?DL|HDTV|x26[45]|HEVC|\(|\[|S\d{2}E\d{2}',
                         raw, flags=re.I)[0].strip()
        t['metadata'] = tmdb(clean)
        result.append(t)
    return jsonify(result)

@app.route('/files')
def files_route():
    h = request.args.get('hash', '').strip()
    if not h: return jsonify({"type": "flat", "files": []})
    try:
        data = requests.get("https://apibay.org/f.php", params={"id": h},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8).json()
        if not isinstance(data, list) or not data:
            return jsonify({"type": "flat", "files": []})
        flat = []
        for e in data:
            name = e.get('name', {})
            size = e.get('size', {})
            if isinstance(name, dict): name = list(name.values())[0] if name else ''
            if isinstance(size, dict): size = list(size.values())[0] if size else 0
            flat.append({"name": str(name), "size": int(size or 0)})
        return jsonify(build_tree(flat))
    except Exception as e:
        print(f"Files error: {e}")
        return jsonify({"type": "flat", "files": []})

@app.route('/play')
def play():
    h          = request.args.get('hash', '').strip().lower()
    file_index = request.args.get('index', '-1')   # -1 = niet opgegeven
    file_path  = request.args.get('path', '').strip()

    if not h: return jsonify({"error": "geen hash"}), 400

    def do_stream():
        with _lock:
            for key in ('wt', 'mpv'):
                proc = _state.get(key)
                if proc and proc.poll() is None:
                    proc.terminate()
            _state.update(status="connecting", hash=h, speed="", peers="", url=None)

        cleanup_cache()

        VIDEO_RE = re.compile(r'\.(mkv|mp4|avi|mov|m4v|wmv|flv|ts|webm)$', re.I)

        # Geen --select: webtorrent serveert alle bestanden via HTTP.
        # We kiezen zelf het juiste bestand via de URL.
        cmd = ['webtorrent', magnet(h), '--out', CACHE_DIR]
        print(f"▶ {' '.join(cmd)}")

        wt = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1)
        with _lock: _state['wt'] = wt

        mpv_started  = False
        server_ready = False   # webtorrent HTTP server is gestart
        stream_url   = None

        for line in wt.stdout:
            line = line.rstrip()
            if line: print(f"[wt] {line}")

            # Stats bijhouden
            m = re.search(r'Speed:\s*([\d.]+\s*\w+/s)', line)
            if m:
                with _lock: _state['speed'] = m.group(1)
            m = re.search(r'Peers:\s*(\d+/\d+)', line)
            if m:
                with _lock: _state['peers'] = m.group(1)

            # Wacht op de "Server running at:" regel — die bevestigt dat de
            # HTTP server van webtorrent actief is op poort 8000.
            if not server_ready and 'Server running at:' in line:
                server_ready = True

                if file_path and VIDEO_RE.search(file_path):
                    # Gebruiker heeft een specifiek bestand gekozen via de UI.
                    # Bouw de URL op: elke component apart encoden, slashes behouden.
                    parts = file_path.replace('\\', '/').split('/')
                    enc   = '/'.join(requests.utils.quote(p, safe='') for p in parts if p)
                    stream_url = f"http://localhost:8000/webtorrent/{h}/{enc}"
                else:
                    # Geen pad opgegeven (bijv. film zonder bestandskeuze).
                    # Gebruik de URL die webtorrent zelf print, maar alleen als
                    # het een videobestand is. Anders wachten we op de volgende
                    # "Server running at:" regel (webtorrent herhaalt die elke seconde).
                    url_m = re.search(r'Server running at:\s*(http://\S+)', line)
                    if url_m:
                        candidate = url_m.group(1)
                        if VIDEO_RE.search(candidate):
                            stream_url = candidate
                        else:
                            server_ready = False  # reset: wacht op volgende regel met video-URL

            # Zodra we een geldige stream_url hebben, start MPV
            if stream_url and not mpv_started:
                print(f"🎬 MPV ← {stream_url}")
                with _lock:
                    _state['url']    = stream_url
                    _state['status'] = 'streaming'

                mpv = subprocess.Popen([
                    'mpv',
                    '--vo=gpu',
                    '--gpu-context=auto',
                    '--force-window=immediate',
                    '--force-seekable=yes',
                    '--cache=yes',
                    '--cache-secs=90',
                    '--demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5',
                    '--hwdec=no',
                    '--no-config',
                    '--title=PSTREAM',
                    stream_url
                ], env=os.environ.copy(), stdin=subprocess.DEVNULL)
                with _lock: _state['mpv'] = mpv
                mpv_started = True

        wt.wait()
        with _lock: _state['status'] = 'idle'

    threading.Thread(target=do_stream, daemon=True).start()
    return jsonify({"ok": True})

@app.route('/status')
def status():
    with _lock:
        return jsonify({k: _state[k] for k in ('status','hash','speed','peers','url')})

@app.route('/stop')
def stop():
    with _lock:
        for key in ('mpv', 'wt'):
            p = _state.get(key)
            if p and p.poll() is None: p.terminate()
        _state['status'] = 'idle'
    cleanup_cache()
    return jsonify({"ok": True})

@app.route('/download')
def download():
    h            = request.args.get('hash', '').strip().lower()
    save_path    = request.args.get('savepath', DOWNLOAD_DIR)
    file_indices = request.args.get('files', '')

    if not h: return jsonify({"error": "geen hash"}), 400
    os.makedirs(save_path, exist_ok=True)

    sess = requests.Session()
    try:
        r = sess.post(f"{QB_URL}/api/v2/auth/login",
                      data={"username": QB_USER, "password": QB_PASS}, timeout=5)
        if r.text.strip() != "Ok.":
            return jsonify({"error": f"qBittorrent login mislukt: '{r.text.strip()}' — controleer QB_USER/QB_PASS in pstream_app.py"}), 500
    except Exception as e:
        return jsonify({"error": f"qBittorrent niet bereikbaar op {QB_URL} — zet Web UI aan via qBittorrent → Voorkeuren → Web UI (poort 8080)"}), 503

    try:
        r = sess.post(f"{QB_URL}/api/v2/torrents/add",
                      data={"urls": magnet(h), "savepath": save_path,
                            "category": "PSTREAM", "autoTMM": "false"}, timeout=5)
        if r.text.strip() != "Ok.":
            return jsonify({"error": f"Toevoegen mislukt: {r.text}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Ongewenste bestanden op prioriteit 0 zetten
    if file_indices:
        time.sleep(3)
        wanted = set(int(x) for x in file_indices.split(',') if x.strip().isdigit())
        try:
            files_r = sess.get(f"{QB_URL}/api/v2/torrents/files",
                               params={"hash": h}, timeout=5)
            all_files = files_r.json()
            for i in range(len(all_files)):
                if i not in wanted:
                    sess.post(f"{QB_URL}/api/v2/torrents/filePrio",
                              data={"hash": h, "id": str(i), "priority": "0"}, timeout=5)
        except Exception as e:
            print(f"File prio fout: {e}")

    return jsonify({"ok": True, "savepath": save_path,
                    "message": f"Download gestart → {save_path}"})


if __name__ == '__main__':
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    cleanup_cache()
    app.run(debug=True, port=5000, threaded=True)
