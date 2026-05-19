import os
import uuid
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file
from imageio_ffmpeg import get_ffmpeg_exe
import yt_dlp

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
UPLOAD_DIR = Path(__file__).parent / "uploads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
FFMPEG_PATH = get_ffmpeg_exe()

jobs = {}
info_cache = {}

ALLOWED_EXTENSIONS = {
    'mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a', 'wma', 'opus', 'webm',
    'mp4', 'avi', 'mov', 'mkv', 'wmv',
}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def cleanup_file(paths, delay=600):
    def _remove():
        time.sleep(delay)
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
    threading.Thread(target=_remove, daemon=True).start()


def safe_title(title):
    return "".join(c for c in title if c.isalnum() or c in " -_").strip() or "file"


# ── YouTube / TikTok ──

def fetch_info(url):
    if url in info_cache:
        cached_time, cached_data = info_cache[url]
        if time.time() - cached_time < 300:
            return cached_data
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 10}
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
    else:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            jobs[job_id]["title"] = info.get("title", "audio")
        if audio_fmt == "m4a":
            out_file = next((f for f in DOWNLOAD_DIR.glob(f"{job_id}.*") if f.suffix in (".m4a", ".webm", ".opus", ".ogg")), None)
        else:
            out_file = DOWNLOAD_DIR / f"{job_id}.mp3"
            if not out_file.exists():
                out_file = None
        if out_file and out_file.exists():
            jobs[job_id].update({"status": "done", "progress": 100, "file": str(out_file), "format": out_file.suffix.lstrip(".")})
            cleanup_file([str(out_file)])
        else:
            jobs[job_id].update({"status": "error", "error": "Conversion failed"})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


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
    fmt = f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    opts = {**COMMON_OPTS, "format": fmt, "outtmpl": str(DOWNLOAD_DIR / f"{job_id}.mp4"), "merge_output_format": "mp4", "progress_hooks": [progress_hook]}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            jobs[job_id]["title"] = info.get("title", "video")
        mp4_file = DOWNLOAD_DIR / f"{job_id}.mp4"
        if mp4_file.exists():
            jobs[job_id].update({"status": "done", "progress": 100, "file": str(mp4_file)})
            cleanup_file([str(mp4_file)])
        else:
            jobs[job_id].update({"status": "error", "error": "Download failed"})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


# ── File conversion ──

QUALITY_MAP = {
    "low": {"audio_bitrate": "96k", "crf": "32", "preset": "ultrafast"},
    "medium": {"audio_bitrate": "192k", "crf": "26", "preset": "medium"},
    "high": {"audio_bitrate": "320k", "crf": "20", "preset": "slow"},
}

AUDIO_FORMATS = {'mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a', 'wma', 'opus'}
VIDEO_FORMATS = {'mp4', 'webm', 'avi', 'mov', 'mkv'}


def convert_file(job_id, input_path, output_fmt, quality):
    jobs[job_id] = {"status": "converting", "progress": 0, "title": Path(input_path).stem, "file": None, "error": None, "format": output_fmt}
    output_path = str(DOWNLOAD_DIR / f"{job_id}.{output_fmt}")
    q = QUALITY_MAP.get(quality, QUALITY_MAP["medium"])

    cmd = [FFMPEG_PATH, "-y", "-i", input_path]
    if output_fmt in AUDIO_FORMATS:
        cmd += ["-b:a", q["audio_bitrate"], "-vn"]
    else:
        cmd += ["-crf", q["crf"], "-preset", q["preset"], "-b:a", q["audio_bitrate"]]
    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and Path(output_path).exists():
            jobs[job_id].update({"status": "done", "progress": 100, "file": output_path})
            cleanup_file([input_path, output_path])
        else:
            jobs[job_id].update({"status": "error", "error": result.stderr[:200] or "Conversion failed"})
            cleanup_file([input_path])
    except subprocess.TimeoutExpired:
        jobs[job_id].update({"status": "error", "error": "Conversion timed out"})
        cleanup_file([input_path])
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})
        cleanup_file([input_path])


# ── Video compression ──

COMPRESS_MAP = {
    "light": {"crf": "24", "preset": "fast"},
    "medium": {"crf": "30", "preset": "medium"},
    "heavy": {"crf": "36", "preset": "slow"},
}


def compress_video(job_id, input_path, level, resolution):
    original_size = os.path.getsize(input_path)
    jobs[job_id] = {"status": "converting", "progress": 0, "title": Path(input_path).stem, "file": None, "error": None, "format": "mp4", "savings": 0}
    output_path = str(DOWNLOAD_DIR / f"{job_id}.mp4")
    c = COMPRESS_MAP.get(level, COMPRESS_MAP["medium"])

    cmd = [FFMPEG_PATH, "-y", "-i", input_path, "-c:v", "libx264", "-crf", c["crf"], "-preset", c["preset"], "-c:a", "aac", "-b:a", "128k"]
    if resolution != "original":
        cmd += ["-vf", f"scale=-2:{resolution}"]
    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode == 0 and Path(output_path).exists():
            new_size = os.path.getsize(output_path)
            savings = max(0, round((1 - new_size / original_size) * 100))
            jobs[job_id].update({"status": "done", "progress": 100, "file": output_path, "savings": savings})
            cleanup_file([input_path, output_path])
        else:
            jobs[job_id].update({"status": "error", "error": result.stderr[:200] or "Compression failed"})
            cleanup_file([input_path])
    except subprocess.TimeoutExpired:
        jobs[job_id].update({"status": "error", "error": "Compression timed out"})
        cleanup_file([input_path])
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})
        cleanup_file([input_path])


# ── Audio trimming ──

def trim_audio(job_id, input_path, start, end, output_fmt):
    ext = output_fmt if output_fmt != "same" else Path(input_path).suffix.lstrip(".")
    jobs[job_id] = {"status": "converting", "progress": 0, "title": Path(input_path).stem, "file": None, "error": None, "format": ext}
    output_path = str(DOWNLOAD_DIR / f"{job_id}.{ext}")
    duration = end - start

    cmd = [FFMPEG_PATH, "-y", "-i", input_path, "-ss", str(start), "-t", str(duration)]
    if output_fmt == "same":
        cmd += ["-c", "copy"]
    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and Path(output_path).exists():
            jobs[job_id].update({"status": "done", "progress": 100, "file": output_path})
            cleanup_file([input_path, output_path])
        else:
            jobs[job_id].update({"status": "error", "error": result.stderr[:200] or "Trim failed"})
            cleanup_file([input_path])
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})
        cleanup_file([input_path])


# ── Routes: Pages ──

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/youtube")
def youtube_page():
    return render_template("youtube.html")

@app.route("/convert")
def convert_page():
    return render_template("convert.html")

@app.route("/compress")
def compress_page():
    return render_template("compress.html")

@app.route("/trim")
def trim_page():
    return render_template("trim.html")


# ── Routes: YT/TikTok API ──

@app.route("/api/preview", methods=["POST"])
def api_preview():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    try:
        return jsonify(fetch_info(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/yt/convert", methods=["POST"])
def api_yt_convert():
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")
    if not url:
        return jsonify({"error": "No URL"}), 400

    job_id = uuid.uuid4().hex[:12]
    if fmt == "mp4":
        t = threading.Thread(target=download_video, args=(job_id, url, quality), daemon=True)
    elif fmt == "m4a":
        t = threading.Thread(target=download_audio, args=(job_id, url, 0, "m4a"), daemon=True)
    else:
        t = threading.Thread(target=download_audio, args=(job_id, url, int(quality), "mp3"), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/api/yt/batch", methods=["POST"])
def api_yt_batch():
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
            t = threading.Thread(target=download_video, args=(job_id, url, quality), daemon=True)
        elif fmt == "m4a":
            t = threading.Thread(target=download_audio, args=(job_id, url, 0, "m4a"), daemon=True)
        else:
            t = threading.Thread(target=download_audio, args=(job_id, url, int(quality), "mp3"), daemon=True)
        t.start()
    return jsonify({"job_ids": job_ids})


# ── Routes: File tools API ──

def save_upload(req):
    if 'file' not in req.files:
        return None, "No file uploaded"
    f = req.files['file']
    if not f.filename or not allowed_file(f.filename):
        return None, "Invalid file type"
    job_id = uuid.uuid4().hex[:12]
    ext = f.filename.rsplit('.', 1)[1].lower()
    input_path = str(UPLOAD_DIR / f"{job_id}_input.{ext}")
    f.save(input_path)
    return job_id, input_path

@app.route("/api/file/convert", methods=["POST"])
def api_file_convert():
    job_id, result = save_upload(request)
    if not job_id:
        return jsonify({"error": result}), 400
    fmt = request.form.get("format", "mp3")
    quality = request.form.get("quality", "medium")
    t = threading.Thread(target=convert_file, args=(job_id, result, fmt, quality), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/api/file/compress", methods=["POST"])
def api_file_compress():
    job_id, result = save_upload(request)
    if not job_id:
        return jsonify({"error": result}), 400
    level = request.form.get("level", "medium")
    resolution = request.form.get("resolution", "720")
    t = threading.Thread(target=compress_video, args=(job_id, result, level, resolution), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/api/file/trim", methods=["POST"])
def api_file_trim():
    job_id, result = save_upload(request)
    if not job_id:
        return jsonify({"error": result}), 400
    start = int(request.form.get("start", 0))
    end = int(request.form.get("end", 0))
    fmt = request.form.get("format", "same")
    if end <= start:
        return jsonify({"error": "End must be after start"}), 400
    t = threading.Thread(target=trim_audio, args=(job_id, result, start, end, fmt), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


# ── Routes: Shared ──

@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        return jsonify({"error": "File not ready"}), 404
    title = safe_title(job.get("title", "file"))
    ext = job.get("format", "mp3")
    return send_file(job["file"], as_attachment=True, download_name=f"{title}.{ext}")


if __name__ == "__main__":
    app.run(debug=True, port=5555)
