"""临时脚本：取 B 站音频直链，立刻交给 probe_media 测速。"""
import json
import re
import subprocess
import sys

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BV = sys.argv[1] if len(sys.argv) > 1 else "BV1Qy4y1M7ep"
PICK = int(sys.argv[2]) if len(sys.argv) > 2 else 30232  # 132kbps

s = requests.Session()
s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
s.get("https://www.bilibili.com/", timeout=20)
page = s.get(f"https://www.bilibili.com/video/{BV}/", timeout=20)
m = re.search(r"window\.__playinfo__\s*=\s*(\{.*?\})\s*</script>", page.text, re.S)
if not m:
    print("没有 __playinfo__，可能需要登录或改版")
    raise SystemExit(1)

data = json.loads(m.group(1))["data"]
dash = data.get("dash") or {}
audios = dash.get("audio") or []
print("可选音轨：", [(a["id"], a["bandwidth"]) for a in audios])

chosen = next((a for a in audios if a["id"] == PICK), audios[0])
url = chosen.get("baseUrl") or chosen.get("base_url")
print(f"选中 {chosen['id']} / {chosen['bandwidth']/1000:.0f} kbps")
print(f"URL 长度 {len(url)}")

referer = f"https://www.bilibili.com/video/{BV}/"
cmd = [
    sys.executable,
    "tools/probe_media.py",
    "--url", url,
    "--referer", referer,
    "--limit-mb", "30",
    "--workers", "6",
]
print("->", " ".join(cmd[:4]), "...")
raise SystemExit(subprocess.call(cmd))
