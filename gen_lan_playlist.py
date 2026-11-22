#!/usr/bin/env python3
"""
Regenerate the served playlist t.m3u from tvlist-latest.m3u8.

Cloud-adapted version (2026-11-22) — drop-in replacement for the optiplex
original. Behaviour is identical except for how the #EXTM3U url-tvg header
is chosen:

  1. CLOUD_BASE_URL env var set (GitHub Actions / any cloud run)
       -> url-tvg = $CLOUD_BASE_URL/e.xml          (public, e.g. Pages)
  2. otherwise, LAN IP auto-detection as before (optiplex)
       -> url-tvg = http://<lan-ip>:6789/e.xml
  3. otherwise -> relative "e.xml"

The rest is unchanged:
- Copies the channel body of tvlist-latest.m3u8
- **Applies verified_urls.json**: keeps only channels that passed real ffmpeg
  decode verification, and re-applies the CCTV backup-source swaps. Without this
  the weekly automation would rebuild t.m3u from scratch and reintroduce ~170
  dead channels. Use --no-filter to emit the raw upstream list for re-auditing.
- **Applies verified_urls.json -> user_excluded** (added 2026-11-08): channels
  the user reported as 错台 / 卡顿 on the mobile dashboard are excluded by BOTH
  URL and exact display name, so a future source-URL change or a rebuilt
  whitelist cannot silently bring them back. Use --include-user-excluded to
  temporarily bypass this (restore / re-audit only).

Run this after update_tvlist.py (e.g. inside the weekly automation).
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
PORT = 6789
SRC = os.path.join(WORKSPACE, "tvlist-latest.m3u8")
DST = os.path.join(WORKSPACE, "t.m3u")
VERIFIED = os.path.join(WORKSPACE, "verified_urls.json")


def _ps_ips():
    # Windows only: Get-NetIPAddress. On Linux (GitHub Actions) powershell
    # does not exist -> returns [] and the socket fallback below is used.
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress"],
            text=True, stderr=subprocess.DEVNULL, timeout=15,
        )
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


def _socket_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return [ip] if ip else []
    except Exception:
        return []
    finally:
        s.close()


def detect_lan_ip():
    ips = _ps_ips() or _socket_ip()
    if not ips:
        return None
    # preference order: 192.168.68.* -> 192.168.* -> 10.* -> 172.16-31.*
    prefs = ("192.168.68.", "192.168.", "10.", "172.1", "172.2", "172.3")
    for pat in prefs:
        for ip in ips:
            if ip != "127.0.0.1" and ip.startswith(pat):
                return ip
    return None


def detect_epg_url():
    """CLOUD_BASE_URL wins; else LAN detection; else relative path."""
    cloud = os.environ.get("CLOUD_BASE_URL", "").strip().rstrip("/")
    if cloud:
        return f"{cloud}/e.xml", "cloud"
    ip = detect_lan_ip()
    if ip:
        return f"http://{ip}:{PORT}/e.xml", "lan"
    print("WARNING: no CLOUD_BASE_URL and no LAN IP detected; "
          "using relative EPG path 'e.xml'")
    return "e.xml", "relative"


def parse_entries(path):
    """Parse (extinf, extras, url) triples; tolerates #EXTVLCOPT/blank lines."""
    lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    out = []
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("#EXTINF"):
            url, j = None, i + 1
            while j < min(i + 6, len(lines)):
                s = lines[j].strip()
                if not s or s.startswith("#"):
                    j += 1
                    continue
                if s.startswith("http"):
                    url = s
                break
            if url:
                out.append({"extinf": l, "extras": lines[i + 1:j], "url": url})
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return out


def display_name(extinf):
    """Channel name after the last comma in #EXTINF (matches channel_status_data)."""
    return extinf.rpartition(",")[-1].strip()


def load_verified():
    if not os.path.exists(VERIFIED):
        return None
    d = json.load(open(VERIFIED, encoding="utf-8"))
    ex = d.get("user_excluded", [])
    return {
        "set": set(d["verified_urls"]),
        "swaps": {r["old"]: r["new"] for r in d.get("repairs", [])},
        # 频道名回退：上游每次刷新都会换 URL，只按 old-URL 键控会让整个 CCTV
        # 频道被静默丢出列表（2026-11-15 丢过 6 个）。名字命中且 URL 不在白名单时回退。
        "swaps_by_name": {r["name"]: r["new"] for r in d.get("repairs", [])
                          if r.get("name") and r.get("new")},
        "excluded_urls": {x["url"] for x in ex if x.get("url")},
        "excluded_names": {x["name"] for x in ex if x.get("name")},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-filter", action="store_true",
                    help="emit raw upstream list (for re-auditing), skip verification filter")
    ap.add_argument("--include-user-excluded", action="store_true",
                    help="ignore user_excluded entries (restore / re-audit only)")
    ap.add_argument("--output", default=DST)
    a = ap.parse_args()

    if not os.path.exists(SRC):
        print(f"ERROR: source not found: {SRC}")
        sys.exit(1)

    epg_url, mode = detect_epg_url()

    entries = parse_entries(SRC)
    v = None if a.no_filter else load_verified()

    kept, dropped, swapped, excluded, seen = [], 0, 0, 0, set()
    for e in entries:
        url = e["url"]
        if v:
            # 之前审计判定为死的 CCTV 源 -> 换成已验证可播的备用源
            if url in v["swaps"]:
                url = v["swaps"][url]
                swapped += 1
            elif url not in v["set"] and display_name(e["extinf"]) in v["swaps_by_name"]:
                url = v["swaps_by_name"][display_name(e["extinf"])]
                swapped += 1
            # 用户报告剔除：URL 或精确频道名命中都排除（防上游换 URL / 白名单重建回流）
            if not a.include_user_excluded and (
                url in v["excluded_urls"] or display_name(e["extinf"]) in v["excluded_names"]
            ):
                excluded += 1
                continue
            if url not in v["set"]:
                dropped += 1
                continue
            if url in seen:
                dropped += 1
                continue
            seen.add(url)
            e = dict(e, url=url)
        kept.append(e)

    # newline="\r\n": the optiplex original writes CRLF implicitly (Python's
    # default newline translation on Windows), and TVirl has only ever read
    # the CRLF file. Keep CRLF so cloud output is byte-identical to
    # production except for the intended url-tvg header change.
    with open(a.output, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(f'#EXTM3U url-tvg="{epg_url}"\n')
        f.write(f"#VERIFIED-FILTER: {len(kept)} channels passed ffmpeg decode; "
                f"{dropped} dropped, {swapped} backup swaps, "
                f"{excluded} user-excluded\n")
        for e in kept:
            f.write(e["extinf"] + "\n")
            for x in e["extras"]:
                f.write(x + "\n")
            f.write(e["url"] + "\n\n")

    print(f"OK: wrote {a.output}  ({len(kept)} channels, EPG -> {epg_url} [{mode}])")
    if v:
        print(f"    filter: dropped={dropped} backup-swaps={swapped} "
              f"user-excluded={excluded}")


if __name__ == "__main__":
    main()
