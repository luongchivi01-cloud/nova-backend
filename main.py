from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, json, base64

app = FastAPI()
WORK_DIR = "/tmp/nova_work"
os.makedirs(WORK_DIR, exist_ok=True)

@app.get("/")
def root():
    return {"status": "Nova Backend v2 Online"}

def extract_frames(video_path, out_dir, count=6):
    os.makedirs(out_dir, exist_ok=True)
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",video_path], capture_output=True, text=True)
    try:
        duration = float(json.loads(r.stdout)["format"]["duration"])
    except:
        duration = 30.0
    interval = duration / (count + 1)
    frames = []
    for i in range(1, count + 1):
        t = interval * i
        out_path = f"{out_dir}/frame_{i:02d}.jpg"
        subprocess.run(["ffmpeg","-y","-ss",str(t),"-i",video_path,"-vframes","1","-q:v","3","-vf","scale=720:-1",out_path], capture_output=True)
        if os.path.exists(out_path):
            with open(out_path,"rb") as f:
                frames.append({"timestamp":round(t,1),"base64":base64.b64encode(f.read()).decode()})
    return frames, duration

@app.post("/extract-frames")
async def api_extract_frames(video: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    video_path = f"{out_dir}/input.mp4"
    with open(video_path,"wb") as f:
        f.write(await video.read())
    frames, duration = extract_frames(video_path, f"{out_dir}/frames")
    return {"job_id":job_id,"duration":duration,"frames":frames}

@app.post("/analyze-url")
async def analyze_url(url: str = Form(...)):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    try:
        result = subprocess.run([
            "yt-dlp","--no-playlist",
            "-f","best[height<=720]/best",
            "--no-check-certificate",
            "-o",f"{out_dir}/video.mp4", url
        ], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr[-300:]}, status_code=400)
        frames, duration = extract_frames(f"{out_dir}/video.mp4", f"{out_dir}/frames")
        return {"job_id":job_id,"duration":duration,"frames":frames,"status":"ready"}
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

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

    # Style params
    duration_per_image = float(style_data.get("duration_per_image", 3))
    speed = float(plan_data.get("speed", 1.0))
    brightness = float(plan_data.get("brightness", 1.0))
    contrast = float(plan_data.get("contrast", 1.0))
    transition = style_data.get("transition", "fade")

    output_path = f"{out_dir}/output.mp4"
    inputs = []

    # Xử lý ảnh → clip có áp dụng style
    for i, img in enumerate(images):
        content = await img.read()
        if len(content) < 100:
            continue
        img_path = f"{out_dir}/img_{i:03d}.jpg"
        with open(img_path,"wb") as f:
            f.write(content)

        clip_path = f"{out_dir}/img_{i:03d}_clip.mp4"

        # Build filter với style thật
        vf_parts = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "setsar=1",
            f"eq=brightness={brightness-1:.3f}:contrast={contrast:.3f}:saturation=1.2",
        ]
        if speed != 1.0:
            vf_parts.append(f"setpts={1/speed:.3f}*PTS")

        # Thêm zoom ken burns effect
        vf_parts.append(f"zoompan=z=\'min(zoom+0.0015,1.5)\':d={int(duration_per_image*30)}:s=1080x1920")

        vf_str = ",".join(vf_parts)

        r = subprocess.run([
            "ffmpeg","-y",
            "-loop","1","-i", img_path,
            "-t", str(duration_per_image),
            "-vf", vf_str,
            "-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast",
            clip_path
        ], capture_output=True, text=True, timeout=60)

        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
            inputs.append(clip_path)

    # Xử lý video clips
    for i, vid in enumerate(videos):
        content = await vid.read()
        if len(content) < 100:
            continue
        vid_path = f"{out_dir}/vid_{i:03d}.mp4"
        with open(vid_path,"wb") as f:
            f.write(content)

        clip_path = f"{out_dir}/vid_{i:03d}_clip.mp4"
        trim_start = float(plan_data.get("trimStart", 0))
        trim_end = float(plan_data.get("trimEnd", 0))

        vf_parts = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "setsar=1",
            f"eq=brightness={brightness-1:.3f}:contrast={contrast:.3f}:saturation=1.2",
        ]
        if speed != 1.0:
            vf_parts.append(f"setpts={1/speed:.3f}*PTS")

        cmd = ["ffmpeg","-y"]
        if trim_start > 0:
            cmd += ["-ss", str(trim_start)]
        cmd += ["-i", vid_path]
        if trim_end > trim_start:
            cmd += ["-t", str(trim_end - trim_start)]
        cmd += ["-vf", ",".join(vf_parts), "-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast", clip_path]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
            inputs.append(clip_path)

    if not inputs:
        return JSONResponse({"error":"Không tạo được clip — file không hợp lệ"}, status_code=500)

    # Concat với transition fade
    if len(inputs) == 1:
        # 1 clip thì copy thẳng
        import shutil
        shutil.copy(inputs[0], output_path)
    else:
        # Nhiều clip → concat
        concat_path = f"{out_dir}/concat.txt"
        with open(concat_path,"w") as f:
            for p in inputs:
                f.write(f"file '{p}'\n")

        r = subprocess.run([
            "ffmpeg","-y","-f","concat","-safe","0",
            "-i", concat_path,
            "-c","copy", output_path
        ], capture_output=True, text=True, timeout=120)

        if r.returncode != 0 or not os.path.exists(output_path):
            return JSONResponse({"error": r.stderr[-500:]}, status_code=500)

    return {"job_id":job_id,"status":"done","download_url":f"/download/{job_id}"}

@app.post("/edit-video")
async def edit_video(video: UploadFile = File(...), edit_plan: str = Form("{}")):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    plan = json.loads(edit_plan)
    input_path = f"{out_dir}/input.mp4"
    output_path = f"{out_dir}/output.mp4"
    with open(input_path,"wb") as f:
        f.write(await video.read())
    speed = plan.get("speed",1.0)
    brightness = plan.get("brightness",1.0)
    contrast = plan.get("contrast",1.0)
    vf = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        f"eq=brightness={brightness-1:.2f}:contrast={contrast:.2f}:saturation=1.1"
    ]
    if speed != 1.0:
        vf.append(f"setpts={1/speed:.2f}*PTS")
    cmd = ["ffmpeg","-y","-i",input_path,"-vf",",".join(vf),"-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast",output_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return JSONResponse({"error":r.stderr[-500:]}, status_code=500)
    return {"job_id":job_id,"status":"done","download_url":f"/download/{job_id}"}

@app.get("/download/{job_id}")
def download(job_id: str):
    path = f"{WORK_DIR}/{job_id}/output.mp4"
    if not os.path.exists(path):
        return JSONResponse({"error":"Not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="nova_output.mp4")
