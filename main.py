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

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "downloads"
TEMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="VidSave - YouTube to MP4 Converter")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ─── ffmpeg config ───────────────────────────────────────────────────────────
# If ffmpeg is in your PATH leave as None.
# Otherwise set full path: r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_MANUAL_PATH = r"C:\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe"

def _find_ffmpeg_dir() -> str | None:
    if FFMPEG_MANUAL_PATH:
        p = Path(FFMPEG_MANUAL_PATH)
        if p.exists():
            return str(p.parent)
    which = shutil.which("ffmpeg")
    return str(Path(which).parent) if which else None

FFMPEG_DIR = _find_ffmpeg_dir()
print(f"[VidSave] ffmpeg: {'FOUND at ' + FFMPEG_DIR if FFMPEG_DIR else 'NOT FOUND — only pre-muxed streams available'}")
# ─────────────────────────────────────────────────────────────────────────────


class ConvertRequest(BaseModel):
    url: str
    format: str = "mp4"

class DownloadRequest(BaseModel):
    url: str
    quality: str
    format: str = "mp4"


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

def base_opts() -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if FFMPEG_DIR:
        o["ffmpeg_location"] = FFMPEG_DIR
    return o


# ─── format selection ────────────────────────────────────────────────────────

def _classify(formats: list) -> tuple[list, list, list]:
    """Split formats into video-only, audio-only, muxed."""
    video, audio, muxed = [], [], []
    for f in formats:
        vco = (f.get("vcodec") or "none").lower()
        aco = (f.get("acodec") or "none").lower()
        h   = f.get("height") or 0
        has_v = vco != "none"
        has_a = aco != "none"
        if has_v and not has_a and h > 0:
            video.append(f)
        elif has_a and not has_v:
            audio.append(f)
        elif has_v and has_a and h > 0:
            muxed.append(f)
    return video, audio, muxed


def _best_video_at(video_fmts: list, target: int) -> dict | None:
    """
    Pick the video-only format whose height is the CLOSEST to target
    from below (never above). Among ties, highest bitrate wins.
    Prefer mp4/avc1 for compatibility.
    """
    cands = [f for f in video_fmts if (f.get("height") or 0) <= target]
    if not cands:
        return None
    max_h = max(f["height"] for f in cands)
    # Only keep formats AT the closest height (not everything below)
    at_h  = [f for f in cands if f["height"] == max_h]
    # Prefer mp4 container
    mp4   = [f for f in at_h if f.get("ext") == "mp4"]
    pool  = mp4 if mp4 else at_h
    return sorted(pool, key=lambda f: f.get("tbr") or f.get("vbr") or 0, reverse=True)[0]


def _best_audio(audio_fmts: list) -> dict | None:
    if not audio_fmts:
        return None
    # Prefer m4a (compatible with mp4 container)
    m4a  = [f for f in audio_fmts if f.get("ext") == "m4a"]
    pool = m4a if m4a else audio_fmts
    return sorted(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)[0]


def _best_muxed_at(muxed_fmts: list, target: int) -> dict | None:
    cands = [f for f in muxed_fmts if (f.get("height") or 0) <= target]
    if not cands:
        return None
    max_h = max(f["height"] for f in cands)
    at_h  = [f for f in cands if f["height"] == max_h]
    return sorted(at_h, key=lambda f: f.get("tbr") or 0, reverse=True)[0]


def _select_format_id(all_formats: list, target: int, container: str) -> tuple[str, int, bool]:
    """
    Returns (format_id_string, actual_height, needs_ffmpeg).
    Raises ValueError if no suitable format found.
    """
    video, audio, muxed = _classify(all_formats)

    if FFMPEG_DIR:
        # With ffmpeg: pick exact video-only + audio-only, merge them
        best_v = _best_video_at(video, target)
        best_a = _best_audio(audio)

        if best_v and best_a:
            fmt_str = f"{best_v['format_id']}+{best_a['format_id']}"
            actual  = best_v["height"]
            print(f"[VidSave] MERGE {fmt_str}  "
                  f"(video={best_v['height']}p ext={best_v.get('ext')} "
                  f"tbr={best_v.get('tbr'):.0f}, "
                  f"audio ext={best_a.get('ext')} "
                  f"abr={best_a.get('abr') or best_a.get('tbr'):.0f})")
            return fmt_str, actual, False

        if best_v:
            print(f"[VidSave] VIDEO-ONLY {best_v['format_id']} ({best_v['height']}p)")
            return best_v["format_id"], best_v["height"], False

    # No ffmpeg OR no video-only streams: must use pre-muxed
    best_m = _best_muxed_at(muxed, target)
    if best_m:
        actual = best_m["height"]
        print(f"[VidSave] MUXED {best_m['format_id']} ({actual}p)")
        return best_m["format_id"], actual, False

    raise ValueError(
        f"No suitable format found for {target}p. "
        "Install ffmpeg to unlock HD downloads."
    )


# ─── yt-dlp workers ──────────────────────────────────────────────────────────

def _fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    all_fmts         = info.get("formats", [])
    video, audio, mx = _classify(all_fmts)

    available_heights = sorted(
        set(f["height"] for f in video),
        reverse=True
    )
    muxed_heights = sorted(
        set(f["height"] for f in mx),
        reverse=True
    )
    print(f"[VidSave] Video-only heights: {available_heights}")
    print(f"[VidSave] Muxed heights:      {muxed_heights}")
    print(f"[VidSave] ffmpeg available:   {bool(FFMPEG_DIR)}")

    qualities = []
    for res, height in QUALITY_HEIGHT.items():
        meta = QUALITY_META[res]

        # Check availability
        has_video_at  = any(f["height"] <= height for f in video)
        has_muxed_at  = any((f.get("height") or 0) <= height for f in mx)
        is_available  = (FFMPEG_DIR and has_video_at) or has_muxed_at

        # Get file size estimate from the best video format at this height
        best_v = _best_video_at(video, height) if FFMPEG_DIR else None
        best_m = _best_muxed_at(mx, height)
        ref    = best_v or best_m
        size   = format_filesize(
            ref.get("filesize") or ref.get("filesize_approx")
        ) if ref else "—"

        qualities.append({
            "res":          res,
            "label":        meta["label"],
            "size":         size,
            "badge":        meta["badge"],
            "badge_class":  meta["badge_class"],
            "available":    is_available,
            "needs_ffmpeg": not is_available and has_video_at,
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

    print(f"\n[VidSave] ── Download: {quality} ({target}p) as {fmt} ──")

    # Phase 1: fetch format list
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    all_fmts = info.get("formats", [])
    title    = info.get("title", "video")

    # Phase 2: build download opts
    dl = base_opts()
    dl["outtmpl"] = str(out_dir / "%(title)s.%(ext)s")

    if fmt == "mp3":
        _, audio, _ = _classify(all_fmts)
        best_a      = _best_audio(audio)
        dl["format"] = best_a["format_id"] if best_a else "bestaudio/best"
        if FFMPEG_DIR:
            dl["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
    else:
        fmt_str, actual_h, _ = _select_format_id(all_fmts, target, fmt)
        dl["format"] = fmt_str
        print(f"[VidSave] Using format_id='{fmt_str}'  actual={actual_h}p")

        if FFMPEG_DIR and "+" in fmt_str:
            dl["merge_output_format"] = fmt

    # Phase 3: download
    with yt_dlp.YoutubeDL(dl) as ydl:
        ydl.extract_info(url, download=True)

    # Phase 4: locate file
    files = [
        f for f in sorted(out_dir.iterdir())
        if f.suffix not in (".part", ".ytdl", ".temp")
    ]
    if not files:
        files = sorted(out_dir.iterdir())
    if not files:
        raise RuntimeError("yt-dlp produced no output file.")

    final = files[0]
    print(f"[VidSave] Saved: {final.name}  ({final.stat().st_size // 1024} KB)\n")
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