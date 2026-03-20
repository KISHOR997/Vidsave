from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pathlib import Path
import re
import asyncio
import uuid
import httpx
import os

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path("/tmp/vidssave")
TEMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="VidSave")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── RapidAPI config ───────────────────────────────────────────────────────────
# Get your free key at https://rapidapi.com/ytjar/api/yt-api
# Add RAPIDAPI_KEY to Render environment variables
RAPIDAPI_KEY  = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "yt-api.p.rapidapi.com"
RAPIDAPI_BASE = "https://yt-api.p.rapidapi.com"
# ─────────────────────────────────────────────────────────────────────────────


class ConvertRequest(BaseModel):
    url: str
    format: str = "mp4"

class DownloadRequest(BaseModel):
    url: str
    quality: str
    format: str = "mp4"


FORMATS = ["mp4", "mp3"]

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


def fmt_views(n):
    if not n: return "—"
    if n >= 1e9: return f"{n/1e9:.1f}B views"
    if n >= 1e6: return f"{n/1e6:.1f}M views"
    if n >= 1e3: return f"{n/1e3:.1f}K views"
    return f"{n} views"

def fmt_dur(s):
    if not s: return "—"
    try:
        s = int(s)
        m, s = divmod(s, 60); h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except:
        return str(s)

def safe_name(t: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", t).strip()[:80]

def extract_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([\w\-]{11})", url)
    return m.group(1) if m else ""

def rapidapi_headers() -> dict:
    return {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }


# ── RapidAPI workers ──────────────────────────────────────────────────────────

async def _fetch_info(url: str) -> dict:
    vid_id = extract_id(url)
    if not vid_id:
        raise ValueError("Invalid YouTube URL")

    async with httpx.AsyncClient(timeout=30) as client:
        # Get video info + formats
        res = await client.get(
            f"{RAPIDAPI_BASE}/info",
            params={"id": vid_id},
            headers=rapidapi_headers(),
        )

    if res.status_code != 200:
        print(f"[VidSave] RapidAPI info error: {res.status_code} {res.text[:200]}")
        raise ValueError(f"API error {res.status_code}: {res.text[:100]}")

    data = res.json()
    print(f"[VidSave] info fetched: {data.get('title', '?')[:50]}")

    # Extract available formats
    formats     = data.get("formats", [])
    vid_formats = [f for f in formats if f.get("qualityLabel")]

    # Build quality list
    qualities = []
    for res_label, height in QUALITY_HEIGHT.items():
        meta = QUALITY_META[res_label]
        # Find a matching format
        match = next(
            (f for f in vid_formats
             if str(height) in str(f.get("qualityLabel", ""))),
            None
        )
        qualities.append({
            "res":         res_label,
            "label":       meta["label"],
            "size":        "—",
            "badge":       meta["badge"],
            "badge_class": meta["badge_class"],
            "available":   match is not None,
            "needs_ffmpeg": False,
        })

    return {
        "video": {
            "title":     data.get("title", "Unknown"),
            "channel":   data.get("channelTitle", "YouTube"),
            "duration":  fmt_dur(data.get("lengthSeconds")),
            "views":     fmt_views(data.get("viewCount")),
            "thumbnail": data.get("thumbnail", [{}])[-1].get("url", "")
                         if isinstance(data.get("thumbnail"), list)
                         else f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg",
            "video_id":  vid_id,
        },
        "qualities":    qualities,
        "ffmpeg_ready": True,
    }


async def _download_file(url: str, quality: str, fmt: str):
    vid_id  = extract_id(url)
    target  = QUALITY_HEIGHT.get(quality, 720)
    out_dir = TEMP_DIR / uuid.uuid4().hex
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[VidSave] download {quality} ({target}p) as {fmt}")

    async with httpx.AsyncClient(timeout=30) as client:
        if fmt == "mp3":
            res = await client.get(
                f"{RAPIDAPI_BASE}/dl",
                params={"id": vid_id, "cgeo": "US"},
                headers=rapidapi_headers(),
            )
        else:
            res = await client.get(
                f"{RAPIDAPI_BASE}/dl",
                params={"id": vid_id, "cgeo": "US"},
                headers=rapidapi_headers(),
            )

    if res.status_code != 200:
        raise ValueError(f"API error {res.status_code}")

    data = res.json()
    print(f"[VidSave] dl response status={data.get('status')}")

    # Find the right format URL
    formats     = data.get("formats", [])
    download_url = None
    title        = data.get("title", "video")

    if fmt == "mp3":
        # Get audio format
        audio_fmts = [f for f in formats if f.get("hasAudio") and not f.get("hasVideo")]
        if audio_fmts:
            download_url = audio_fmts[0].get("url")
        else:
            # fallback: adaptiveFormats audio
            adaptive = data.get("adaptiveFormats", [])
            audio    = [f for f in adaptive if "audio" in f.get("mimeType", "")]
            if audio:
                download_url = audio[0].get("url")
    else:
        # Find video format closest to target quality
        video_fmts = [
            f for f in formats
            if f.get("qualityLabel") and f.get("url")
        ]
        # Sort by closeness to target
        def quality_diff(f):
            label = f.get("qualityLabel", "0p")
            try:
                h = int(re.search(r"\d+", label).group())
                return abs(h - target)
            except:
                return 9999

        video_fmts.sort(key=quality_diff)
        if video_fmts:
            download_url = video_fmts[0].get("url")
            print(f"[VidSave] picked format: {video_fmts[0].get('qualityLabel')}")

    if not download_url:
        # last resort: use the direct url from response
        download_url = data.get("url")

    if not download_url:
        raise ValueError("No download URL found in API response")

    # Stream file to disk
    ext      = "mp3" if fmt == "mp3" else "mp4"
    out_file = out_dir / f"{safe_name(title)}.{ext}"

    print(f"[VidSave] streaming → {out_file.name}")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=30),
        follow_redirects=True
    ) as client:
        async with client.stream("GET", download_url) as r:
            r.raise_for_status()
            with open(out_file, "wb") as f:
                async for chunk in r.aiter_bytes(512 * 1024):
                    f.write(chunk)

    size_kb = out_file.stat().st_size // 1024
    print(f"[VidSave] saved: {out_file.name} ({size_kb} KB)")
    return out_file, title


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    has_key = bool(RAPIDAPI_KEY)
    return templates.TemplateResponse("index.html", {
        "request":      request,
        "formats":      FORMATS,
        "ffmpeg_ready": True,
        "has_api_key":  has_key,
    })


@app.post("/api/convert")
async def convert(payload: ConvertRequest):
    if not RAPIDAPI_KEY:
        raise HTTPException(500, "RAPIDAPI_KEY not configured. Add it to environment variables.")
    url = payload.url.strip()
    if not url:
        raise HTTPException(400, "URL is required.")
    if not YT_REGEX.search(url):
        raise HTTPException(422, "Please enter a valid YouTube URL.")

    try:
        data = await _fetch_info(url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except httpx.TimeoutException:
        raise HTTPException(422, "Request timed out. Try again.")
    except Exception as e:
        print(f"[VidSave] convert error: {e}")
        raise HTTPException(500, str(e))

    return JSONResponse({"success": True, **data, "format": payload.format})


@app.post("/api/download")
async def download(payload: DownloadRequest):
    if not RAPIDAPI_KEY:
        raise HTTPException(500, "RAPIDAPI_KEY not configured.")
    url = payload.url.strip()
    if not YT_REGEX.search(url):
        raise HTTPException(422, "Invalid YouTube URL.")
    if payload.quality not in QUALITY_HEIGHT:
        raise HTTPException(422, f"Invalid quality: {payload.quality}")

    try:
        file_path, title = await _download_file(url, payload.quality, payload.format)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except httpx.TimeoutException:
        raise HTTPException(422, "Download timed out.")
    except Exception as e:
        print(f"[VidSave] download error: {e}")
        raise HTTPException(500, str(e))

    fname = safe_name(title) + file_path.suffix
    return FileResponse(
        path=str(file_path),
        filename=fname,
        media_type="application/octet-stream",
    )


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "backend": "rapidapi",
        "api_key": "set" if RAPIDAPI_KEY else "MISSING",
    }