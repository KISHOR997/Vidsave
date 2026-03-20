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
import shutil

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path("/tmp/vidssave")
TEMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="VidSave")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Cobalt API endpoint — no API key needed, completely free
COBALT_API = "https://api.cobalt.tools/"

COBALT_HEADERS = {
    "Accept":       "application/json",
    "Content-Type": "application/json",
}


# ---------- models ----------

class ConvertRequest(BaseModel):
    url: str
    format: str = "mp4"

class DownloadRequest(BaseModel):
    url: str
    quality: str
    format: str = "mp4"


# ---------- constants ----------

FORMATS = ["mp4", "mp3", "webm"]

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

def safe_name(t: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", t).strip()[:80]


# ---------- cobalt API ----------

async def cobalt_fetch_info(url: str) -> dict:
    """
    Ping Cobalt with the URL to verify it's valid and get basic info.
    Cobalt doesn't have a separate metadata endpoint so we probe with
    720p to confirm the URL works, then return mock quality cards.
    All qualities are marked available — Cobalt handles them all.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            COBALT_API,
            json={
                "url":           url,
                "videoQuality":  "720",
                "filenameStyle": "pretty",
            },
            headers=COBALT_HEADERS,
        )

    data = res.json()
    print(f"[VidSave] cobalt probe: status={data.get('status')} "
          f"code={res.status_code}")

    status = data.get("status", "")
    if status == "error":
        code = data.get("error", {}).get("code", "unknown")
        raise ValueError(f"Cobalt error: {code}")

    if status not in ("stream", "redirect", "tunnel", "picker"):
        raise ValueError(f"Unexpected Cobalt status: {status}")

    # Build quality list — Cobalt supports all of these
    qualities = []
    for res_label, height in QUALITY_HEIGHT.items():
        meta = QUALITY_META[res_label]
        qualities.append({
            "res":         res_label,
            "label":       meta["label"],
            "size":        "—",        # Cobalt doesn't give file sizes upfront
            "badge":       meta["badge"],
            "badge_class": meta["badge_class"],
            "available":   True,
            "needs_ffmpeg": False,
        })

    # Extract filename hint from Cobalt response for title
    filename = data.get("filename", "")
    title    = filename.rsplit(".", 1)[0] if filename else "YouTube Video"

    return {
        "video": {
            "title":     title or "YouTube Video",
            "channel":   "YouTube",
            "duration":  "—",
            "views":     "—",
            "thumbnail": f"https://img.youtube.com/vi/"
                         f"{_extract_vid_id(url)}/hqdefault.jpg",
            "video_id":  _extract_vid_id(url),
        },
        "qualities":    qualities,
        "ffmpeg_ready": True,  # Cobalt handles merging server-side
    }


async def cobalt_download(url: str, quality: str, fmt: str) -> tuple[Path, str]:
    """
    Download video via Cobalt API at the requested quality.
    Cobalt merges video+audio server-side — no ffmpeg needed locally.
    """
    height  = QUALITY_HEIGHT.get(quality, 720)
    out_dir = TEMP_DIR / uuid.uuid4().hex
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build Cobalt request
    payload = {
        "url":           url,
        "videoQuality":  str(height),
        "filenameStyle": "pretty",
    }

    if fmt == "mp3":
        payload["downloadMode"] = "audio"
    else:
        payload["downloadMode"] = "auto"

    print(f"[VidSave] cobalt request: quality={height} fmt={fmt}")

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            COBALT_API,
            json=payload,
            headers=COBALT_HEADERS,
        )

    data = res.json()
    print(f"[VidSave] cobalt response: {data.get('status')} "
          f"filename={data.get('filename')}")

    status = data.get("status", "")

    if status == "error":
        code = data.get("error", {}).get("code", "unknown")
        raise ValueError(f"Cobalt: {code}")

    if status == "picker":
        # Multiple streams — pick first video
        streams = data.get("picker", [])
        video_streams = [s for s in streams if s.get("type") == "video"]
        download_url  = (video_streams or streams)[0]["url"]
        filename      = data.get("filename") or f"video_{quality}.mp4"

    elif status in ("stream", "redirect", "tunnel"):
        download_url = data.get("url")
        filename     = data.get("filename") or f"video_{quality}.mp4"

    else:
        raise ValueError(f"Unexpected Cobalt status: {status}")

    if not download_url:
        raise ValueError("Cobalt returned no download URL")

    # Stream file to disk
    ext      = "mp3" if fmt == "mp3" else filename.rsplit(".", 1)[-1]
    out_file = out_dir / f"video_{quality}.{ext}"

    print(f"[VidSave] downloading from cobalt → {out_file.name}")

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

    title = filename.rsplit(".", 1)[0] if filename else "video"
    return out_file, title


def _extract_vid_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([\w\-]{11})", url)
    return m.group(1) if m else ""


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request":      request,
        "formats":      FORMATS,
        "ffmpeg_ready": True,
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
        data = await cobalt_fetch_info(url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except httpx.TimeoutException:
        raise HTTPException(422, "Cobalt API timed out. Try again.")
    except Exception as e:
        print(f"[VidSave] convert error: {e}")
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
        file_path, title = await cobalt_download(
            url, payload.quality, payload.format
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    except httpx.TimeoutException:
        raise HTTPException(422, "Download timed out. Try a lower quality.")
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
    return {"status": "ok", "backend": "cobalt"}