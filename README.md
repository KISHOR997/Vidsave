# VidSave — YouTube to MP4 Converter (FastAPI)

## Project structure

```
vidssave/
├── main.py                  # FastAPI app — routes & API logic
├── requirements.txt
├── templates/
│   └── index.html           # Jinja2 HTML template
└── static/
    ├── css/
    │   └── style.css        # All styles
    └── js/
        └── app.js           # Fetch calls to FastAPI endpoints
```

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the dev server
uvicorn main:app --reload

# 4. Open in browser
#    http://127.0.0.1:8000
```

## API endpoints

| Method | Path            | Description                                |
|--------|-----------------|--------------------------------------------|
| GET    | `/`             | Serves the HTML UI                         |
| POST   | `/api/convert`  | Accepts `{url, format}` → returns metadata + quality list |
| POST   | `/api/download` | Accepts `{url, quality, format}` → returns download URL   |
| GET    | `/health`       | Health check                               |

### POST /api/convert — request body
```json
{ "url": "https://www.youtube.com/watch?v=...", "format": "mp4" }
```

### POST /api/download — request body
```json
{ "url": "https://www.youtube.com/watch?v=...", "quality": "1080p", "format": "mp4" }
```

## Plugging in real downloads (yt-dlp)

In `main.py`, find the `mock_video_info()` function and the `download()` route.
Replace the mock logic with yt-dlp calls:

```python
import yt_dlp

def get_video_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title":     info.get("title"),
            "channel":   info.get("uploader"),
            "duration":  str(info.get("duration_string")),
            "views":     f"{info.get('view_count', 0):,} views",
            "thumbnail": info.get("thumbnail"),
            "video_id":  info.get("id"),
        }
```

Uncomment `yt-dlp` in `requirements.txt` and install it:
```bash
pip install yt-dlp
```
