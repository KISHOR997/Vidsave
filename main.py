from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pathlib import Path
import yt_dlp
import re
import asyncio
import uuid
import shutil
import os
import tempfile

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path("/tmp/vidssave")
TEMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="VidSave")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Set manually if ffmpeg not in PATH:
# FFMPEG_MANUAL_PATH = r"C:\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe"
FFMPEG_MANUAL_PATH = None

def _find_ffmpeg():
    if FFMPEG_MANUAL_PATH:
        p = Path(FFMPEG_MANUAL_PATH)
        if p.exists():
            return str(p.parent)
    w = shutil.which("ffmpeg")
    return str(Path(w).parent) if w else None

FFMPEG_DIR = _find_ffmpeg()
print(f"[VidSave] ffmpeg: {'FOUND → ' + FFMPEG_DIR if FFMPEG_DIR else 'NOT FOUND'}")
print(f"[VidSave] yt-dlp version: {yt_dlp.version.__version__}")


# ---------- models ----------

class ConvertRequest(BaseModel):
    url: str
    format: str = "mp4"

class DownloadRequest(BaseModel):
    url: str
    quality: str
    format: str = "mp4"


# ---------- constants ----------

FORMATS = ["mp4", "mp3", "webm", "avi"]
YT_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+")

QUALITY_HEIGHT = {
    "2160p": 2160, "1080p": 1080,
    "720p":  720,  "480p":  480, "360p": 360,
}
QUALITY_META = {
    "2160p": {"label": "Ultra HD · 4K", "badge": "4K",  "badge_class": "uhd"},
    "1080p": {"label": "Full HD",        "badge": "FHD", "badge_class": "fhd"},
    "720p":  {"label": "HD",             "badge": "HD",  "badge_class": "hd"},
    "480p":  {"label": "Standard",       "badge": "SD",  "badge_class": "sd"},
    "360p":  {"label": "Mobile",         "badge": "SD",  "badge_class": "sd"},
}


# ---------- helpers ----------

def fmt_views(n):
    if not n: return "—"
    if n >= 1e9: return f"{n/1e9:.1f}B views"
    if n >= 1e6: return f"{n/1e6:.1f}M views"
    if n >= 1e3: return f"{n/1e3:.1f}K views"
    return f"{n} views"

def fmt_dur(s):
    if not s: return "—"
    m, s = divmod(int(s), 60); h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def fmt_size(b):
    if not b: return "—"
    mb = b / 1048576
    return f"~{mb/1024:.1f} GB" if mb >= 1024 else f"~{int(mb)} MB"

def safe_name(t):
    return re.sub(r'[\\/*?:"<>|]', "", t).strip()[:80]


# ---------- cookies ----------

_cookie_tmp = None

def cookie_file():
    global _cookie_tmp
    local = BASE_DIR / "cookies.txt"
    if local.exists():
        return str(local)
    env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if env:
        if _cookie_tmp and Path(_cookie_tmp).exists():
            return _cookie_tmp
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(env); f.flush(); f.close()
        _cookie_tmp = f.name
        return _cookie_tmp
    return None


# ---------- yt-dlp opts ----------

def base_opts(client: str = "ios") -> dict:
    o = {
        "quiet":        True,
        "no_warnings":  True,
        "noplaylist":   True,
        "extractor_args": {
            "youtube": {
                "player_client": [client],
            }
        },
    }
    if FFMPEG_DIR:
        o["ffmpeg_location"] = FFMPEG_DIR
    cf = cookie_file()
    if cf:
        o["cookiefile"] = cf
    return o


def _find_node() -> str | None:
    """Find node.js binary — needed by yt-dlp for JS extraction."""
    for candidate in ["node", "nodejs"]:
        p = shutil.which(candidate)
        if p:
            return p
    for path in ["/usr/bin/node", "/usr/local/bin/node",
                 "/usr/bin/nodejs", "/usr/local/bin/nodejs"]:
        if Path(path).exists():
            return path
    return None

NODE_PATH = _find_node()
print(f"[VidSave] node.js: {NODE_PATH or 'NOT FOUND'}")


def yt_extract(url: str, download: bool, extra_opts: dict = None) -> dict:
    """
    Try multiple YouTube player clients in order until one works.
    Clients that don't need JS (mweb, ios) are tried first.
    """
    # mweb and ios don't require JS runtime — try them first
    # web_creator, android_vr also bypass JS in most cases
    clients = [
        "mweb",          # mobile web — most reliable on cloud IPs
        "ios",           # iOS app client
        "android",       # Android app client
        "android_vr",    # Android VR — different format list
        "web_creator",   # YouTube Studio client
        "tv_embedded",   # TV embedded player
        "web",           # standard web (needs JS)
    ]
    last_err = None

    for client in clients:
        opts = base_opts(client)

        # Pass node.js path if found — helps web client
        if NODE_PATH:
            opts["extractor_args"]["youtube"]["js_player_path"] = NODE_PATH

        if extra_opts:
            opts.update(extra_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            if info:
                print(f"[VidSave] success with client={client}")
                return info
        except yt_dlp.utils.DownloadError as e:
            print(f"[VidSave] client={client} failed: {e}")
            last_err = e
            continue
        except Exception as e:
            print(f"[VidSave] client={client} exception: {e}")
            last_err = yt_dlp.utils.DownloadError(str(e))
            continue

    raise last_err


# ---------- workers ----------

def _fetch_info(url: str) -> dict:
    info = yt_extract(url, download=False)

    fmts = info.get("formats", [])

    # All heights that have a video stream
    v_heights = sorted(set(
        f["height"] for f in fmts
        if f.get("height") and (f.get("vcodec") or "none") != "none"
    ), reverse=True)

    # Heights that are pre-muxed (video+audio, no ffmpeg needed)
    m_heights = sorted(set(
        f["height"] for f in fmts
        if f.get("height")
        and (f.get("vcodec") or "none") != "none"
        and (f.get("acodec") or "none") != "none"
    ), reverse=True)

    print(f"[VidSave] video heights : {v_heights}")
    print(f"[VidSave] muxed heights : {m_heights}")

    qualities = []
    for res, h in QUALITY_HEIGHT.items():
        meta      = QUALITY_META[res]
        has_v     = any(x <= h for x in v_heights)
        has_m     = any(x <= h for x in m_heights)
        available = (FFMPEG_DIR and has_v) or has_m

        ref = next((
            f for f in sorted(fmts, key=lambda x: x.get("height") or 0, reverse=True)
            if (f.get("height") or 0) <= h and (f.get("vcodec") or "none") != "none"
        ), None)

        qualities.append({
            "res": res, "label": meta["label"],
            "size": fmt_size(ref.get("filesize") or ref.get("filesize_approx")) if ref else "—",
            "badge": meta["badge"], "badge_class": meta["badge_class"],
            "available": available, "needs_ffmpeg": not available and has_v,
        })

    return {
        "video": {
            "title":     info.get("title", "Unknown"),
            "channel":   info.get("uploader") or info.get("channel", "?"),
            "duration":  fmt_dur(info.get("duration")),
            "views":     fmt_views(info.get("view_count")),
            "thumbnail": info.get("thumbnail", ""),
            "video_id":  info.get("id", ""),
        },
        "qualities":    qualities,
        "ffmpeg_ready": FFMPEG_DIR is not None,
    }


def _download_file(url: str, quality: str, fmt: str):
    target  = QUALITY_HEIGHT.get(quality, 720)
    out_dir = TEMP_DIR / uuid.uuid4().hex
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[VidSave] ── download {quality} ({target}p) as {fmt} ──")

    dl           = base_opts()
    dl["outtmpl"] = str(out_dir / "%(title)s.%(ext)s")

    if fmt == "mp3":
        dl["format"]      = "bestaudio/best"
        dl["format_sort"] = ["abr"]
        if FFMPEG_DIR:
            dl["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

    else:
        # Use "b" (best single pre-muxed stream) — ALWAYS available on any IP.
        # format_sort controls which resolution gets picked.
        # No merging, no filters = no "format not available" errors.
        dl["format"]      = "b"
        dl["format_sort"] = [
            f"res:{target}",  # closest to requested height wins
            "fps",
            "+size",
        ]

    print(f"[VidSave] format      = {dl['format']}")
    print(f"[VidSave] format_sort = {dl.get('format_sort')}")

    # Pass dl opts as extra — yt_extract will try multiple clients
    # Remove keys already in base_opts to avoid duplication
    extra = {k: v for k, v in dl.items()
             if k not in ("quiet","no_warnings","noplaylist",
                          "ffmpeg_location","cookiefile","extractor_args")}
    info = yt_extract(url, download=True, extra_opts=extra)

    title = (info or {}).get("title", "video")
    files = [f for f in sorted(out_dir.iterdir())
             if f.suffix not in (".part", ".ytdl", ".temp")]
    if not files:
        files = sorted(out_dir.iterdir())
    if not files:
        raise RuntimeError("No output file produced.")

    final = files[0]
    print(f"[VidSave] output: {final.name} ({final.stat().st_size // 1024} KB)")
    return final, title


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request, "formats": FORMATS,
        "ffmpeg_ready": FFMPEG_DIR is not None,
    })

@app.post("/api/convert")
async def convert(payload: ConvertRequest):
    url = payload.url.strip()
    if not url:
        raise HTTPException(400, "URL is required.")
    if not YT_REGEX.search(url):
        raise HTTPException(422, "Please enter a valid YouTube URL.")
    if payload.format not in FORMATS:
        raise HTTPException(422, f"Unsupported format: {payload.format}")
    try:
        data = await asyncio.to_thread(_fetch_info, url)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        print(f"[VidSave] convert error: {msg}")
        # Give user a friendly message
        if "Sign in" in msg or "bot" in msg.lower():
            raise HTTPException(422, "YouTube blocked this request. Try again in a moment.")
        if "not available" in msg:
            raise HTTPException(422, "This video is not available for download.")
        if "Private" in msg or "private" in msg:
            raise HTTPException(422, "This video is private.")
        raise HTTPException(422, f"Could not fetch video: {msg}")
    except Exception as e:
        print(f"[VidSave] convert exception: {e}")
        raise HTTPException(500, str(e))
    return JSONResponse({"success": True, **data, "format": payload.format})

@app.post("/api/download")
async def download(payload: DownloadRequest):
    url = payload.url.strip()
    if not YT_REGEX.search(url):
        raise HTTPException(422, "Invalid YouTube URL.")
    if payload.quality not in QUALITY_HEIGHT:
        raise HTTPException(422, f"Invalid quality: {payload.quality}")
    if payload.format not in FORMATS:
        raise HTTPException(422, f"Invalid format: {payload.format}")
    try:
        file_path, title = await asyncio.to_thread(
            _download_file, url, payload.quality, payload.format
        )
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, f"yt-dlp: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))

    fname = safe_name(title) + file_path.suffix
    return FileResponse(
        path=str(file_path),
        filename=fname,
        media_type="application/octet-stream",
    )

@app.get("/health")
async def health():
    return {"status": "ok", "ffmpeg": FFMPEG_DIR is not None,
            "yt_dlp": yt_dlp.version.__version__}