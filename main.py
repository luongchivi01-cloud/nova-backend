from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, json, base64, re, shutil, struct, wave
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

def render_timeline(timeline: list, duration: float, out_dir: str, base_vf: str) -> list:
    """
    Nhận timeline JSON từ Gemini → render từng segment → trả list clip paths.
    Gemini viết filter gì → chạy filter đó. Không hardcode.
    """
    clips = []
    for i, seg in enumerate(timeline):
        t_start = float(seg.get("time_start", 0))
        t_end   = float(seg.get("time_end", t_start + 3))
        seg_dur = max(t_end - t_start, 0.5)
        raw_filter = seg.get("ffmpeg_filter") or ""
        clean_filter = sanitize_vf(raw_filter)
        # Dùng filter của segment nếu có, fallback về base_vf
        vf = clean_filter if clean_filter else base_vf
        clip_path = f"{out_dir}/tl_{i:03d}.mp4"
        # Tạo clip màu đen có filter (placeholder visual)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=black:s=1080x1920:d={seg_dur}",
            "-vf", vf,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            "-preset", "ultrafast", clip_path
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 100:
            clips.append(clip_path)
    return clips


def compose_filter(rules: list, prompt: str = "") -> dict:
    if not rules:
        return {
            "ffmpeg_vf": "eq=brightness=0:contrast=1.0:saturation=1.0",
            "reasoning": "no knowledge yet",
            "sources": [],
            "tutorial_steps": []
        }
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
                        elif k == 'contrast': eq_c.append(val)
                        elif k == 'saturation': eq_s.append(val)
                    except:
                        pass
        for part in vf.split(','):
            part = part.strip()
            if part and not part.startswith('eq=') and sanitize_vf(part) and part not in extra_filters:
                extra_filters.append(part)
    def avg(lst, default):
        return round(sum(lst)/len(lst), 3) if lst else default
    b = avg(eq_b, 0)
    c = avg(eq_c, 1.0)
    s = avg(eq_s, 1.0)
    parts = [f"eq=brightness={b}:contrast={c}:saturation={s}"]
    if extra_filters:
        parts.append(extra_filters[0])
    return {
        "ffmpeg_vf": ",".join(parts),
        "reasoning": f"Composed từ {len(top)} rules: {'; '.join(sources[:2])}",
        "sources": sources,
        "tutorial_steps": top[0].get("tutorial_steps", []) if top[0].get("type") == "tutorial" else []
    }

def build_vf(ai_vf: str) -> str:
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
# FRAME + AUDIO EXTRACTION
# ═══════════════════════════════════

def extract_frames(video_path, out_dir, count=6):
    os.makedirs(out_dir, exist_ok=True)
    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration","-of","json",video_path],
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

def extract_audio(video_path, out_dir) -> str:
    """Extract audio mp3 từ video, giới hạn 60s đầu để tiết kiệm"""
    audio_path = f"{out_dir}/audio.mp3"
    subprocess.run([
        "ffmpeg","-y","-i",video_path,
        "-t","60",          # chỉ lấy 60s đầu
        "-vn",              # bỏ video
        "-acodec","mp3",
        "-ab","64k",        # bitrate thấp — đủ cho STT
        "-ar","16000",      # 16kHz — chuẩn Whisper
        audio_path
    ], capture_output=True, timeout=60)
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
        return audio_path
    return ""

# ═══════════════════════════════════
# SMART ANALYSIS (FREE TIER)
# ═══════════════════════════════════

def detect_scene_changes(video_path: str, threshold: float = 0.35) -> list:
    """
    Dùng FFmpeg scene change detection — hoàn toàn free.
    Trả về list timestamp (giây) tại điểm cắt thật.
    """
    r = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr", "-f", "null", "-"
    ], capture_output=True, text=True, timeout=120)

    timestamps = []
    for line in r.stderr.splitlines():
        if 'pts_time:' in line:
            try:
                t = float(line.split('pts_time:')[1].split()[0])
                timestamps.append(round(t, 2))
            except:
                pass
    # Luôn có điểm đầu
    if not timestamps or timestamps[0] > 0.5:
        timestamps.insert(0, 0.0)
    return sorted(set(timestamps))


def extract_smart_frames(video_path: str, out_dir: str, max_frames: int = 12) -> tuple:
    """
    Kết hợp: scene change frames + đều nhau.
    Tối đa max_frames để không spam Gemini.
    """
    os.makedirs(out_dir, exist_ok=True)

    # Lấy duration
    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration","-of","json", video_path],
        capture_output=True, text=True
    )
    try:
        duration = float(json.loads(r.stdout)["format"]["duration"])
    except:
        duration = 30.0

    # Scene change timestamps
    scene_ts = detect_scene_changes(video_path)

    # Đều nhau timestamps
    even_count = max(3, max_frames - len(scene_ts))
    interval = duration / (even_count + 1)
    even_ts = [round(interval * i, 2) for i in range(1, even_count + 1)]

    # Merge + deduplicate (không trùng trong vòng 0.5s)
    all_ts = sorted(set(scene_ts + even_ts))
    merged = []
    for t in all_ts:
        if not merged or t - merged[-1] > 0.5:
            merged.append(t)

    # Giới hạn max_frames
    if len(merged) > max_frames:
        # Ưu tiên scene change
        sc_set = set(scene_ts)
        priority = [t for t in merged if t in sc_set]
        others   = [t for t in merged if t not in sc_set]
        merged   = (priority + others)[:max_frames]
        merged   = sorted(merged)

    frames = []
    for i, t in enumerate(merged):
        if t >= duration:
            continue
        out_path = f"{out_dir}/smart_{i:02d}.jpg"
        subprocess.run([
            "ffmpeg","-y","-ss",str(t),"-i",video_path,
            "-vframes","1","-q:v","3","-vf","scale=720:-1", out_path
        ], capture_output=True, timeout=15)
        if os.path.exists(out_path):
            with open(out_path,"rb") as f:
                frames.append({
                    "timestamp": t,
                    "base64": base64.b64encode(f.read()).decode(),
                    "is_scene_change": t in set(scene_ts),
                })

    return frames, duration


def detect_beats(audio_path: str) -> dict:
    """
    Beat detection không cần librosa — dùng FFmpeg volumedetect + silencedetect.
    Phát hiện: BPM ước tính, energy peaks, silence gaps.
    Hoàn toàn free, không cần pip install.
    """
    result = {"bpm_estimate": 0, "beat_timestamps": [], "energy": "medium", "has_music": False}

    if not audio_path or not os.path.exists(audio_path):
        return result

    # Volume analysis
    r = subprocess.run([
        "ffmpeg", "-i", audio_path,
        "-af", "volumedetect", "-f", "null", "-"
    ], capture_output=True, text=True, timeout=30)

    mean_vol = -91.0
    max_vol  = -91.0
    for line in r.stderr.splitlines():
        if 'mean_volume:' in line:
            try: mean_vol = float(line.split('mean_volume:')[1].split('dB')[0].strip())
            except: pass
        if 'max_volume:' in line:
            try: max_vol = float(line.split('max_volume:')[1].split('dB')[0].strip())
            except: pass

    result["mean_volume_db"] = mean_vol
    result["max_volume_db"]  = max_vol

    if mean_vol > -30:
        result["energy"] = "high"
        result["has_music"] = True
    elif mean_vol > -45:
        result["energy"] = "medium"
        result["has_music"] = True
    else:
        result["energy"] = "low"

    # Silence detection → ước tính nhịp
    r2 = subprocess.run([
        "ffmpeg", "-i", audio_path,
        "-af", "silencedetect=noise=-40dB:d=0.1",
        "-f", "null", "-"
    ], capture_output=True, text=True, timeout=30)

    silence_ends = []
    for line in r2.stderr.splitlines():
        if 'silence_end:' in line:
            try:
                t = float(line.split('silence_end:')[1].strip().split()[0])
                silence_ends.append(round(t, 2))
            except: pass

    if len(silence_ends) >= 2:
        gaps = [silence_ends[i+1] - silence_ends[i] for i in range(len(silence_ends)-1)]
        avg_gap = sum(gaps) / len(gaps)
        if 0.3 < avg_gap < 2.0:
            result["bpm_estimate"] = round(60 / avg_gap)
            result["beat_timestamps"] = silence_ends[:20]

    return result


def analyze_color(video_path: str, out_dir: str) -> dict:
    """
    Color analysis kỹ thuật thuần — FFmpeg histogram.
    Không cần AI đoán màu. Trả về màu dominant thật + grade suggestion.
    """
    os.makedirs(out_dir, exist_ok=True)
    thumb = f"{out_dir}/color_thumb.png"

    # Extract 1 frame đại diện (giữa video)
    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration","-of","json", video_path],
        capture_output=True, text=True
    )
    try:
        dur = float(json.loads(r.stdout)["format"]["duration"])
        mid = dur / 2
    except:
        mid = 5.0

    subprocess.run([
        "ffmpeg","-y","-ss",str(mid),"-i",video_path,
        "-vframes","1","-vf","scale=160:90",thumb
    ], capture_output=True, timeout=15)

    # Dùng FFmpeg signalstats để lấy Y/U/V trung bình
    r2 = subprocess.run([
        "ffmpeg","-y","-ss",str(mid),"-i",video_path,
        "-vframes","30",
        "-vf","signalstats",
        "-f","null","-"
    ], capture_output=True, text=True, timeout=20)

    yuv = {"y": 128, "u": 128, "v": 128}
    for line in r2.stderr.splitlines():
        if 'YAVG' in line:
            try: yuv["y"] = float(line.split('YAVG:')[1].split()[0])
            except: pass
        if 'UAVG' in line:
            try: yuv["u"] = float(line.split('UAVG:')[1].split()[0])
            except: pass
        if 'VAVG' in line:
            try: yuv["v"] = float(line.split('VAVG:')[1].split()[0])
            except: pass

    # Phân tích từ YUV → tạo ffmpeg filter phù hợp
    brightness = (yuv["y"] - 128) / 128  # -1 to 1
    warmth = (yuv["v"] - 128) / 128      # + = warm/orange, - = cool/blue
    saturation_hint = abs(yuv["u"] - 128) + abs(yuv["v"] - 128)

    # Đề xuất filter để MATCH màu gốc
    b_val = round(-brightness * 0.3, 3)   # bù brightness
    c_val = round(1.0 + abs(brightness) * 0.2, 2)
    s_val = round(1.0 + (saturation_hint / 100) * 0.5, 2)

    # Color channel mixer cho warmth/cool tone
    if warmth > 0.1:
        tone = "warm"
        cmx = f"colorchannelmixer=rr=1.05:gg=0.98:bb=0.9"
    elif warmth < -0.1:
        tone = "cool"
        cmx = f"colorchannelmixer=rr=0.9:gg=0.98:bb=1.05"
    else:
        tone = "neutral"
        cmx = ""

    eq_filter = f"eq=brightness={b_val}:contrast={c_val}:saturation={s_val}"
    full_filter = f"{eq_filter},{cmx}" if cmx else eq_filter

    return {
        "yuv": yuv,
        "brightness_level": round(brightness, 3),
        "warmth": round(warmth, 3),
        "tone": tone,
        "saturation_level": round(saturation_hint, 1),
        "suggested_ffmpeg_vf": full_filter,
        "reasoning": f"Y={yuv['y']:.0f}(bright={brightness:.2f}) U={yuv['u']:.0f} V={yuv['v']:.0f}(warm={warmth:.2f}) → {tone} tone"
    }


# ═══════════════════════════════════
# CLIP BUILDERS
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
        "status": "Nova Backend v5 Online",
        "knowledge_count": len(rules),
        "tutorial_count": sum(1 for r in rules if r.get("type") == "tutorial"),
        "style_count": sum(1 for r in rules if r.get("type") == "style")
    }

@app.post("/extract-frames")
async def api_extract_frames(video: UploadFile = File(...)):
    """Extract frames + audio từ video local"""
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    video_path = f"{out_dir}/input.mp4"
    with open(video_path,"wb") as f:
        f.write(await video.read())

    # Smart frames: scene change + đều nhau (tối đa 12)
    frames, duration = extract_smart_frames(video_path, f"{out_dir}/frames", max_frames=12)

    # Color analysis kỹ thuật thuần
    color_info = analyze_color(video_path, out_dir)

    # Extract audio
    audio_path = extract_audio(video_path, out_dir)
    audio_b64 = ""
    if audio_path:
        with open(audio_path,"rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()

    # Beat detection
    beat_info = detect_beats(audio_path) if audio_path else {}

    # Scene change timestamps (để app biết)
    scene_timestamps = [f["timestamp"] for f in frames if f.get("is_scene_change")]

    return {
        "job_id": job_id,
        "duration": duration,
        "frames": frames,
        "audio_b64": audio_b64,
        "has_audio": bool(audio_b64),
        "scene_changes": scene_timestamps,
        "scene_count": len(scene_timestamps),
        "color_analysis": color_info,
        "beat_info": beat_info,
        "smart_ffmpeg_vf": color_info.get("suggested_ffmpeg_vf",""),
    }

@app.post("/analyze-url")
async def analyze_url(url: str = Form(...)):
    """Download video từ URL + extract frames + audio"""
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

        frames, duration = extract_smart_frames(f"{out_dir}/video.mp4", f"{out_dir}/frames", max_frames=12)

        color_info = analyze_color(f"{out_dir}/video.mp4", out_dir)
        audio_path = extract_audio(f"{out_dir}/video.mp4", out_dir)
        audio_b64 = ""
        if audio_path:
            with open(audio_path,"rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()

        beat_info = detect_beats(audio_path) if audio_path else {}
        scene_timestamps = [f["timestamp"] for f in frames if f.get("is_scene_change")]

        return {
            "job_id": job_id,
            "duration": duration,
            "frames": frames,
            "audio_b64": audio_b64,
            "has_audio": bool(audio_b64),
            "scene_changes": scene_timestamps,
            "scene_count": len(scene_timestamps),
            "color_analysis": color_info,
            "beat_info": beat_info,
            "smart_ffmpeg_vf": color_info.get("suggested_ffmpeg_vf",""),
            "status": "ready"
        }
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

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
    delta = 1.0 if rating == "good" else -0.5
    rules = load_knowledge()
    for r in rules:
        if r.get("id") == rule_id:
            r["rating"] = round(r.get("rating", 0.0) + delta, 2)
            r["use_count"] = r.get("use_count", 0) + 1
            break
    save_knowledge_file(rules)
    return {"status":"updated","rule_id":rule_id,"delta":delta}

@app.delete("/knowledge/{rule_id}")
def delete_knowledge(rule_id: str):
    rules = load_knowledge()
    rules = [r for r in rules if r.get("id") != rule_id]
    save_knowledge_file(rules)
    return {"status":"deleted","remaining":len(rules)}

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

    # Ưu tiên: edit_plan.ffmpeg_vf > style.ffmpeg_vf > compose từ knowledge
    ai_vf = sanitize_vf(
        plan_data.get("ffmpeg_vf","") or style_data.get("ffmpeg_vf","")
    )
    if not ai_vf:
        rules = load_knowledge()
        if rules:
            ai_vf = compose_filter(rules).get("ffmpeg_vf","")

    dur = float(style_data.get("duration_per_image", plan_data.get("duration_per_clip", 3)))
    ts  = float(plan_data.get("trimStart", 0))
    te  = float(plan_data.get("trimEnd", 0))
    vf_str = build_vf(ai_vf)

    # Log filter đang dùng để debug
    print(f"[create-video] filter: {ai_vf or '(default)'}")
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
        return JSONResponse({"error":"Không tạo được clip"}, status_code=500)
    ok, err = concat_clips(inputs, output_path, out_dir)
    if not ok:
        return JSONResponse({"error":err}, status_code=500)
    return {
        "job_id": job_id,
        "status": "done",
        "download_url": f"/download/{job_id}",
        "filter_used": ai_vf,
        "clips_count": len(inputs),
    }

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
        "ffmpeg","-y","-i",input_path,"-vf",vf_str,
        "-c:v","libx264","-pix_fmt","yuv420p","-r","30","-preset","ultrafast",
        output_path
    ], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return JSONResponse({"error":r.stderr[-500:]}, status_code=500)
    return {"job_id":job_id,"status":"done","download_url":f"/download/{job_id}","filter_used":ai_vf}

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
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    output_path = f"{out_dir}/output.mp4"
    dur = float(duration_per_clip)
    ts  = float(trim_start)
    te  = float(trim_end)
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

@app.post("/compare")
async def compare_videos(
    tutorial: UploadFile = File(None),
    generated: UploadFile = File(None),
    tutorial_url: str = Form(""),
    generated_url: str = Form(""),
):
    """Extract frames từ cả 2 video, trả về để app gửi Gemini so sánh"""
    job_id = str(uuid.uuid4())[:8]
    out_dir = f"{WORK_DIR}/{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    result = {"job_id": job_id}

    # Tutorial video
    if tutorial and tutorial.filename:
        t_path = f"{out_dir}/tutorial.mp4"
        with open(t_path, "wb") as f:
            f.write(await tutorial.read())
        frames, dur = extract_frames(t_path, f"{out_dir}/t_frames")
        result["tutorial_frames"] = frames
        result["tutorial_duration"] = dur
    elif tutorial_url:
        sub = subprocess.run(
            ["yt-dlp","--no-playlist","-f","best[height<=480]/best",
             "-o", f"{out_dir}/tutorial.mp4", tutorial_url],
            capture_output=True, timeout=120
        )
        if sub.returncode == 0:
            frames, dur = extract_frames(f"{out_dir}/tutorial.mp4", f"{out_dir}/t_frames")
            result["tutorial_frames"] = frames
            result["tutorial_duration"] = dur

    # Generated video
    if generated and generated.filename:
        g_path = f"{out_dir}/generated.mp4"
        with open(g_path, "wb") as f:
            f.write(await generated.read())
        frames, dur = extract_frames(g_path, f"{out_dir}/g_frames")
        result["generated_frames"] = frames
        result["generated_duration"] = dur

    return result


@app.get("/download/{job_id}")
def download(job_id: str):
    path = f"{WORK_DIR}/{job_id}/output.mp4"
    if not os.path.exists(path):
        return JSONResponse({"error":"Not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="nova_output.mp4")
