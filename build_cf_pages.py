#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把局域网版播放列表打包成「Cloudflare Pages 可发布」的静态目录
==========================================================
来源:  t.m3u (gen_lan_playlist.py + cctv_refresh.py 的产物)
       e.xml (update_tvlist.py 抓取的 EPG)
产物:  cf_pages/
         ├── t.m3u       # url-tvg 头部改写成公网地址
         ├── e.xml       # EPG，原样拷贝
         └── index.html  # 极简落地页，用于人工确认部署是否成功

为什么需要这一步:
  t.m3u 的 #EXTM3U 头里写的是 http://192.168.68.115:8888/e.xml —— 内网地址，
  出了局域网 TVIRL 就取不到 EPG。发布前必须换成 Pages 的公网域名。

用法:
  python build_cf_pages.py --base-url https://tvlist.pages.dev
  python build_cf_pages.py --base-url https://tvlist.pages.dev --out cf_pages
  python build_cf_pages.py --probe                # 只探测 Pages 单文件 25MiB 限制
"""
import argparse
import html
import os
import re
import shutil
import sys
import time

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
SRC_M3U = os.path.join(WORKSPACE, "t.m3u")
SRC_EPG = os.path.join(WORKSPACE, "e.xml")

# Cloudflare Pages 免费版硬限制
PAGES_MAX_FILE_BYTES = 25 * 1024 * 1024        # 单个文件 25 MiB
PAGES_MAX_FILES = 20000                        # 文件数上限

RE_URG_TVG = re.compile(r'#EXTM3U(\s+url-tvg="[^"]*")?')


def count_channels(path):
    n = 0
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("#EXTINF"):
                n += 1
    return n


def rewrite_m3u(src, dst, base_url):
    """把 url-tvg 头换成公网 EPG 地址，其余字节原样保留。"""
    with open(src, encoding="utf-8") as f:
        lines = f.readlines()
    if not lines:
        raise RuntimeError(f"{src} 是空文件")

    first = lines[0]
    if not first.startswith("#EXTM3U"):
        raise RuntimeError(f"{src} 首行不是 #EXTM3U: {first[:60]!r}")

    old = first.strip()
    new = f'#EXTM3U url-tvg="{base_url.rstrip("/")}/e.xml"'
    lines[0] = new + "\n"

    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)

    return {"old": old, "new": new}


INDEX_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TV List</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:2.2rem 1.4rem; background:#0f1115; color:#e6e8eb;
         font:15px/1.65 -apple-system,"Segoe UI",system-ui,sans-serif; }}
  .wrap {{ max-width:680px; margin:0 auto; }}
  h1 {{ font-size:1.35rem; margin:0 0 .3rem; letter-spacing:.01em; }}
  .sub {{ color:#8b94a3; font-size:.86rem; margin-bottom:1.8rem; }}
  a.card {{ display:block; padding:.95rem 1.1rem; margin:.6rem 0; border-radius:10px;
           background:#171a21; border:1px solid #232833; text-decoration:none;
           color:#e6e8eb; transition:border-color .15s,background .15s; }}
  a.card:hover {{ border-color:#3d7dff; background:#1b1f28; }}
  .name {{ font-weight:600; }}
  .url {{ color:#8b94a3; font-size:.8rem; font-family:ui-monospace,Consolas,monospace;
         word-break:break-all; margin-top:.15rem; }}
  table {{ width:100%; border-collapse:collapse; margin-top:1.9rem; font-size:.86rem; }}
  td {{ padding:.42rem 0; border-bottom:1px solid #1e222b; }}
  td:last-child {{ text-align:right; color:#c7cdd8;
                   font-family:ui-monospace,Consolas,monospace; }}
  .ok {{ color:#3fbf7f; }}
  footer {{ margin-top:1.8rem; color:#5c6575; font-size:.76rem;
            font-family:ui-monospace,Consolas,monospace; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>TV List</h1>
  <div class="sub">Cloudflare Pages · 静态分发（不含代理，播放直连源站）</div>

  <a class="card" href="t.m3u">
    <div class="name">t.m3u</div>
    <div class="url">{base}/t.m3u</div>
  </a>
  <a class="card" href="e.xml">
    <div class="name">e.xml</div>
    <div class="url">{base}/e.xml</div>
  </a>

  <table>
    <tr><td>频道数</td><td class="ok">{channels}</td></tr>
    <tr><td>EPG 覆盖</td><td>{epg_channels}</td></tr>
    <tr><td>t.m3u 体积</td><td>{m3u_size}</td></tr>
    <tr><td>e.xml 体积</td><td>{epg_size}</td></tr>
    <tr><td>构建时间</td><td>{built_at}</td></tr>
  </table>

  <footer>{base}/t.m3u</footer>
</div>
</body>
</html>
"""


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://tvlist-3ac.pages.dev",
                    help="Pages 站点根地址，用于改写 url-tvg 与落地页。"
                         "注意：项目名是 tvlist，但 tvlist.pages.dev 已被他人占用，"
                         "CF 分配的实际子域是 tvlist-3ac；deploy_cf_pages.py 会自动覆盖此项")
    ap.add_argument("--out", default="cf_pages", help="输出目录")
    ap.add_argument("--src-m3u", default=SRC_M3U)
    ap.add_argument("--src-epg", default=SRC_EPG)
    ap.add_argument("--probe", action="store_true",
                    help="只检查体积是否超出 Pages 限制，不写文件")
    a = ap.parse_args()

    base = a.base_url.rstrip("/")
    out_dir = os.path.join(WORKSPACE, a.out)

    if not os.path.exists(a.src_m3u):
        print(f"ERROR: 找不到 {a.src_m3u}")
        return 1

    m3u_bytes = os.path.getsize(a.src_m3u)
    epg_bytes = os.path.getsize(a.src_epg) if os.path.exists(a.src_epg) else 0
    channels = count_channels(a.src_m3u)
    epg_channels = 0
    if epg_bytes:
        with open(a.src_epg, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "<channel id=" in line:
                    epg_channels += 1

    problems = []
    if m3u_bytes > PAGES_MAX_FILE_BYTES:
        problems.append(f"t.m3u {_human(m3u_bytes)} > 25 MiB")
    if epg_bytes > PAGES_MAX_FILE_BYTES:
        problems.append(f"e.xml {_human(epg_bytes)} > 25 MiB（需先裁剪频道数）")

    if a.probe:
        print(f"t.m3u   : {_human(m3u_bytes)}  ({channels} 频道)")
        print(f"e.xml   : {_human(epg_bytes)}  ({epg_channels} 频道 EPG)")
        print(f"限制    : 单文件 25 MiB / 共 {PAGES_MAX_FILES} 文件")
        print("结论    : " + ("超限 -> " + "; ".join(problems) if problems
                              else "全部在 Pages 限制内"))
        return 1 if problems else 0

    if problems:
        print("ERROR: " + "; ".join(problems))
        return 1

    # 全量重建，避免上一轮残留文件被一起上传
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    info = rewrite_m3u(a.src_m3u, os.path.join(out_dir, "t.m3u"), base)
    if epg_bytes:
        shutil.copy2(a.src_epg, os.path.join(out_dir, "e.xml"))

    built_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(out_dir, "index.html"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(INDEX_TMPL.format(
            base=html.escape(base), channels=channels,
            epg_channels=epg_channels, m3u_size=_human(m3u_bytes),
            epg_size=_human(epg_bytes), built_at=built_at,
        ))

    files = sorted(os.listdir(out_dir))
    total = sum(os.path.getsize(os.path.join(out_dir, x)) for x in files)

    print(f"OK: {a.out}/  ({len(files)} 文件, {_human(total)})")
    for x in files:
        print(f"    {x:12s} {_human(os.path.getsize(os.path.join(out_dir, x)))}")
    print(f"\nurl-tvg 头:")
    print(f"  - {info['old']}")
    print(f"  + {info['new']}")
    print(f"\n下一步: python deploy_cf_pages.py --project tvlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
