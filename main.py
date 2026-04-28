from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, json, base64, re, shutil
from datetime import datetime

app = FastAPI()
WORK_DIR = "/tmp/nova_work"
KNOWLEDGE_FILE = "/tmp/nova_knowledge.json"
os.makedirs(WORK_DIR, exist_ok=True)

# ═══════════════════════════════════
# KNOWLEDGE STORE
# ═══════════════════════════════════

def load_knowledge() -> list:
    if os.path.exists(KNOWLEDGE_FILE):
        try:
            with open(KNOWLEDGE_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_knowledge_file(rules: list):
    with open(KNOWLEDGE_FILE, "w") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)

def add_rule(rule: dict) -> str:
    rules = load_knowledge()
    rule["id"] = str(uuid.uuid4())[:8]
    rule["learned_at"] = datetime.now().isoformat()
    rule["use_count"] = 0
    rule["rating"] = 0.0
    rules.append(rule)
    save_knowledge_file(rules)
    return rule["id"]

# ═══════════════════════════════════
# FILTER ENGINE
# ═══════════════════════════════════

def sanitize_vf(vf_str: str) -> str:
    if not vf_str or not isinstance(vf_str, str):
        return ""
    if any(c in vf_str for c in [';', '&&', '||', '`', '$', '>', '<', '|']):
        return ""
    if not re.match(r'^[a-zA-Z0-9=.,:\-_\'()\[\]/\\ ]+$', vf_str):
        return ""
    return vf_str.strip()

def compose_filter(rules: list, prompt: str = "") -> dict:
    """
    Compose filter thông minh từ knowledge:
    - Tutorial rules ưu tiên hơn style
    - Rating cao → weighted nhiều hơn
    - Keyword match với prompt
    - Average eq params từ top rules
    """
    if not rules:
        return {
            "ffmpeg_vf": "eq=brightness=0:contrast=1.0:saturation=1.0",
            "reasoning": "no knowledge yet — default filter",
            "sources": [],
            "tutorial_steps": []
        }

    # Score từng rule
    def score(r):
        s = r.get("rating", 0) * 2
        if r.get("type") == "tutorial":
            s += 3
        if prompt:
            desc = (r.get("description","") + r.get("style","") + r.get("mood","")).lower()
            for kw in prompt.lower().split():
                if kw in desc:
                    s += 2
        return s

    top = sorted(rules, key=score, reverse=True)[:3]

    # Parse eq params từ top rules
    eq_b, eq_c, eq_s = [], [], []
    extra_filters = []
    sources = []

    for r in top:
        vf = r.get("ffmpeg_vf", "")
        sources.append(f"[{r.get('type','?')}] {r.get('style','?')} rating={r.get('rating',0)}")
        m = re.search(r'eq=([^,\s]+)', vf)
        if m:
            for param in m.group(1).split(':'):
                if '=' in param:
                    k, v = param.split('=', 1)
                    try:
                        val = float(v)
                        if k == 'brightness': eq_b.append(val)
                        elif k == 'contrast':  eq_c.append(val)
                        elif k == 'saturation': eq_s.append(val)
                    except:
                        pass
        # Extra non-eq filters
        for part in vf.split(','):
            part = part.strip()
            if part and not part.startswith('eq=') and sanitize_vf(part) and part not in extra_filters:
                extra_filters.append(part)

    def avg(lst, default):
        return round(sum(lst)/len(lst), 3) if lst else default

    b = avg(eq_b, 0)
    c = avg(eq_c, 1.0)
    s = avg(eq_s, 1.0)
    composed = f"eq=brightness={b}:contrast={c}:saturation={s}"

    parts = [composed]
    if extra_filters:
        parts.append(extra_filters[0])

    return {
        "ffmpeg_vf": ",".join(parts),
        "reasoning": f"Composed từ {len(top)} rules: {'; '.join(sources[:2])}",
        "sources": sources,
        "tutorial_steps": top[0].get("tutorial_steps", []) if top[0].get("type") == "tutorial" else []
    }

def build_vf(ai_vf: str) -> str:
    """Scale chuẩn 1080x1920 + AI filter"""
    parts = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "setsar=1"
    ]
    clean = sanitize_vf(ai_vf)
    if clean:
        parts.append(clean)
    return ",".join(parts)

# ═══════════════════════════════════
# FRAME EXTRACTION
# ═══════════════════════════════════

def extract_frames(video_path, out_dir, count=6):
    os.makedirs(out_dir, exist_ok=True)
    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration","-of","json", video_path],
        capture_output=True, text=True
    )
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
            "ffmpeg","-y","-ss",str(t),"-i",video_path,
            "-vframes","1","-q:v","3","-vf","scale=720:-1",out_path
        ], capture_output=True)
        if os.path.exists(out_path):
            with open(out_path,"rb") as f:
                frames.append({"timestamp":round(t,1),"base64":base64.b64encode(f.read()).decode()})
    return frames, duration

# ═══════════════════════════════════
# CLIP BUILDERS (tách ra dùng chung)
# ═══════════════════════════════════

def make_image_clip(img_path, clip_path, vf_str, duration):
    vf = vf_str
    if "zoompan" not in vf:
        vf += f",zoompan=z='min(zoom+0.0015,1.5)':d={int(duration*30)}:s=1080x1920"
    r = subprocess.run([
        "ffmpeg","-y","-loop","1","-i",img_path,
        "-t",str(duration),"-vf",vf,
        "-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast",
        clip_path
    ], capture_output=True, text=True, timeout=60)
    return os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000

def make_video_clip(vid_path, clip_path, vf_str, trim_start=0, trim_end=0):
    cmd = ["ffmpeg","-y"]
    if trim_start > 0:
        cmd += ["-ss",str(trim_start)]
    cmd += ["-i",vid_path]
    if trim_end > trim_start:
        cmd += ["-t",str(trim_end - trim_start)]
    cmd += ["-vf",vf_str,"-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast",clip_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000

def concat_clips(inputs, output_path, out_dir):
    if len(inputs) == 1:
        shutil.copy(inputs[0], output_path)
        return True, ""
    concat_path = f"{out_dir}/concat.txt"
    with open(concat_path,"w") as f:
        for p in inputs:
            f.write(f"file '{p}'\n")
    r = subprocess.run([
        "ffmpeg","-y","-f","concat","-safe","0",
        "-i",concat_path,"-c","copy",output_path
    ], capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0 and os.path.exists(output_path)
    return ok, r.stderr[-300:] if not ok else ""

# ═══════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════

@app.get("/")
def root():
    rules = load_knowledge()
    return {
        "status": "Nova Backend v4 Online",
        "knowledge_count": len(rules),
        "tutorial_count": sum(1 for r in rules if r.get("type") == "tutorial"),
        "style_count": sum(1 for r in rules if r.get("type") == "style")
    }

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
            return JSONResponse({"error":result.stderr[-300:]}, status_code=400)
        frames, duration = extract_frames(f"{out_dir}/video.mp4", f"{out_dir}/frames")
        return {"job_id":job_id,"duration":duration,"frames":frames,"status":"ready"}
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

# ── Knowledge endpoints ──────────────────

@app.post("/save-knowledge")
async def save_knowledge_endpoint(rule: str = Form(...)):
    try:
        rule_data = json.loads(rule)
        rule_id = add_rule(rule_data)
        all_rules = load_knowledge()
        return {
            "status": "saved",
            "rule_id": rule_id,
            "total_rules": len(all_rules),
            "tutorial_count": sum(1 for r in all_rules if r.get("type") == "tutorial"),
            "style_count": sum(1 for r in all_rules if r.get("type") == "style")
        }
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

@app.get("/knowledge")
def get_knowledge():
    rules = load_knowledge()
    return {
        "total": len(rules),
        "tutorial_count": sum(1 for r in rules if r.get("type") == "tutorial"),
        "style_count": sum(1 for r in rules if r.get("type") == "style"),
        "rules": rules
    }

@app.post("/feedback")
async def feedback(rule_id: str = Form(...), rating: str = Form(...)):
    """👍 good = +1.0 / 👎 bad = -0.5"""
    delta = 1.0 if rating == "good" else -0.5
    rules = load_knowledge()
    for r in rules:
        if r.get("id") == rule_id:
            r["rating"] = round(r.get("rating", 0.0) + delta, 2)
            r["use_count"] = r.get("use_count", 0) + 1
            break
    save_knowledge_file(rules)
    return {"status": "updated", "rule_id": rule_id, "delta": delta}

@app.delete("/knowledge/{rule_id}")
def delete_knowledge(rule_id: str):
    rules = load_knowledge()
    rules = [r for r in rules if r.get("id") != rule_id]
    save_knowledge_file(rules)
    return {"status": "deleted", "remaining": len(rules)}

# ── Create video (nâng cấp từ v2) ───────────

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
    plan_data  = json.loads(edit_plan)

    # Ưu tiên: AI filter từ app → compose từ knowledge → default
    ai_vf = sanitize_vf(
        plan_data.get("ffmpeg_vf","") or style_data.get("ffmpeg_vf","")
    )
    if not ai_vf:
        rules = load_knowledge()
        if rules:
            ai_vf = compose_filter(rules).get("ffmpeg_vf","")

    dur = float(style_data.get("duration_per_image",
                plan_data.get("duration_per_clip", 3)))
    ts  = float(plan_data.get("trimStart", 0))
    te  = float(plan_data.get("trimEnd", 0))
    vf_str = build_vf(ai_vf)
    output_path = f"{out_dir}/output.mp4"
    inputs = []

    for i, img in enumerate(images):
        content = await img.read()
        if len(content) < 100: continue
        img_path  = f"{out_dir}/img_{i:03d}.jpg"
        clip_path = f"{out_dir}/img_{i:03d}_clip.mp4"
        with open(img_path,"wb") as f: f.write(content)
        if make_image_clip(img_path, clip_path, vf_str, dur):
            inputs.append(clip_path)

    for i, vid in enumerate(videos):
        content = await vid.read()
        if len(content) < 100: continue
        vid_path  = f"{out_dir}/vid_{i:03d}.mp4"
        clip_path = f"{out_dir}/vid_{i:03d}_clip.mp4"
        with open(vid_path,"wb") as f: f.write(content)
        if make_video_clip(vid_path, clip_path, vf_str, ts, te):
            inputs.append(clip_path)

    if not inputs:
        return JSONResponse({"error":"Không tạo được clip — file không hợp lệ"}, status_code=500)

    ok, err = concat_clips(inputs, output_path, out_dir)
    if not ok:
        return JSONResponse({"error": err}, status_code=500)

    return {"job_id":job_id,"status":"done","download_url":f"/download/{job_id}","filter_used":ai_vf}

# ── Edit video (nâng cấp từ v2) ─────────────

@app.post("/edit-video")
async def edit_video(video: UploadFile = File(...), edit_plan: str = Form("{}")):
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    plan = json.loads(edit_plan)
    input_path  = f"{out_dir}/input.mp4"
    output_path = f"{out_dir}/output.mp4"
    with open(input_path,"wb") as f:
        f.write(await video.read())

    ai_vf = sanitize_vf(plan.get("ffmpeg_vf",""))
    if not ai_vf:
        rules = load_knowledge()
        if rules:
            ai_vf = compose_filter(rules).get("ffmpeg_vf","")

    vf_str = build_vf(ai_vf)
    r = subprocess.run([
        "ffmpeg","-y","-i",input_path,
        "-vf",vf_str,
        "-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast",
        output_path
    ], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return JSONResponse({"error":r.stderr[-500:]}, status_code=500)
    return {"job_id":job_id,"status":"done","download_url":f"/download/{job_id}","filter_used":ai_vf}

# ── Smart edit (endpoint mới) ────────────────

@app.post("/smart-edit")
async def smart_edit(
    video:             UploadFile       = File(None),
    images:            list[UploadFile] = File(default=[]),
    videos:            list[UploadFile] = File(default=[]),
    prompt:            str = Form(""),
    ffmpeg_vf:         str = Form(""),
    duration_per_clip: str = Form("3"),
    trim_start:        str = Form("0"),
    trim_end:          str = Form("0")
):
    """
    Smart edit — backend tự chọn filter từ knowledge nếu app không gửi.
    Trả về filter_used + reasoning để app hiển thị.
    """
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    output_path = f"{out_dir}/output.mp4"

    dur = float(duration_per_clip)
    ts  = float(trim_start)
    te  = float(trim_end)

    # Quyết định filter
    clean_vf = sanitize_vf(ffmpeg_vf)
    if clean_vf:
        final_vf     = clean_vf
        compose_info = {"reasoning":"AI filter từ app","sources":[],"tutorial_steps":[]}
    else:
        rules        = load_knowledge()
        compose_info = compose_filter(rules, prompt)
        final_vf     = compose_info["ffmpeg_vf"]

    vf_str = build_vf(final_vf)
    inputs = []

    # Single video
    if video and video.filename:
        content = await video.read()
        if len(content) > 100:
            vid_path  = f"{out_dir}/input.mp4"
            clip_path = f"{out_dir}/input_clip.mp4"
            with open(vid_path,"wb") as f: f.write(content)
            if make_video_clip(vid_path, clip_path, vf_str, ts, te):
                inputs.append(clip_path)

    for i, img in enumerate(images):
        content = await img.read()
        if len(content) < 100: continue
        img_path  = f"{out_dir}/img_{i:03d}.jpg"
        clip_path = f"{out_dir}/img_{i:03d}_clip.mp4"
        with open(img_path,"wb") as f: f.write(content)
        if make_image_clip(img_path, clip_path, vf_str, dur):
            inputs.append(clip_path)

    for i, vid in enumerate(videos):
        content = await vid.read()
        if len(content) < 100: continue
        vid_path  = f"{out_dir}/vid_{i:03d}.mp4"
        clip_path = f"{out_dir}/vid_{i:03d}_clip.mp4"
        with open(vid_path,"wb") as f: f.write(content)
        if make_video_clip(vid_path, clip_path, vf_str, ts, te):
            inputs.append(clip_path)

    if not inputs:
        return JSONResponse({"error":"Không tạo được clip"}, status_code=500)

    ok, err = concat_clips(inputs, output_path, out_dir)
    if not ok:
        return JSONResponse({"error":err}, status_code=500)

    return {
        "job_id":         job_id,
        "status":         "done",
        "download_url":   f"/download/{job_id}",
        "filter_used":    final_vf,
        "reasoning":      compose_info["reasoning"],
        "tutorial_steps": compose_info.get("tutorial_steps",[]),
        "sources":        compose_info.get("sources",[])
    }

@app.get("/download/{job_id}")
def download(job_id: str):
    path = f"{WORK_DIR}/{job_id}/output.mp4"
    if not os.path.exists(path):
        return JSONResponse({"error":"Not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="nova_output.mp4")
