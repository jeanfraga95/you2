#!/usr/bin/env python3
# ================================================================
#  YouTube Stream Server v2.0
#  - Suporte robusto a lives (auto-renovação de URL)
#  - Painel web para gerenciar canais/vídeos
#  - Cache inteligente: curto para lives, longo para VODs
#  - Installer de dependências incluso (install.sh)
# ================================================================

import os, re, time, json, threading, hashlib, secrets
from datetime import datetime
from flask import Flask, Response, jsonify, redirect, request, session

# ── yt-dlp ──────────────────────────────────────────────────────
try:
    import yt_dlp
except ImportError:
    raise SystemExit("yt-dlp não encontrado. Execute: pip3 install yt-dlp")

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)   # gerado uma vez por processo

# ================================================================
#  CONFIGURAÇÕES
# ================================================================
PANEL_PASSWORD  = "admin123"          # 🔒 TROQUE ESTA SENHA
DATA_FILE       = "channels.json"
PORT            = 8010

CACHE_VOD       = 3600   # 1h   – vídeos normais
CACHE_LIVE      = 900    # 15min – lives (URL expira ~6h, mas renovamos cedo)
CACHE_LIVE_WARN = 120    # renova quando restar < 2min

# ================================================================
#  ESTADO GLOBAL
# ================================================================
cache   = {}           # video_id → {url, ts, is_live, title}
entries = []           # lista de entradas gerenciadas pelo painel
cache_lock = threading.Lock()

stats = {"req": 0, "hits": 0, "errors": 0, "start": time.time()}

# ================================================================
#  PERSISTÊNCIA
# ================================================================
def load_entries():
    global entries
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                entries = json.load(f)
        except Exception:
            entries = []

def save_entries():
    with open(DATA_FILE, "w") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

# ================================================================
#  YT-DLP
# ================================================================
def ydl_opts(is_live_hint=False):
    fmt = "best[height<=720]/best" if not is_live_hint else "best[height<=720]/best"
    return {
        "quiet":              True,
        "no_warnings":        True,
        "format":             fmt,
        "geo_bypass":         True,
        "geo_bypass_country": "BR",
        "skip_download":      True,
        "socket_timeout":     30,
        "retries":            5,
        "fragment_retries":   5,
        "noplaylist":         True,
        "live_from_start":    False,  # não bufferiza tudo
        "http_headers": {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    }

def fetch_stream(video_id: str) -> dict | None:
    """Extrai a URL de stream de um vídeo. Retorna dict ou None."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        # Live ainda não começou
        if "This live event will begin" in msg or "Premieres" in msg:
            return {"error": "live_not_started", "msg": msg}
        return {"error": "download_error", "msg": msg}
    except Exception as e:
        return {"error": "generic", "msg": str(e)}

    is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")

    # Escolhe a melhor URL de stream
    stream_url = None
    if info.get("url"):
        stream_url = info["url"]
    elif info.get("formats"):
        # Preferência: mp4 com video+audio, até 720p
        candidates = [
            f for f in info["formats"]
            if f.get("url") and f.get("vcodec") != "none" and f.get("acodec") != "none"
        ]
        if not candidates:
            candidates = [f for f in info["formats"] if f.get("url")]
        # Ordena por altura desc, pega ≤ 720
        candidates.sort(key=lambda f: f.get("height") or 0, reverse=True)
        for f in candidates:
            if (f.get("height") or 9999) <= 720:
                stream_url = f["url"]
                break
        if not stream_url and candidates:
            stream_url = candidates[-1]["url"]

    if not stream_url:
        return {"error": "no_url", "msg": "Nenhuma URL de stream encontrada."}

    return {
        "url":     stream_url,
        "is_live": is_live,
        "title":   info.get("title", ""),
        "ts":      time.time(),
    }

def get_cached(video_id: str) -> dict | None:
    """Retorna cache válido ou None."""
    with cache_lock:
        entry = cache.get(video_id)
    if not entry or "error" in entry:
        return None
    ttl = CACHE_LIVE if entry.get("is_live") else CACHE_VOD
    age = time.time() - entry["ts"]
    if age < ttl - CACHE_LIVE_WARN:
        return entry
    return None

def get_stream_url(video_id: str) -> dict:
    """Retorna URL (do cache ou buscando), sempre atualizado para lives."""
    cached = get_cached(video_id)
    if cached:
        stats["hits"] += 1
        return cached

    result = fetch_stream(video_id)
    if result and "url" in result:
        with cache_lock:
            cache[video_id] = result
    return result or {"error": "generic", "msg": "Falha desconhecida."}

# ================================================================
#  RENOVAÇÃO AUTOMÁTICA DE LIVES
# ================================================================
def live_refresher():
    """Thread que renova URLs de lives antes de expirarem."""
    while True:
        time.sleep(60)
        with cache_lock:
            ids_to_refresh = [
                vid for vid, entry in cache.items()
                if entry.get("is_live")
                and "error" not in entry
                and (time.time() - entry["ts"]) >= (CACHE_LIVE - CACHE_LIVE_WARN)
            ]
        for vid in ids_to_refresh:
            print(f"[live-refresh] renovando {vid}")
            result = fetch_stream(vid)
            if result and "url" in result:
                with cache_lock:
                    cache[vid] = result
            else:
                print(f"[live-refresh] falha em {vid}: {result}")

threading.Thread(target=live_refresher, daemon=True).start()

# ================================================================
#  ROTAS DE STREAM
# ================================================================
@app.route("/<video_id>")
def stream(video_id):
    # Ignora rotas especiais
    if video_id in ("panel", "api", "status", "favicon.ico"):
        return jsonify({"erro": "rota inválida"}), 400

    if not re.match(r'^[\w-]{11,}$', video_id):
        return jsonify({"erro": "ID inválido", "exemplo": "/dQw4w9WgXcQ"}), 400

    stats["req"] += 1
    result = get_stream_url(video_id)

    if "url" in result:
        resp = redirect(result["url"], 302)
        resp.headers["X-Stream-Live"]  = str(result.get("is_live", False))
        resp.headers["X-Stream-Title"] = result.get("title", "")[:80]
        resp.headers["X-Cache-Age"]    = str(int(time.time() - result.get("ts", time.time())))
        return resp

    err = result.get("error", "generic")
    if err == "live_not_started":
        return jsonify({"erro": "Live ainda não iniciou", "detalhe": result["msg"]}), 503
    stats["errors"] += 1
    return jsonify({"erro": result.get("msg", "Erro desconhecido")}), 502

@app.route("/status")
def status_route():
    uptime = time.time() - stats["start"]
    eff    = (stats["hits"] / stats["req"] * 100) if stats["req"] else 0
    return jsonify({
        "status":       "online",
        "versao":       "2.0",
        "uptime":       f"{int(uptime//3600)}h {int((uptime%3600)//60)}m",
        "requisicoes":  stats["req"],
        "cache_hits":   stats["hits"],
        "eficiencia":   f"{eff:.1f}%",
        "erros":        stats["errors"],
        "cache_itens":  len(cache),
        "entradas":     len(entries),
    })

# ================================================================
#  PAINEL WEB  —  AUTH
# ================================================================
def auth_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            if request.is_json:
                return jsonify({"erro": "Não autenticado"}), 401
            return redirect("/panel/login")
        return f(*args, **kwargs)
    return wrapper

@app.route("/panel/login", methods=["GET", "POST"])
def panel_login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == PANEL_PASSWORD:
            session["auth"] = True
            return redirect("/panel")
        error = "Senha incorreta."
    return render_login(error)

@app.route("/panel/logout")
def panel_logout():
    session.clear()
    return redirect("/panel/login")

# ================================================================
#  PAINEL WEB  —  API
# ================================================================
@app.route("/panel/api/entries", methods=["GET"])
@auth_required
def api_list():
    host = request.host
    proto = "https" if request.is_secure else "http"
    result = []
    for e in entries:
        vid = e["video_id"]
        cached = cache.get(vid)
        result.append({
            **e,
            "vlc_link":   f"{proto}://{host}/{vid}",
            "is_live":    cached.get("is_live", False) if cached else None,
            "cache_age":  int(time.time() - cached["ts"]) if cached and "ts" in cached else None,
            "cache_title":cached.get("title","") if cached else "",
        })
    return jsonify(result)

@app.route("/panel/api/entries", methods=["POST"])
@auth_required
def api_add():
    data = request.get_json()
    yt_url = (data.get("url") or "").strip()
    name   = (data.get("name") or "").strip()

    # Extrai video ID
    vid = None
    m = re.search(r'(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})', yt_url)
    if m:
        vid = m.group(1)

    if not vid:
        return jsonify({"erro": "URL inválida ou ID não encontrado"}), 400

    # Duplicado?
    if any(e["video_id"] == vid for e in entries):
        return jsonify({"erro": "Este vídeo já está cadastrado"}), 409

    # Busca info em background para preencher título
    def bg_fetch():
        result = fetch_stream(vid)
        if result and "url" in result:
            # Atualiza nome se não fornecido
            for e in entries:
                if e["video_id"] == vid and not e["name"]:
                    e["name"] = result.get("title", vid)
            save_entries()

    entry = {
        "id":         int(time.time() * 1000),
        "video_id":   vid,
        "name":       name or "",
        "url":        yt_url,
        "added_at":   datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    entries.append(entry)
    save_entries()
    threading.Thread(target=bg_fetch, daemon=True).start()
    return jsonify({"ok": True, "entry": entry})

@app.route("/panel/api/entries/<int:entry_id>", methods=["DELETE"])
@auth_required
def api_delete(entry_id):
    global entries
    before = len(entries)
    entries = [e for e in entries if e["id"] != entry_id]
    if len(entries) == before:
        return jsonify({"erro": "Não encontrado"}), 404
    save_entries()
    return jsonify({"ok": True})

@app.route("/panel/api/entries/<int:entry_id>", methods=["PUT"])
@auth_required
def api_edit(entry_id):
    data = request.get_json()
    for e in entries:
        if e["id"] == entry_id:
            e["name"] = (data.get("name") or e["name"]).strip()
            new_url = (data.get("url") or "").strip()
            if new_url and new_url != e["url"]:
                m = re.search(r'(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})', new_url)
                if not m:
                    return jsonify({"erro": "URL inválida"}), 400
                old_vid = e["video_id"]
                e["video_id"] = m.group(1)
                e["url"] = new_url
                # Limpa cache do antigo
                with cache_lock:
                    cache.pop(old_vid, None)
            save_entries()
            return jsonify({"ok": True, "entry": e})
    return jsonify({"erro": "Não encontrado"}), 404

@app.route("/panel/api/refresh/<video_id>", methods=["POST"])
@auth_required
def api_refresh(video_id):
    with cache_lock:
        cache.pop(video_id, None)
    result = get_stream_url(video_id)
    if "url" in result:
        return jsonify({"ok": True, "is_live": result.get("is_live"), "title": result.get("title","")})
    return jsonify({"erro": result.get("msg","Falha")}), 502

# ================================================================
#  PAINEL WEB  —  HTML
# ================================================================
@app.route("/panel")
@app.route("/panel/")
@auth_required
def panel():
    return PANEL_HTML

def render_login(error=""):
    err_html = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Stream — Login</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#080c10;font-family:'JetBrains Mono',monospace;color:#cdd6f4;}}
.box{{background:#0d1117;border:1px solid #ff0040;padding:48px 40px;width:380px;position:relative;}}
.box::after{{content:'';position:absolute;bottom:-1px;right:24px;width:60px;height:2px;background:#ff0040;}}
h1{{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:2px;color:#fff;margin-bottom:4px;}}
.sub{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#555;margin-bottom:36px;}}
label{{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#666;display:block;margin-bottom:8px;}}
input{{width:100%;background:#080c10;border:1px solid #1e2430;color:#fff;padding:12px 16px;
  font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;}}
input:focus{{border-color:#ff0040;}}
button{{margin-top:20px;width:100%;background:#ff0040;color:#fff;border:none;padding:13px;
  font-family:'Bebas Neue',sans-serif;font-size:16px;letter-spacing:2px;cursor:pointer;}}
button:hover{{background:#cc0033;}}
.err{{margin-top:12px;color:#ff4466;font-size:11px;}}
.icon{{font-size:36px;margin-bottom:20px;display:block;}}
</style>
</head>
<body>
<form method="POST">
  <div class="box">
    <span class="icon">▶</span>
    <h1>Stream Manager</h1>
    <p class="sub">YouTube · VLC · HLS</p>
    <label>Senha</label>
    <input type="password" name="password" autofocus placeholder="••••••••">
    {err_html}
    <button type="submit">ENTRAR</button>
  </div>
</form>
</body>
</html>"""

PANEL_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Stream Manager</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#080c10;--s1:#0d1117;--s2:#111820;--border:#1e2430;
  --red:#ff0040;--orange:#ff6b00;--green:#00e676;--yellow:#ffd600;--blue:#4fc3f7;
  --text:#cdd6f4;--muted:#4a5568;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;min-height:100vh;}

/* HEADER */
header{background:var(--s1);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;padding:0 28px;height:56px;
  position:sticky;top:0;z-index:100;}
.logo{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:2px;
  display:flex;align-items:center;gap:10px;}
.logo-dot{width:10px;height:10px;background:var(--red);animation:blink 1.4s infinite;}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.hdr-right{display:flex;align-items:center;gap:16px;}
.stat-pill{background:var(--s2);border:1px solid var(--border);
  padding:4px 12px;font-size:10px;color:var(--muted);}
.stat-pill span{color:var(--text);}
a.logout{color:var(--muted);font-size:10px;text-decoration:none;letter-spacing:1px;
  text-transform:uppercase;border:1px solid var(--border);padding:5px 12px;transition:.2s;}
a.logout:hover{color:var(--red);border-color:var(--red);}

/* LAYOUT */
.wrap{max-width:1280px;margin:0 auto;padding:28px;}
.cols{display:grid;grid-template-columns:340px 1fr;gap:24px;align-items:start;}

/* CARDS */
.card{background:var(--s1);border:1px solid var(--border);padding:24px;}
.card-hd{font-family:'Bebas Neue',sans-serif;font-size:14px;letter-spacing:1.5px;
  color:#fff;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px;}
.card-hd::before{content:'';width:4px;height:14px;background:var(--red);}

/* FORM */
.fg{margin-bottom:14px;}
label.fl{display:block;font-size:10px;letter-spacing:1px;text-transform:uppercase;
  color:var(--muted);margin-bottom:6px;}
input.fi,textarea.fi{width:100%;background:var(--bg);border:1px solid var(--border);
  color:var(--text);padding:10px 13px;font-family:'JetBrains Mono',monospace;
  font-size:12px;outline:none;resize:vertical;transition:.2s;}
input.fi:focus,textarea.fi:focus{border-color:var(--red);}
input.fi::placeholder,textarea.fi::placeholder{color:#2a3040;}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:5px;padding:9px 16px;
  font-family:'Bebas Neue',sans-serif;font-size:13px;letter-spacing:1px;
  cursor:pointer;border:none;transition:.15s;text-transform:uppercase;}
.btn-red{background:var(--red);color:#fff;}
.btn-red:hover{background:#cc0033;}
.btn-full{width:100%;justify-content:center;}
.btn-sm{padding:5px 10px;font-size:11px;}
.btn-ghost{background:transparent;color:var(--muted);border:1px solid var(--border);}
.btn-ghost:hover{color:var(--text);border-color:var(--text);}
.btn-warn{background:transparent;color:var(--yellow);border:1px solid var(--yellow);}
.btn-warn:hover{background:var(--yellow);color:#000;}
.btn-danger{background:transparent;color:var(--red);border:1px solid var(--red);}
.btn-danger:hover{background:var(--red);color:#fff;}
.btn-green{background:transparent;color:var(--green);border:1px solid var(--green);}
.btn-green:hover{background:var(--green);color:#000;}

/* TABLE */
.tbl{width:100%;border-collapse:collapse;}
.tbl th{text-align:left;padding:9px 12px;font-size:10px;letter-spacing:1.5px;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);}
.tbl td{padding:12px;border-bottom:1px solid #0f1520;vertical-align:middle;}
.tbl tr:last-child td{border-bottom:none;}
.tbl tr:hover td{background:var(--s2);}
.id-tag{display:inline-block;background:var(--s2);border:1px solid var(--border);
  padding:2px 7px;font-size:11px;color:var(--red);font-weight:700;}
.entry-name{font-weight:700;color:#fff;margin-bottom:3px;}
.entry-vid{font-size:10px;color:var(--muted);}
.live-badge{display:inline-flex;align-items:center;gap:5px;font-size:10px;
  padding:2px 8px;letter-spacing:.5px;text-transform:uppercase;}
.live-badge.live{background:#0a2010;border:1px solid var(--green);color:var(--green);}
.live-badge.vod{background:#101018;border:1px solid var(--border);color:var(--muted);}
.live-badge.unknown{background:#101018;border:1px solid var(--border);color:var(--muted);}
.live-badge.live::before{content:'●';animation:blink 1s infinite;}
.vlc-link{font-size:11px;background:var(--bg);border:1px solid var(--border);
  padding:6px 10px;cursor:pointer;transition:.2s;color:var(--orange);
  word-break:break-all;display:block;}
.vlc-link:hover{border-color:var(--orange);}
.actions{display:flex;gap:5px;flex-wrap:wrap;}
.date{font-size:10px;color:var(--muted);}

/* STATUS BAR */
.status-bar{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px;}
.status-item{background:var(--s1);border:1px solid var(--border);
  padding:12px 20px;flex:1;min-width:120px;}
.status-label{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.status-val{font-family:'Bebas Neue',sans-serif;font-size:24px;color:#fff;margin-top:2px;}

/* EMPTY */
.empty{text-align:center;padding:60px;color:var(--muted);}
.empty-icon{font-size:48px;opacity:.2;margin-bottom:16px;}

/* TOAST */
.toast{position:fixed;bottom:20px;right:20px;background:var(--green);color:#000;
  padding:11px 18px;font-family:'Bebas Neue',sans-serif;font-size:13px;letter-spacing:1px;
  opacity:0;transition:.3s;z-index:999;pointer-events:none;}
.toast.show{opacity:1;}
.toast.err-toast{background:var(--red);color:#fff;}

/* MODAL */
.modal-ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.87);
  z-index:200;align-items:center;justify-content:center;}
.modal-ov.open{display:flex;}
.modal{background:var(--s1);border:1px solid var(--border);padding:28px;
  width:460px;max-width:95vw;position:relative;}
.modal-title{font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:1px;
  margin-bottom:20px;}
.modal-x{position:absolute;top:14px;right:16px;background:none;border:none;
  color:var(--muted);cursor:pointer;font-size:18px;}
.modal-x:hover{color:#fff;}

/* CACHE INFO */
.cache-info{font-size:10px;color:var(--muted);margin-top:4px;}
.cache-info.warn{color:var(--yellow);}

@media(max-width:900px){.cols{grid-template-columns:1fr;}}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-dot"></div>
    YT STREAM MANAGER
  </div>
  <div class="hdr-right">
    <div class="stat-pill" id="uptime-pill">uptime: <span id="sv-uptime">…</span></div>
    <div class="stat-pill">reqs: <span id="sv-req">…</span> | erros: <span id="sv-err">…</span></div>
    <a href="/panel/logout" class="logout">Sair</a>
  </div>
</header>

<div class="wrap">
  <!-- STATUS -->
  <div class="status-bar">
    <div class="status-item"><div class="status-label">Entradas</div><div class="status-val" id="sv-entries">—</div></div>
    <div class="status-item"><div class="status-label">Cache hits</div><div class="status-val" id="sv-hits">—</div></div>
    <div class="status-item"><div class="status-label">Eficiência</div><div class="status-val" id="sv-eff">—</div></div>
    <div class="status-item"><div class="status-label">Cache itens</div><div class="status-val" id="sv-cache">—</div></div>
  </div>

  <div class="cols">
    <!-- FORMULÁRIO ADD -->
    <div>
      <div class="card">
        <div class="card-hd">Adicionar Canal / Live</div>
        <div class="fg">
          <label class="fl">Nome / Descrição</label>
          <input type="text" class="fi" id="add-name" placeholder="Ex: SBT Brasil ao vivo">
        </div>
        <div class="fg">
          <label class="fl">URL do YouTube</label>
          <textarea class="fi" id="add-url" rows="3"
            placeholder="https://www.youtube.com/watch?v=...&#10;https://youtu.be/...&#10;https://www.youtube.com/live/..."></textarea>
        </div>
        <button class="btn btn-red btn-full" onclick="addEntry()">＋ ADICIONAR</button>
      </div>

      <div class="card" style="margin-top:16px;">
        <div class="card-hd">Como usar no VLC</div>
        <p style="line-height:1.9;color:var(--muted);font-size:11px;">
          Copie o <strong style="color:#fff">Link VLC</strong> de qualquer entrada.<br>
          No VLC: <strong style="color:#fff">Mídia → Abrir URL</strong><br><br>
          Links de <strong style="color:var(--green)">LIVE</strong> são renovados automaticamente a cada 15 min.<br>
          Use o botão <strong style="color:var(--yellow)">↺</strong> para forçar renovação manual.
        </p>
      </div>
    </div>

    <!-- TABELA -->
    <div class="card">
      <div class="card-hd">Entradas Cadastradas</div>
      <div id="entries-container">
        <div class="empty"><div class="empty-icon">📋</div>Carregando…</div>
      </div>
    </div>
  </div>
</div>

<!-- MODAL EDITAR -->
<div class="modal-ov" id="editModal">
  <div class="modal">
    <button class="modal-x" onclick="closeEdit()">✕</button>
    <div class="modal-title">✏ EDITAR ENTRADA</div>
    <input type="hidden" id="edit-eid">
    <div class="fg">
      <label class="fl">Nome</label>
      <input type="text" class="fi" id="edit-name">
    </div>
    <div class="fg">
      <label class="fl">Nova URL do YouTube</label>
      <textarea class="fi" id="edit-url" rows="3"></textarea>
    </div>
    <p style="font-size:10px;color:var(--muted);margin-bottom:14px;">
      ⚠ O link VLC permanecerá o mesmo após editar.
    </p>
    <button class="btn btn-red btn-full" onclick="saveEdit()">SALVAR</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── Utils ──────────────────────────────────────────────────────
function toast(msg, err=false){
  const t=document.getElementById('toast');
  t.textContent=msg; t.className='toast'+(err?' err-toast':'');
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2500);
}
async function api(path,method='GET',body=null){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body) opts.body=JSON.stringify(body);
  const r=await fetch(path,opts);
  return [r.ok, await r.json()];
}
function copyText(txt){
  navigator.clipboard.writeText(txt);
  toast('✓ Link copiado!');
}

// ── Status ─────────────────────────────────────────────────────
async function loadStatus(){
  const [ok,d]=await api('/status');
  if(!ok) return;
  document.getElementById('sv-uptime').textContent = d.uptime;
  document.getElementById('sv-req').textContent    = d.requisicoes;
  document.getElementById('sv-err').textContent    = d.erros;
  document.getElementById('sv-entries').textContent= d.entradas;
  document.getElementById('sv-hits').textContent   = d.cache_hits;
  document.getElementById('sv-eff').textContent    = d.eficiencia;
  document.getElementById('sv-cache').textContent  = d.cache_itens;
}

// ── Entradas ───────────────────────────────────────────────────
async function loadEntries(){
  const [ok,data]=await api('/panel/api/entries');
  if(!ok){document.getElementById('entries-container').innerHTML='<div class="empty"><div class="empty-icon">⚠</div>Erro ao carregar</div>';return;}
  if(!data.length){
    document.getElementById('entries-container').innerHTML='<div class="empty"><div class="empty-icon">📋</div>Nenhum link cadastrado.<br>Adicione um ao lado.</div>';
    return;
  }
  let rows=data.map(e=>{
    const liveHtml = e.is_live===true
      ? '<span class="live-badge live">● LIVE</span>'
      : e.is_live===false
        ? '<span class="live-badge vod">VOD</span>'
        : '<span class="live-badge unknown">—</span>';
    const cacheInfo = e.cache_age!==null
      ? `<div class="cache-info ${e.cache_age>750?'warn':''}">cache: ${e.cache_age}s atrás ${e.cache_title?'· '+e.cache_title.substring(0,30):''}</div>`
      : '';
    return `<tr>
      <td><span class="id-tag">#${e.id%10000}</span></td>
      <td>
        <div class="entry-name">${esc(e.name||e.video_id)}</div>
        <div class="entry-vid">${e.video_id}</div>
        <div style="margin-top:5px;">${liveHtml}</div>
        ${cacheInfo}
      </td>
      <td>
        <div class="vlc-link" onclick="copyText('${esc(e.vlc_link)}')">${esc(e.vlc_link)}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:3px;">clique para copiar</div>
      </td>
      <td><span class="date">${e.added_at}</span></td>
      <td>
        <div class="actions">
          <button class="btn btn-sm btn-green" onclick="refreshEntry('${e.video_id}')" title="Renovar URL">↺</button>
          <button class="btn btn-sm btn-warn" onclick="openEdit(${e.id},'${esc(e.name)}','${esc(e.url)}')">✏</button>
          <button class="btn btn-sm btn-danger" onclick="delEntry(${e.id})">✕</button>
        </div>
      </td>
    </tr>`;
  }).join('');
  document.getElementById('entries-container').innerHTML=
    `<div style="overflow-x:auto"><table class="tbl">
      <thead><tr><th>#</th><th>Nome / Tipo</th><th>Link VLC</th><th>Adicionado</th><th>Ações</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

// ── CRUD ────────────────────────────────────────────────────────
async function addEntry(){
  const name=document.getElementById('add-name').value.trim();
  const url=document.getElementById('add-url').value.trim();
  if(!url){toast('Cole a URL do YouTube',true);return;}
  const [ok,d]=await api('/panel/api/entries','POST',{name,url});
  if(ok){
    toast('✓ Adicionado! Título sendo carregado…');
    document.getElementById('add-name').value='';
    document.getElementById('add-url').value='';
    loadEntries(); loadStatus();
  } else {
    toast(d.erro||'Erro ao adicionar',true);
  }
}

async function delEntry(id){
  if(!confirm('Remover esta entrada?')) return;
  const [ok,d]=await api(`/panel/api/entries/${id}`,'DELETE');
  if(ok){toast('🗑 Removido');loadEntries();loadStatus();}
  else toast(d.erro||'Erro',true);
}

async function refreshEntry(vid){
  toast(`↺ Renovando ${vid}…`);
  const [ok,d]=await api(`/panel/api/refresh/${vid}`,'POST');
  if(ok) toast(`✓ Renovado · ${d.is_live?'LIVE':'VOD'} · ${(d.title||'').substring(0,30)}`);
  else toast(d.erro||'Erro ao renovar',true);
  loadEntries();
}

function openEdit(id,name,url){
  document.getElementById('edit-eid').value=id;
  document.getElementById('edit-name').value=name;
  document.getElementById('edit-url').value=url;
  document.getElementById('editModal').classList.add('open');
}
function closeEdit(){document.getElementById('editModal').classList.remove('open');}
document.getElementById('editModal').addEventListener('click',function(e){if(e.target===this)closeEdit();});

async function saveEdit(){
  const id=document.getElementById('edit-eid').value;
  const name=document.getElementById('edit-name').value.trim();
  const url=document.getElementById('edit-url').value.trim();
  const [ok,d]=await api(`/panel/api/entries/${id}`,'PUT',{name,url});
  if(ok){toast('✓ Salvo');closeEdit();loadEntries();}
  else toast(d.erro||'Erro',true);
}

// ── Poll ────────────────────────────────────────────────────────
loadEntries(); loadStatus();
setInterval(()=>{loadEntries();loadStatus();}, 30000);
</script>
</body>
</html>"""

# ================================================================
#  MAIN
# ================================================================
if __name__ == "__main__":
    load_entries()
    print("""
╔══════════════════════════════════════════════╗
║   YouTube Stream Manager v2.0               ║
╠══════════════════════════════════════════════╣
║  Stream:  http://SEU_IP:{port}/{video_id}    ║
║  Painel:  http://SEU_IP:{port}/panel         ║
║  Status:  http://SEU_IP:{port}/status        ║
╚══════════════════════════════════════════════╝
""".replace("{port}", str(PORT)))
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
