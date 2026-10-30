import json
import re

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BV = "BV1Qy4y1M7ep"
URL = f"https://www.bilibili.com/video/{BV}/"

s = requests.Session()
s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
# 先访问首页拿 buvid3，很多接口没有它会返回 -352
s.get("https://www.bilibili.com/", timeout=20)
r = s.get(URL, timeout=20)
t = r.text
print("page", r.status_code, len(t))
print("cookies:", sorted(s.cookies.get_dict().keys()))

for key in ["__playinfo__", "__INITIAL_STATE__", "playurl", '"cid"', "isLogin"]:
    i = t.find(key)
    print(f"{key:20} at {i}")

m = re.search(r'"cid":(\d+)', t)
print("cid:", m.group(1) if m else None)

m2 = re.search(r"window\.__playinfo__\s*=\s*(\{.*?\})\s*</script>", t, re.S)
print("playinfo:", bool(m2))
if m2:
    d = json.loads(m2.group(1))
    dash = (d.get("data") or {}).get("dash") or {}
    for a in (dash.get("audio") or [])[:5]:
        print("audio id", a.get("id"), "bw", a.get("bandwidth"), a.get("codecs"))
        print("   ", (a.get("baseUrl") or a.get("base_url") or "")[:120])
    print("keys:", list(d.keys()))
