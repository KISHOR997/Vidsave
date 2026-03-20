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
TEMP_DIR = Path("/tmp/vidssave_downloads")
TEMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="VidSave - YouTube to MP4 Converter")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ─── ffmpeg ──────────────────────────────────────────────────────────────────
FFMPEG_MANUAL_PATH = None  # e.g. r"C:\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe"

def _find_ffmpeg_dir():
    if FFMPEG_MANUAL_PATH:
        p = Path(FFMPEG_MANUAL_PATH)
        if p.exists():
            return str(p.parent)
    which = shutil.which("ffmpeg")
    return str(Path(which).parent) if which else None

FFMPEG_DIR = _find_ffmpeg_dir()
print(f"[VidSave] ffmpeg: {'FOUND at ' + FFMPEG_DIR if FFMPEG_DIR else 'NOT FOUND'}")


# ─── models ──────────────────────────────────────────────────────────────────

class ConvertRequest(BaseModel):
    url: str
    format: str = "mp4"

class DownloadRequest(BaseModel):
    url: str
    quality: str
    format: str = "mp4"


# ─── constants ───────────────────────────────────────────────────────────────

FORMATS = ["mp4", "mp3", "webm", "avi"]

YT_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+"
)

QUALITY_HEIGHT = {
    "2160p": 2160,
    "1080p": 1080,
    "720p":  720,
    "480p":  480,
    "360p":  360,
}

QUALITY_META = {
    "2160p": {"label": "Ultra HD · 4K", "badge": "4K",  "badge_class": "uhd"},
    "1080p": {"label": "Full HD",        "badge": "FHD", "badge_class": "fhd"},
    "720p":  {"label": "HD",             "badge": "HD",  "badge_class": "hd"},
    "480p":  {"label": "Standard",       "badge": "SD",  "badge_class": "sd"},
    "360p":  {"label": "Mobile",         "badge": "SD",  "badge_class": "sd"},
}


# ─── helpers ─────────────────────────────────────────────────────────────────

def format_views(n):
    if n is None: return "—"
    if n >= 1e9: return f"{n/1e9:.1f}B views"
    if n >= 1e6: return f"{n/1e6:.1f}M views"
    if n >= 1e3: return f"{n/1e3:.1f}K views"
    return f"{n} views"

def format_duration(s):
    if not s: return "—"
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def format_filesize(b):
    if not b: return "—"
    mb = b / 1048576
    return f"~{mb/1024:.1f} GB" if mb >= 1024 else f"~{int(mb)} MB"

def safe_name(t: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", t).strip()[:80]


# ─── cookies ─────────────────────────────────────────────────────────────────

_cookie_tmp_path = None  # module-level cache

def _get_cookie_file() -> str | None:
    global _cookie_tmp_path

    # 1. Local cookies.txt file
    local = BASE_DIR / "cookies.txt"
    if local.exists():
        return str(local)

    # 2. Environment variable (Render deployment)
    cookie_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookie_content:
        if _cookie_tmp_path and Path(_cookie_tmp_path).exists():
            return _cookie_tmp_path
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        )
        tmp.write(cookie_content)
        tmp.flush()
        tmp.close()
        _cookie_tmp_path = tmp.name
        print(f"[VidSave] cookies loaded from env → {_cookie_tmp_path}")
        return _cookie_tmp_path

    return None


def base_opts() -> dict:
    o = {
        "quiet":       True,
        "no_warnings": True,
        "noplaylist":  True,
    }
    if FFMPEG_DIR:
        o["ffmpeg_location"] = FFMPEG_DIR

    cookie_file = _get_cookie_file()
    if cookie_file:
        o["cookiefile"] = cookie_file

    return o


# ─── format string builder (single yt-dlp call, no format_id picking) ────────

def _build_format(height: int, container: str, has_ffmpeg: bool) -> str:
    if container == "mp3":
        return "bestaudio/best"

    if has_ffmpeg:
        # No ext filters — let format_sort pick the best codec available
        # on this server. Removing [ext=mp4] prevents "format not available"
        # errors when YouTube only serves webm/vp9 to certain IPs.
        return (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]"
        )
    else:
        return (
            f"best[height<={height}][vcodec!=none][acodec!=none]/"
            f"best[height<={height}]"
        )


# ─── yt-dlp workers ──────────────────────────────────────────────────────────

def _fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    all_fmts = info.get("formats", [])

    # Heights with a real video stream
    video_heights = sorted(set(
        f["height"] for f in all_fmts
        if (f.get("vcodec") or "none") != "none"
        and f.get("height")
    ), reverse=True)

    # Heights with pre-muxed (video+audio)
    muxed_heights = sorted(set(
        f["height"] for f in all_fmts
        if (f.get("vcodec") or "none") != "none"
        and (f.get("acodec") or "none") != "none"
        and f.get("height")
    ), reverse=True)

    print(f"[VidSave] video heights: {video_heights}")
    print(f"[VidSave] muxed heights: {muxed_heights}")

    qualities = []
    for res, height in QUALITY_HEIGHT.items():
        meta = QUALITY_META[res]

        has_video = any(h <= height for h in video_heights)
        has_muxed = any(h <= height for h in muxed_heights)
        available = (FFMPEG_DIR and has_video) or has_muxed

        # Best size estimate
        cands = [
            f for f in all_fmts
            if (f.get("height") or 0) <= height
            and (f.get("vcodec") or "none") != "none"
        ]
        cands.sort(key=lambda f: f.get("height") or 0, reverse=True)
        ref      = cands[0] if cands else None
        size_str = format_filesize(
            ref.get("filesize") or ref.get("filesize_approx")
        ) if ref else "—"

        qualities.append({
            "res":          res,
            "label":        meta["label"],
            "size":         size_str,
            "badge":        meta["badge"],
            "badge_class":  meta["badge_class"],
            "available":    available,
            "needs_ffmpeg": not available and has_video,
        })

    return {
        "video": {
            "title":     info.get("title", "Unknown"),
            "channel":   info.get("uploader") or info.get("channel", "Unknown"),
            "duration":  format_duration(info.get("duration")),
            "views":     format_views(info.get("view_count")),
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

    print(f"\n[VidSave] Download: {quality} ({target}p) as {fmt}")

    dl = base_opts()
    dl["outtmpl"] = str(out_dir / "%(title)s.%(ext)s")

    if fmt == "mp3":
        dl["format"] = "bestaudio/best"
        if FFMPEG_DIR:
            dl["postprocessors"] = [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "192",
            }]
    else:
        dl["format"] = _build_format(target, fmt, bool(FFMPEG_DIR))

        # format_sort pins resolution — this is the key to getting the RIGHT quality
        # res:N = rank formats closest to N first (not just "anything below N")
        dl["format_sort"] = [f"res:{target}"]

        if FFMPEG_DIR:
            dl["merge_output_format"] = fmt

    print(f"[VidSave] format='{dl['format']}'  format_sort={dl.get('format_sort')}")

    # Single call — fetch info + download in one shot (avoids format_id mismatch)
    with yt_dlp.YoutubeDL(dl) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title", "video")

    files = [
        f for f in sorted(out_dir.iterdir())
        if f.suffix not in (".part", ".ytdl", ".temp")
    ]
    if not files:
        files = sorted(out_dir.iterdir())
    if not files:
        raise RuntimeError("yt-dlp produced no output file.")

    final = files[0]
    print(f"[VidSave] Saved: {final.name}  ({final.stat().st_size // 1024} KB)")
    return final, title


# ─── routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request":      request,
        "formats":      FORMATS,
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
        raise HTTPException(422, f"yt-dlp: {e}")
    except Exception as e:
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
    except ValueError as e:
        raise HTTPException(422, str(e))
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
    return {"status": "ok", "ffmpeg": FFMPEG_DIR is not None}