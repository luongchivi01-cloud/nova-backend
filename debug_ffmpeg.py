import subprocess, os

# Test FFmpeg convert ảnh giả → clip
test_dir = "/tmp/test_nova"
os.makedirs(test_dir, exist_ok=True)

# Tạo ảnh test bằng FFmpeg
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:size=100x100:duration=1",
    "-vframes", "1", f"{test_dir}/test.jpg"
], capture_output=True)

# Convert ảnh → clip
result = subprocess.run([
    "ffmpeg", "-y",
    "-loop", "1", "-i", f"{test_dir}/test.jpg",
    "-t", "3",
    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
    f"{test_dir}/clip.mp4"
], capture_output=True, text=True, timeout=60)

print("returncode:", result.returncode)
print("stderr:", result.stderr[-500:])
print("clip exists:", os.path.exists(f"{test_dir}/clip.mp4"))
if os.path.exists(f"{test_dir}/clip.mp4"):
    print("clip size:", os.path.getsize(f"{test_dir}/clip.mp4"))
