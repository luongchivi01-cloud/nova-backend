import re

fix = '''
        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
            inputs.append(clip_path)
'''

with open('main.py', 'r') as f:
    content = f.read()

# Fix cả 2 chỗ append
content = content.replace(
    "        if os.path.exists(clip_path):\n            inputs.append(clip_path)\n",
    "        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:\n            inputs.append(clip_path)\n"
)

with open('main.py', 'w') as f:
    f.write(content)

print("Done")
