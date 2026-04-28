from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, shutil, json, tempfile

app = FastAPI()
WORK_DIR = "/tmp/nova_work"
os.makedirs(WORK_DIR, exist_ok=True)

@app.get("/")
def root():
    return {"status": "Nova Backend Online"}

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
            return JSONResponse({"error": result.stderr}, status_code=400)
        return {"job_id": job_id, "status": "downloaded", "message": "Video ready for analysis"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/create-video")
async def create_video(
    background_tasks: BackgroundTasks,
    images: list[UploadFile] = File(...),
    style: str = Form("{}"),
    music_url: str = Form(None)
):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)

    style_data = json.loads(style)
    duration_per_image = style_data.get("duration_per_image", 3)
    transition = style_data.get("transition", "fade")

    img_paths = []
    for i, img in enumerate(images):
        path = f"{out_dir}/img_{i:03d}.jpg"
        with open(path, "wb") as f:
            f.write(await img.read())
        img_paths.append(path)

    output_path = f"{out_dir}/output.mp4"

    filter_parts = []
    input_args = []
    for i, p in enumerate(img_paths):
        input_args += ["-loop", "1", "-t", str(duration_per_image), "-i", p]
        filter_parts.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")

    concat = "".join([f"[v{i}]" for i in range(len(img_paths))])
    filter_complex = ";".join(filter_parts) + f";{concat}concat=n={len(img_paths)}:v=1:a=0[outv]"

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

@app.get("/download/{job_id}")
def download(job_id: str):
    path = f"{WORK_DIR}/{job_id}/output.mp4"
    if not os.path.exists(path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="nova_output.mp4")

