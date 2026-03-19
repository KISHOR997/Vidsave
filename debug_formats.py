"""
Run this to diagnose quality issues:
  python debug_formats.py "https://www.youtube.com/watch?v=YOUR_VIDEO_ID"
"""
import sys
import yt_dlp

def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    print(f"\nFetching formats for: {url}\n")

    with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])

    # ── Video-only (DASH) ──
    print("=" * 70)
    print("VIDEO-ONLY streams (need ffmpeg to add audio):")
    print("-" * 70)
    print(f"  {'ID':>8}  {'EXT':>5}  {'HEIGHT':>7}  {'WIDTH':>6}  {'TBR':>7}  CODEC")
    print("-" * 70)
    for f in formats:
        vco = f.get("vcodec", "none")
        aco = f.get("acodec", "none")
        h   = f.get("height")
        if vco not in (None,"none") and aco in (None,"none") and h:
            print(f"  {f['format_id']:>8}  {f.get('ext','?'):>5}  "
                  f"{h:>7}p  {f.get('width',0):>6}  "
                  f"{f.get('tbr',0):>7.0f}  {vco[:20]}")

    # ── Audio-only ──
    print("\nAUDIO-ONLY streams:")
    print("-" * 70)
    print(f"  {'ID':>8}  {'EXT':>5}  {'ABR':>7}  CODEC")
    print("-" * 70)
    for f in formats:
        vco = f.get("vcodec", "none")
        aco = f.get("acodec", "none")
        if aco not in (None,"none") and vco in (None,"none"):
            print(f"  {f['format_id']:>8}  {f.get('ext','?'):>5}  "
                  f"{f.get('abr',0):>7.0f}  {aco[:20]}")

    # ── Pre-muxed (video+audio already combined) ──
    print("\nPRE-MUXED streams (video+audio, no ffmpeg needed):")
    print("-" * 70)
    print(f"  {'ID':>8}  {'EXT':>5}  {'HEIGHT':>7}  {'TBR':>7}")
    print("-" * 70)
    muxed = []
    for f in formats:
        vco = f.get("vcodec", "none")
        aco = f.get("acodec", "none")
        h   = f.get("height")
        if vco not in (None,"none") and aco not in (None,"none") and h:
            muxed.append(f)
            print(f"  {f['format_id']:>8}  {f.get('ext','?'):>5}  "
                  f"{h:>7}p  {f.get('tbr',0):>7.0f}")

    if not muxed:
        print("  (none — ffmpeg is REQUIRED for this video)")

    # ── Summary ──
    video_heights = sorted(set(
        f["height"] for f in formats
        if f.get("vcodec","none") not in (None,"none")
        and f.get("acodec","none") in (None,"none")
        and f.get("height")
    ), reverse=True)

    muxed_heights = sorted(set(
        f["height"] for f in muxed
    ), reverse=True)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print(f"  Video-only heights : {video_heights}")
    print(f"  Pre-muxed heights  : {muxed_heights}")
    print(f"  ffmpeg needed for  : {[h for h in video_heights if h not in muxed_heights]}")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    main()
