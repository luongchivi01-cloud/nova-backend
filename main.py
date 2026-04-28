from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, json, base64, glob

app = FastAPI()
WORK_DIR = "/tmp/nova_work"
os.makedirs(WORK_DIR, exist_ok=True)

@app.get("/")
def root():
    return {"status": "Nova Backend v2 Online"}

def extract_frames(video_path, out_dir, count=6):
    """Extract N frames từ video để gửi Gemini"""
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration", "-of", "json", video_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        duration = float(json.loads(r.stdout)["format"]["duration"])
    except:
        duration = 30.0

    interval = duration / (count + 1)
    frames = []
    for i in range(1, count + 1):
        t = interval * i
        out_path = f"{out_dir}/frame_{i:02d}.jpg"
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(t), "-i", video_path,
            "-vframes", "1", "-q:v", "3",
            "-vf", "scale=720:-1", out_path
        ], capture_output=True)
        if os.path.exists(out_path):
            with open(out_path, "rb") as f:
                frames.append({
                    "timestamp": round(t, 1),
                    "base64": base64.b64encode(f.read()).decode()
                })
    return frames, duration

@app.post("/extract-frames")
async def api_extract_frames(video: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    video_path = f"{out_dir}/input.mp4"
    with open(video_path, "wb") as f:
        f.write(await video.read())
    frames, duration = extract_frames(video_path, f"{out_dir}/frames")
    return {
        "job_id": job_id,
        "duration": duration,
        "frames": frames
    }

@app.post("/analyze-url")
async def analyze_url(url: str = Form(...)):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    try:
        result = subprocess.run([
            "yt-dlp", "--no-playlist",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "--merge-output-format", "mp4",
            "-o", f"{out_dir}/video.mp4", url
        ], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr[-300:]}, status_code=400)
        video_path = f"{out_dir}/video.mp4"
        frames, duration = extract_frames(video_path, f"{out_dir}/frames")
        return {
            "job_id": job_id,
            "duration": duration,
            "frames": frames,
            "status": "ready"
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/create-video")
async def create_video(
    images: list[UploadFile] = File(default=[]),
    videos: list[UploadFile] = File(default=[]),
    style: str = Form("{}"),
    edit_plan: str = Form("{}")
):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)

    style_data = json.loads(style)
    plan_data = json.loads(edit_plan)
    duration_per_image = style_data.get("duration_per_image", 3)
    output_path = f"{out_dir}/output.mp4"

    input_args = []
    filter_parts = []
    idx = 0

    # Xử lý ảnh
    for i, img in enumerate(images):
        path = f"{out_dir}/img_{i:03d}.jpg"
        with open(path, "wb") as f:
            f.write(await img.read())
        input_args += ["-loop", "1", "-t", str(duration_per_image), "-i", path]
        filter_parts.append(
            f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[v{idx}]"
        )
        idx += 1

    # Xử lý video clips
    for i, vid in enumerate(videos):
        path = f"{out_dir}/vid_{i:03d}.mp4"
        with open(path, "wb") as f:
            f.write(await vid.read())

        trim_start = plan_data.get("trimStart", 0)
        trim_end = plan_data.get("trimEnd", 0)

        # Trim nếu có trong edit plan
        trimmed = f"{out_dir}/vid_{i:03d}_trim.mp4"
        trim_cmd = ["ffmpeg", "-y"]
        if trim_start > 0:
            trim_cmd += ["-ss", str(trim_start)]
        trim_cmd += ["-i", path]
        if trim_end > 0:
            trim_cmd += ["-t", str(trim_end - trim_start)]
        trim_cmd += ["-c", "copy", trimmed]
        subprocess.run(trim_cmd, capture_output=True)

        src = trimmed if os.path.exists(trimmed) else path
        input_args += ["-i", src]
        filter_parts.append(
            f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{idx}]"
        )
        idx += 1

    if idx == 0:
        return JSONResponse({"error": "Không có ảnh hoặc video"}, status_code=400)

    concat_in = "".join([f"[v{i}]" for i in range(idx)])
    filter_complex = ";".join(filter_parts) + f";{concat_in}concat=n={idx}:v=1:a=0[outv]"

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", "30", output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        return JSONResponse({"error": result.stderr[-500:]}, status_code=500)

    return {"job_id": job_id, "status": "done", "download_url": f"/download/{job_id}"}

@app.post("/edit-video")
async def edit_video(
    video: UploadFile = File(...),
    edit_plan: str = Form("{}")
):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)

    plan = json.loads(edit_plan)
    input_path = f"{out_dir}/input.mp4"
    output_path = f"{out_dir}/output.mp4"

    with open(input_path, "wb") as f:
        f.write(await video.read())

    trim_start = plan.get("trimStart", 0)
    trim_end = plan.get("trimEnd", 0)
    speed = plan.get("speed", 1.0)
    brightness = plan.get("brightness", 1.0)
    contrast = plan.get("contrast", 1.0)

    vf_filters = []
    vf_filters.append(f"scale=1080:1920:force_original_aspect_ratio=decrease")
    vf_filters.append(f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2")
    vf_filters.append(f"eq=brightness={brightness-1:.2f}:contrast={contrast:.2f}")
    if speed != 1.0:
        vf_filters.append(f"setpts={1/speed:.2f}*PTS")

    cmd = ["ffmpeg", "-y"]
    if trim_start > 0:
        cmd += ["-ss", str(trim_start)]
    cmd += ["-i", input_path]
    if trim_end > trim_start:
        cmd += ["-t", str(trim_end - trim_start)]
    cmd += [
        "-vf", ",".join(vf_filters),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", "30", output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        return JSONResponse({"error": result.stderr[-500:]}, status_code=500)

    return {"job_id": job_id, "status": "done", "download_url": f"/download/{job_id}"}

@app.get("/download/{job_id}")
def download(job_id: str):
    path = f"{WORK_DIR}/{job_id}/output.mp4"
    if not os.path.exists(path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="nova_output.mp4")
