import os
import uuid
import threading
import time
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file
from imageio_ffmpeg import get_ffmpeg_exe
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
FFMPEG_PATH = get_ffmpeg_exe()

jobs = {}
info_cache = {}


def cleanup_file(paths, delay=600):
    def _remove():
        time.sleep(delay)
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
    threading.Thread(target=_remove, daemon=True).start()


def fetch_info(url):
    if url in info_cache:
        cached_time, cached_data = info_cache[url]
        if time.time() - cached_time < 300:
            return cached_data

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 10,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        result = {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "channel": info.get("channel", info.get("uploader", "")),
        }
        info_cache[url] = (time.time(), result)
        return result


COMMON_OPTS = {
    "ffmpeg_location": FFMPEG_PATH,
    "quiet": True,
    "no_warnings": True,
    "concurrent_fragment_downloads": 8,
    "socket_timeout": 15,
    "retries": 5,
    "extractor_retries": 3,
    "throttled_rate": "100K",
    "extractor_args": {"youtube": {"player_client": ["mweb", "default"]}},
}


def download_audio(job_id, url, bitrate, audio_fmt="mp3"):
    output_path = DOWNLOAD_DIR / f"{job_id}.%(ext)s"
    jobs[job_id] = {"status": "downloading", "progress": 0, "title": "", "file": None, "error": None, "format": audio_fmt}

    cached = info_cache.get(url)
    if cached:
        jobs[job_id]["title"] = cached[1].get("title", "")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                pct = 100 if audio_fmt == "m4a" else 90
                jobs[job_id]["progress"] = int(downloaded / total * pct)
        elif d["status"] == "finished":
            if audio_fmt == "mp3":
                jobs[job_id]["status"] = "converting"
            jobs[job_id]["progress"] = 90 if audio_fmt == "mp3" else 100

    opts = {**COMMON_OPTS, "outtmpl": str(output_path), "progress_hooks": [progress_hook]}

    if audio_fmt == "m4a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
        opts["postprocessors"] = []
    else:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate),
        }]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            jobs[job_id]["title"] = info.get("title", "audio")

        if audio_fmt == "m4a":
            candidates = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
            out_file = next((f for f in candidates if f.suffix in (".m4a", ".webm", ".opus", ".ogg")), None)
        else:
            out_file = DOWNLOAD_DIR / f"{job_id}.mp3"
            if not out_file.exists():
                out_file = None

        if out_file and out_file.exists():
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["file"] = str(out_file)
            jobs[job_id]["format"] = out_file.suffix.lstrip(".")
            cleanup_file([str(out_file)])
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Conversion failed"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


def download_video(job_id, url, quality):
    jobs[job_id] = {"status": "downloading", "progress": 0, "title": "", "file": None, "error": None, "format": "mp4"}

    cached = info_cache.get(url)
    if cached:
        jobs[job_id]["title"] = cached[1].get("title", "")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                jobs[job_id]["progress"] = int(downloaded / total * 90)
        elif d["status"] == "finished":
            jobs[job_id]["status"] = "converting"
            jobs[job_id]["progress"] = 90

    height = quality.replace("p", "")
    format_str = f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"

    opts = {
        **COMMON_OPTS,
        "format": format_str,
        "outtmpl": str(DOWNLOAD_DIR / f"{job_id}.mp4"),
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            jobs[job_id]["title"] = info.get("title", "video")

        mp4_file = DOWNLOAD_DIR / f"{job_id}.mp4"
        if mp4_file.exists():
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["file"] = str(mp4_file)
            cleanup_file([str(mp4_file)])
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Download failed"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/preview", methods=["POST"])
def preview():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    try:
        info = fetch_info(url)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/convert", methods=["POST"])
def convert():
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:12]

    if fmt == "mp4":
        thread = threading.Thread(target=download_video, args=(job_id, url, quality), daemon=True)
    elif fmt == "m4a":
        thread = threading.Thread(target=download_audio, args=(job_id, url, 0, "m4a"), daemon=True)
    else:
        thread = threading.Thread(target=download_audio, args=(job_id, url, int(quality), "mp3"), daemon=True)

    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/batch", methods=["POST"])
def batch():
    data = request.json
    urls = data.get("urls", [])
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")

    job_ids = []
    for url in urls[:10]:
        url = url.strip()
        if not url:
            continue
        job_id = uuid.uuid4().hex[:12]
        job_ids.append(job_id)
        if fmt == "mp4":
            thread = threading.Thread(target=download_video, args=(job_id, url, quality), daemon=True)
        elif fmt == "m4a":
            thread = threading.Thread(target=download_audio, args=(job_id, url, 0, "m4a"), daemon=True)
        else:
            thread = threading.Thread(target=download_audio, args=(job_id, url, int(quality), "mp3"), daemon=True)
        thread.start()

    return jsonify({"job_ids": job_ids})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        return jsonify({"error": "File not ready"}), 404

    safe_title = "".join(c for c in job["title"] if c.isalnum() or c in " -_").strip() or "audio"
    ext = job.get("format", "mp3")
    return send_file(job["file"], as_attachment=True, download_name=f"{safe_title}.{ext}")


if __name__ == "__main__":
    app.run(debug=True, port=5555)
