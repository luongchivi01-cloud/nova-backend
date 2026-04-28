#!/bin/bash
# Tải ảnh test
curl -s "https://picsum.photos/200/300" -o /tmp/test.jpg
echo "Ảnh size: $(wc -c < /tmp/test.jpg) bytes"

# Test endpoint create-video
curl -X POST https://gentle-growth-production-b5e0.up.railway.app/create-video \
  -F "images=@/tmp/test.jpg;type=image/jpeg" \
  -F "style={\"duration_per_image\":3}" \
  -F "edit_plan={}" \
  -v 2>&1 | tail -30
