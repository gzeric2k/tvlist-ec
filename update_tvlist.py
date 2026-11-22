#!/usr/bin/env python3
"""
TV List Updater — Personal IPTV Channel Manager
=================================================
Orchestration pipeline:
  1. Load user's channel config (my_channels.json)
  2. Parse master source files (Selected-Channels, cn_all, au_sydney, etc.)
  3. Match channels against user config → build target list
  4. Quick HTTP re-verify all matched streams
  5. (Optional) Deep Playwright verify for borderline streams
  6. Generate personalized tvlist M3U8

Usage:
  python update_tvlist.py                    # Full update
  python update_tvlist.py --quick            # Skip re-verification (use cached status)
  python update_tvlist.py --playwright       # Deep verify with Playwright
  python update_tvlist.py --dry-run          # Show what would be included without writing
  python update_tvlist.py --output mylist.m3u8  # Custom output file
"""

import re
import sys
import os
import json
import time
import asyncio
import argparse
from collections import defaultdict
from datetime import date, datetime

# Windows encoding fix
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Paths ─────────────────────────────────────────────────────
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(WORKSPACE, "my_channels.json")
MASTER_M3U8 = None  # Will find the latest Selected-Channels-*.m3u8
CN_SOURCE = os.path.join(WORKSPACE, "cn_all_status.m3u8")
AU_SOURCE = os.path.join(WORKSPACE, "au_sydney.m3u8")
HK_SOURCE = os.path.join(WORKSPACE, "free_tv_hongkong.m3u8")

# HTTP verification settings
HTTP_CONCURRENCY = 20
HTTP_TIMEOUT_SEC = 8

# ── Updated source URLs (for fetching) ───────────────────────
SOURCE_URLS = {
    "cn_all.m3u8": "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all.m3u8",
    "cn_all_status.m3u8": "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all_status.m3u8",
    "au_sydney.m3u8": "https://i.mjh.nz/au/Sydney/kodi-tv.m3u8",
}


# ══════════════════════════════════════════════════════════════════
#  STEP 1a: Fetch latest sources (optional)
# ══════════════════════════════════════════════════════════════════
def fetch_sources():
    """Download latest M3U8 source files from upstream repositories."""
    import urllib.request

    print("\n[Fetch] Downloading latest source files...")
    downloaded = 0
    for filename, url in SOURCE_URLS.items():
        dest = os.path.join(WORKSPACE, filename)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read()
                # Check it's actually an M3U8 (not HTML error page)
                if content[:20].startswith(b'#EXTM3U') or content[:20].startswith(b'#EXTINF'):
                    with open(dest, 'wb') as f:
                        f.write(content)
                    print(f"  ✅ {filename} ({len(content)} bytes)")
                    downloaded += 1
                else:
                    # Might be an error page; skip
                    print(f"  ⚠️  {filename}: unexpected content (not M3U8), skipped")
        except Exception as e:
            print(f"  ❌ {filename}: {str(e)[:50]}")

    print(f"  Downloaded {downloaded}/{len(SOURCE_URLS)} files")
    return downloaded


# ══════════════════════════════════════════════════════════════════
#  STEP 1b: EPG (Electronic Program Guide) support
# ══════════════════════════════════════════════════════════════════

EPG_CACHE_DIR = os.path.join(WORKSPACE, ".epg_cache")
EPG_MAPPING_FILE = os.path.join(EPG_CACHE_DIR, "epg_mapping.json")

def download_epg_sources(config):
    """Download EPG XMLTV files from configured sources and build channel mapping."""
    epg_config = config.get("epg", {})
    if not epg_config.get("enabled", False):
        return {}, []

    sources = epg_config.get("sources", [])
    if not sources:
        return {}, []

    os.makedirs(EPG_CACHE_DIR, exist_ok=True)
    print("\n[EPG] Downloading EPG data...")

    source_urls = []  # URLs for M3U8 url-tvg header

    for src in sources:
        name = src.get("name", "unknown")
        url = src.get("url", "")
        src_type = src.get("type", "xml")

        if not url:
            continue

        cache_file = os.path.join(EPG_CACHE_DIR, f"epg_{name}.xml")
        source_urls.append(url)

        try:
            import urllib.request
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_data = resp.read()

            # Decompress if gzipped
            if src_type == "gzip":
                import gzip
                try:
                    xml_data = gzip.decompress(raw_data)
                except Exception:
                    xml_data = raw_data
            else:
                xml_data = raw_data

            # Quick sanity check
            if b"<tv>" in xml_data[:200] or b"<channel " in xml_data[:2000]:
                with open(cache_file, 'wb') as f:
                    f.write(xml_data)
                print(f"  ✅ {name}: EPG cached ({len(xml_data)} bytes)")
            else:
                print(f"  ⚠️  {name}: not valid XMLTV, skipped")
                source_urls.pop()  # Remove invalid URL
        except Exception as e:
            print(f"  ❌ {name}: {str(e)[:60]}")
            source_urls.pop()

    # Build channel mapping from cached files
    mapping = build_epg_mapping()
    print(f"  📊 EPG channels mapped: {len(mapping)}")

    # Merge all EPG sources into a single file for easy serving
    merge_epg_files()

    return mapping, source_urls


def build_epg_mapping():
    """Parse cached EPG XML files into a {normalized_name: tvg_id} lookup."""
    import xml.etree.ElementTree as ET

    mapping = {}

    if not os.path.exists(EPG_CACHE_DIR):
        return mapping

    for fname in os.listdir(EPG_CACHE_DIR):
        if not fname.startswith("epg_") or not fname.endswith(".xml"):
            continue
        if fname == "epg_mapping.json":
            continue

        filepath = os.path.join(EPG_CACHE_DIR, fname)
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()

            for ch in root.findall(".//channel"):
                ch_id = ch.get("id", "")
                display_name = ch.findtext("display-name", default="")
                if ch_id and display_name:
                    key = display_name.strip().lower()
                    # Only keep first occurrence per key
                    if key not in mapping:
                        mapping[key] = ch_id
        except Exception as e:
            print(f"  ⚠️  Error parsing {fname}: {e}")

    # Save combined mapping for fast future lookups
    try:
        with open(EPG_MAPPING_FILE, 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return mapping


def merge_epg_files():
    """Merge all cached EPG XML files into a single e.xml in workspace."""
    import xml.etree.ElementTree as ET
    from xml.etree.ElementTree import Element
    import copy

    if not os.path.exists(EPG_CACHE_DIR):
        return

    cached_files = sorted([
        os.path.join(EPG_CACHE_DIR, f)
        for f in os.listdir(EPG_CACHE_DIR)
        if f.startswith("epg_") and f.endswith(".xml") and f != "epg_mapping.json"
    ])

    if not cached_files:
        return

    merged = Element("tv", attrib={"generator-info-name": "tvlist-updater-merge"})
    seen_ids = set()
    ch_count = 0
    prog_count = 0

    for cf in cached_files:
        try:
            tree = ET.parse(cf)
            root = tree.getroot()
            for ch in root.findall(".//channel"):
                ch_id = ch.get("id", "")
                if ch_id and ch_id not in seen_ids:
                    seen_ids.add(ch_id)
                    merged.append(copy.deepcopy(ch))
                    ch_count += 1
            for prog in root.findall(".//programme"):
                merged.append(copy.deepcopy(prog))
                prog_count += 1
        except Exception as e:
            print(f"  \u26a0\ufe0f  Error merging {os.path.basename(cf)}: {e}")

    output_path = os.path.join(WORKSPACE, "e.xml")
    tree = ET.ElementTree(merged)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    print(f"  \ud83d\udcca Merged EPG: {ch_count} channels, {prog_count} programmes ({os.path.getsize(output_path)//1024//1024}MB)")


def load_cached_epg():
    """Load cached EPG mapping if it exists."""
    if not os.path.exists(EPG_MAPPING_FILE):
        return {}, []
    try:
        with open(EPG_MAPPING_FILE, 'r', encoding='utf-8') as f:
            mapping = json.load(f)
        # Reconstruct source URLs from mapping metadata (embedded in filename)
        # For simplicity, return mapping with empty URL list; URLs will be
        # loaded from config at generation time
        return mapping, []
    except Exception:
        return {}, []


def lookup_tvg_id(channel_name, epg_mapping):
    """Find the best tvg-id for a channel name (fuzzy matching)."""
    if not epg_mapping:
        return None

    # Remove status/resolution tags
    clean = re.sub(r'\s*\[(OK|\?)\]', '', channel_name).strip()
    clean = re.sub(r'\s*\(.*?p?\d*\)\s*$', '', clean).strip()

    normalized = clean.lower().strip()

    # 1) Direct match
    if normalized in epg_mapping:
        return epg_mapping[normalized]

    # 2) Remove dash/variants
    variants = [
        normalized.replace("-", ""),
        normalized.replace(" ", ""),
        normalized.replace("-", " "),
    ]
    for v in variants:
        if v in epg_mapping:
            return epg_mapping[v]

    # 3) Substring match
    for key, tvg_id in epg_mapping.items():
        if normalized in key or key in normalized:
            return tvg_id

    return None


# ══════════════════════════════════════════════════════════════════
#  STEP 1: Load user configuration
# ══════════════════════════════════════════════════════════════════
def load_config():
    """Load my_channels.json, return parsed dict."""
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Config file not found: {CONFIG_FILE}")
        print("   Create my_channels.json first (see template).")
        sys.exit(1)

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config


# ══════════════════════════════════════════════════════════════════
#  STEP 2: Parse M3U8 files
# ══════════════════════════════════════════════════════════════════
def parse_m3u8(filepath):
    """
    Parse an M3U8 file and return a list of channel entries.
    Each entry: {name, group, resolution, latency, url, status}
    """
    entries = []
    if not os.path.exists(filepath):
        return entries

    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            inf_line = line

            # Extract channel name (after last comma)
            name_match = re.search(r',(.+)$', inf_line)
            channel_name = name_match.group(1).strip() if name_match else "Unknown"

            # Extract group-title
            group_match = re.search(r'group-title="([^"]*)"', inf_line)
            group = group_match.group(1) if group_match else "未分类"

            # Extract response-time
            rt_match = re.search(r'response-time="([^"]*)"', inf_line)
            latency = rt_match.group(1) if rt_match else "N/A"

            # Extract resolution
            res_match = re.search(r'\[(\d+[KkPp]?)\]', inf_line)
            resolution = res_match.group(1) if res_match else "N/A"

            # Extract OK/UNVERIFIED status (from status="OK" attribute or [OK] suffix)
            status = "unknown"
            status_match = re.search(r'status="([^"]+)"', inf_line)
            if status_match:
                raw = status_match.group(1).upper()
                status = "ok" if raw == "OK" else "unverified"
            elif '[OK]' in channel_name or '[OK]' in inf_line:
                status = "ok"

            # Skip blank lines and comments to find the URL
            url = None
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if not next_line:
                    j += 1
                    continue
                if next_line.startswith('#'):
                    j += 1
                    continue
                if next_line.startswith('http://') or next_line.startswith('https://'):
                    url = next_line
                    break
                j += 1

            if url:
                entries.append({
                    "name": channel_name,
                    "group": group,
                    "resolution": resolution,
                    "latency": latency,
                    "url": url,
                    "status": status,
                })
                i = j + 1
                continue
        i += 1

    return entries


def find_master_m3u8():
    """Find the latest Selected-Channels file."""
    pattern = re.compile(r'Selected-Channels-(\d{4}-\d{2}-\d{2})\.m3u8')
    candidates = []
    for f in os.listdir(WORKSPACE):
        m = pattern.match(f)
        if m:
            candidates.append((m.group(1), f))
    candidates.sort(reverse=True)
    if candidates:
        return os.path.join(WORKSPACE, candidates[0][1])
    return None


def load_all_sources():
    """Load and merge all available source files."""
    all_entries = []

    # 1) Master Selected-Channels file (most comprehensive)
    master_path = find_master_m3u8()
    if master_path:
        entries = parse_m3u8(master_path)
        print(f"  📋 Master ({os.path.basename(master_path)}): {len(entries)} entries")
        all_entries.extend(entries)
    else:
        print("  ⚠️  No Selected-Channels file found")

    # 2) cn_all_status (raw source with status tags)
    if os.path.exists(CN_SOURCE):
        entries = parse_m3u8(CN_SOURCE)
        print(f"  🇨🇳 CN source: {len(entries)} entries")
        all_entries.extend(entries)

    # 3) AU source
    if os.path.exists(AU_SOURCE):
        entries = parse_m3u8(AU_SOURCE)
        print(f"  🇦🇺 AU source: {len(entries)} entries")
        all_entries.extend(entries)

    # Deduplicate (keep the first occurrence of each URL)
    seen_urls = set()
    deduped = []
    for e in all_entries:
        if e["url"] not in seen_urls:
            seen_urls.add(e["url"])
            deduped.append(e)

    print(f"  📊 Total unique entries: {len(deduped)} (deduped from {len(all_entries)})")
    return deduped


# ══════════════════════════════════════════════════════════════════
#  STEP 3: Match against user config
# ══════════════════════════════════════════════════════════════════
# ── Group name mapping (source group names → config category names) ──
GROUP_ALIASES = {
    "央视频道": "CCTV",
    "央视台": "CCTV",
    "体育频道": "Sports",
    "卫视频道": "卫视",
    "其他频道": "__other__",
    "湖南台": "__other__",
}


def map_group(group_name):
    """Map source group name to config category name."""
    if group_name in GROUP_ALIASES:
        return GROUP_ALIASES[group_name]
    return group_name


def match_channels(entries, config):
    """
    Filter entries based on user's my_channels.json config.
    Returns list of selected entries with their source group info.
    """
    categories = config.get("categories", {})
    selected = []
    matched_groups = defaultdict(list)
    unmatched = defaultdict(list)

    # Build lookup: entry name (normalized) → entry
    # Also normalize channel names for fuzzy matching
    def normalize_name(name):
        """Normalize for matching: strip [OK]/[?] suffixes, resolution tags."""
        n = name
        # Remove [OK] / [?] tags
        n = re.sub(r'\s*\[(OK|\?)\]', '', n)
        # Remove resolution tags
        n = re.sub(r'\s*\(.*?\)', '', n)
        n = re.sub(r'\s*\[.*?\]', '', n)
        return n.strip().lower()

    # Build name → entries map (normalized)
    name_map = defaultdict(list)
    for e in entries:
        key = normalize_name(e["name"])
        name_map[key].append(e)

    # For each category in config
    for cat_name, cat_config in categories.items():
        enabled = cat_config.get("enabled", True)
        if not enabled:
            # Even if disabled, check custom_include
            custom_include = cat_config.get("custom_include", [])
            if not custom_include:
                continue

        # Build list of allowed channel names for this category
        exclude = [x.lower() for x in cat_config.get("exclude", [])]
        custom_include = [x.lower() for x in cat_config.get("custom_include", [])]

        # Find all entries matching this category (using both direct match and aliases)
        cat_entries = [e for e in entries if
                       e["group"] == cat_name or
                       map_group(e["group"]) == cat_name or
                       e["group"].lower() == cat_name.lower() or
                       map_group(e["group"]).lower() == cat_name.lower()]

        if not cat_entries:
            # Try partial group match (e.g. "AU TV" within a larger group)
            cat_entries = [e for e in entries if
                           cat_name.lower() in e["group"].lower() or
                           cat_name.lower() in map_group(e["group"]).lower()]

        # If still nothing, skip
        if not cat_entries:
            print(f"  ⚠️  Category '{cat_name}': no matching entries found in sources")
            continue

        # Group entries by normalized name
        cat_by_name = defaultdict(list)
        for e in cat_entries:
            key = normalize_name(e["name"])
            cat_by_name[key].append(e)

        # Select channels
        for norm_name, entries_list in cat_by_name.items():
            # Get a human-readable name
            display_name = entries_list[0]["name"]

            # Check if in exclude list
            is_excluded = any(excl in norm_name for excl in exclude)

            # If category is enabled, include by default
            # If disabled, only include if in custom_include
            if enabled and not is_excluded:
                selected.extend(entries_list)
                matched_groups[cat_name].append(display_name)
            elif not enabled and any(incl in norm_name for incl in custom_include):
                selected.extend(entries_list)
                matched_groups[cat_name].append(display_name)
            elif enabled and is_excluded:
                unmatched[cat_name].append(f"(excluded) {display_name}")
            else:
                unmatched[cat_name].append(f"(disabled) {display_name}")

    # Also handle entries not in any configured category
    configured_groups = set(categories.keys())
    configured_mapped = set(map_group(g) for g in configured_groups)
    selected_urls = set(e["url"] for e in selected)

    for e in entries:
        if e["url"] in selected_urls:
            continue
        eg = e["group"]
        eg_mapped = map_group(eg)
        if eg in configured_groups or eg_mapped in configured_groups:
            continue
        if eg.lower() in configured_mapped or eg_mapped.lower() in configured_mapped:
            continue
        # Check if name matches any custom_include across all categories
        norm = normalize_name(e["name"])
        found = False
        for cat_config in categories.values():
            if any(incl in norm for incl in [x.lower() for x in cat_config.get("custom_include", [])]):
                selected.append(e)
                selected_urls.add(e["url"])
                found = True
                break
        if not found:
            unmatched["__other__"].append(e["name"])

    # Print summary
    total_selected = len(set(e["url"] for e in selected))
    print(f"\n  ✅ Selected: {total_selected} channels from {len(matched_groups)} categories")

    for g, names in sorted(matched_groups.items()):
        print(f"     {g}: {len(names)} channels")

    if unmatched:
        total_unmatched = sum(len(v) for v in unmatched.values())
        print(f"  ⏭️  Skipped/Unmatched: {total_unmatched} channels")

    return selected


# ══════════════════════════════════════════════════════════════════
#  STEP 4: HTTP re-verify (optional but recommended)
# ══════════════════════════════════════════════════════════════════
async def http_verify_stream(session, url, timeout=HTTP_TIMEOUT_SEC):
    """Quick HTTP HEAD/GET check."""
    import aiohttp
    result = {"url": url, "status": False, "latency_ms": None, "error": None}

    try:
        start = time.monotonic()
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                allow_redirects=True, ssl=False) as resp:
            elapsed = int((time.monotonic() - start) * 1000)
            if resp.status < 400:
                result["status"] = True
                result["latency_ms"] = elapsed
                result["http_status"] = resp.status
            else:
                # HEAD failed, try GET with range
                result["error"] = f"HTTP {resp.status}"
    except Exception as e:
        result["error"] = str(type(e).__name__)

    if not result["status"]:
        # Fallback: GET with Range header
        headers = {"Range": "bytes=0-1024",
                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            start = time.monotonic()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                   headers=headers, allow_redirects=True, ssl=False) as resp:
                elapsed = int((time.monotonic() - start) * 1000)
                if resp.status < 400:
                    result["status"] = True
                    result["latency_ms"] = elapsed
                    result["http_status"] = resp.status
                    result["error"] = None
        except Exception as e:
            if not result["error"]:
                result["error"] = str(type(e).__name__)

    return result


async def re_verify(entries, concurrency=HTTP_CONCURRENCY):
    """Re-verify all URLs with aiohttp."""
    import aiohttp

    print(f"\n[3/4] Re-verifying {len(entries)} streams with aiohttp...")

    unique_urls = list(set(e["url"] for e in entries))
    print(f"  Unique URLs to check: {len(unique_urls)}")

    connector = aiohttp.TCPConnector(limit=concurrency, force_close=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(concurrency)

        async def test_one(url):
            async with sem:
                return await http_verify_stream(session, url)

        tasks = [test_one(url) for url in unique_urls]
        results = await asyncio.gather(*tasks)

    # Build URL → result lookup
    url_results = {r["url"]: r for r in results}

    working = sum(1 for r in results if r["status"])
    failed = sum(1 for r in results if not r["status"])
    print(f"  ✅ {working} working  |  ❌ {failed} failed  |  ⏱️  {HTTP_TIMEOUT_SEC}s timeout")

    return url_results


# ══════════════════════════════════════════════════════════════════
#  STEP 5: Generate personalized M3U8
# ══════════════════════════════════════════════════════════════════
def generate_m3u8(entries, config, url_results=None, output_path="tvlist.m3u8",
                  epg_mapping=None, epg_source_urls=None):
    """
    Generate a clean personalized M3U8 file.
    For each channel, pick the best available stream (prefer verified, 1080p, low latency).
    Optionally includes EPG (Electronic Program Guide) data.
    """
    from collections import OrderedDict

    output_filename = config.get("output_filename", "tvlist")
    today = date.today().strftime("%Y-%m-%d")
    if not output_path:
        output_path = os.path.join(WORKSPACE, f"{output_filename}-{today}.m3u8")

    def score_source(e):
        """Score a source for best-selection."""
        score = 0
        # Resolution score
        res = (e.get("resolution") or "N/A").lower().replace('p', '')
        if '4k' in res or '2160' in res:
            score += 120
        elif '1080' in res:
            score += 100
        elif '720' in res:
            score += 70
        elif '576' in res:
            score += 50
        elif '480' in res or '540' in res:
            score += 35
        elif '360' in res:
            score += 20
        else:
            score += 10

        # Verification score
        if url_results:
            vr = url_results.get(e["url"])
            if vr and vr["status"]:
                score += 200
                # Bonus for low latency
                if vr.get("latency_ms") and vr["latency_ms"] < 500:
                    score += 100
                elif vr.get("latency_ms") and vr["latency_ms"] < 1000:
                    score += 50
            elif vr and not vr["status"]:
                score -= 500  # Heavily penalize failed streams

        # Existing status bonus
        if e.get("status") == "ok":
            score += 50

        return score

    # Group by channel name (normalized)
    # Known dead IPs to block
    DEAD_IPS = ["111.8.224.6"]

    channels = defaultdict(list)
    for e in entries:
        # Skip entries from known dead sources
        if any(ip in e["url"] for ip in DEAD_IPS):
            continue

        # Clean name for grouping: remove [OK]/[?] and resolution tags
        clean_name = re.sub(r'\s*\[(OK|\?)\]', '', e["name"]).strip()
        # Also remove duplicate names from resolution variants
        base_name = re.sub(r'\s*\(.*?p?\d*\)\s*$', '', clean_name).strip()

        # Strip common Chinese suffixes for better dedup (CCTV-3综艺 → CCTV-3)
        for suffix in ["综艺","电影","戏曲","新闻","少儿","音乐",
                       "农业农村","社会与法","中文国际","纪录","体育",
                       "电视剧","戏曲","财经","科教","娱乐","国防军事"]:
            if base_name.endswith(suffix) and len(base_name) > len(suffix) + 2:
                stripped = base_name[:-len(suffix)].strip()
                if '-' in stripped or re.match(r'^[A-Za-z]+-\d+', stripped):
                    base_name = stripped
            # Also handle with space prefix (CCTV-4 中文国际)
            spaced = f" {suffix}"
            if base_name.endswith(spaced) and len(base_name) > len(suffix) + 3:
                stripped = base_name[:-len(spaced)].strip()
                if '-' in stripped or re.match(r'^[A-Za-z]+-\d+', stripped):
                    base_name = stripped

        channels[base_name].append(e)

    # Select best source per channel
    selected = OrderedDict()
    for base_name in sorted(channels.keys()):
        sources = channels[base_name]
        best = max(sources, key=score_source)
        selected[base_name] = best

    # Write M3U8
    header_entries = len(selected)
    working_count = sum(1 for e in selected.values()
                        if url_results and url_results.get(e["url"], {}).get("status"))

    # Count EPG match rate
    epg_matched = 0
    if epg_mapping:
        for base_name in selected:
            if lookup_tvg_id(base_name, epg_mapping):
                epg_matched += 1

    with open(output_path, 'w', encoding='utf-8') as f:
        # ── Header with EPG link ──
        if epg_mapping:
            # Use relative path to merged EPG file (deployed alongside M3U8)
            f.write('#EXTM3U url-tvg="e.xml"\n')
        else:
            f.write("#EXTM3U\n")

        f.write(f"#PLAYLIST: Personalized TV List - {today}\n")
        f.write(f"#SOURCE: best-fan/iptv-sources + i.mjh.nz + iptv-org + imDazui\n")
        f.write(f"#CHANNELS: {header_entries} total")
        if url_results:
            f.write(f" ({working_count} verified working)")
        if epg_mapping:
            f.write(f" | EPG: {epg_matched}/{header_entries} matched")
        f.write("\n")
        f.write(f"#LAST_UPDATED: {today}\n")
        f.write(f"#GENERATED_BY: update_tvlist.py\n")
        if epg_mapping:
            merged_path = os.path.join(WORKSPACE, "e.xml")
            if os.path.exists(merged_path):
                f.write(f"#EPG_SOURCES: e.xml (merged EPG, {os.path.getsize(merged_path)//1024//1024}MB)\n")
        f.write("\n")

        for base_name, e in selected.items():
            group = e["group"]
            url = e["url"]

            # Determine status tag
            status_tag = ""
            if url_results:
                vr = url_results.get(url)
                if vr and vr["status"]:
                    latency = vr.get("latency_ms", "?")
                    status_tag = f' response-time="{latency}ms" status="OK"'
                else:
                    status_tag = ' status="UNVERIFIED"'

            # Look up EPG tvg-id
            epg_attrs = ""
            if epg_mapping:
                tvg_id = lookup_tvg_id(base_name, epg_mapping)
                if tvg_id:
                    epg_attrs = f' tvg-id="{tvg_id}"'

            f.write(f'#EXTINF:-1 tvg-name="{base_name}"{epg_attrs} tvg-logo="" group-title="{group}"{status_tag},{base_name}\n')
            f.write(f"{url}\n")
            f.write("\n")

    print(f"\n  📺 Output: {output_path}")
    print(f"     Channels: {header_entries}")
    if url_results:
        print(f"     Verified working: {working_count}/{header_entries}")

    return output_path


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="Personal IPTV Channel Manager — update your tvlist")
    parser.add_argument("--fetch-sources", action="store_true",
                        help="Download latest source files from upstream before updating")
    parser.add_argument("--fetch-epg", action="store_true",
                        help="Force refresh EPG data")
    parser.add_argument("--no-epg", action="store_true",
                        help="Skip EPG even if enabled in config")
    parser.add_argument("--quick", action="store_true",
                        help="Skip HTTP re-verification (use cached status)")
    parser.add_argument("--playwright", action="store_true",
                        help="Use Playwright for deep verification (slower but more accurate)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be included without writing output")
    parser.add_argument("--output",
                        help="Custom output M3U8 path")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed matching info")

    args = parser.parse_args()

    banner = f"""
╔══════════════════════════════════════════════╗
║     📺 TV List Updater                       ║
║     {datetime.now().strftime('%Y-%m-%d %H:%M')}                    ║
╚══════════════════════════════════════════════╝
"""
    print(banner)

    # ── Step 0: Fetch sources (optional) ──
    if args.fetch_sources:
        fetch_sources()

    # ── Step 1: Load config ──
    print("[1/4] Loading user config...")
    config = load_config()
    cat_count = len(config.get("categories", {}))
    print(f"  ✅ Config loaded: {cat_count} categories defined")

    # ── Optional: EPG download ──
    epg_mapping = {}
    epg_source_urls = []
    epg_enabled = config.get("epg", {}).get("enabled", False) and not args.no_epg

    if epg_enabled and not args.dry_run:
        # Check if we need to refresh cached EPG
        cache_file = EPG_MAPPING_FILE
        needs_refresh = args.fetch_epg or not os.path.exists(cache_file)

        if needs_refresh:
            epg_mapping, epg_source_urls = download_epg_sources(config)
        else:
            # Check cache freshness (respect ttl_hours)
            cache_age = 999
            try:
                cache_age = (time.time() - os.path.getmtime(cache_file)) / 3600
            except:
                pass
            ttl = config.get("epg", {}).get("ttl_hours", 24)

            if cache_age > ttl:
                epg_mapping, epg_source_urls = download_epg_sources(config)
            else:
                epg_mapping, _ = load_cached_epg()
                # Get URLs from config
                epg_source_urls = [s["url"] for s in config.get("epg", {}).get("sources", [])]
                print(f"\n[EPG] Using cached EPG ({int(cache_age)}h old, TTL {ttl}h)")
                print(f"  📊 EPG channels mapped: {len(epg_mapping)}")

    elif epg_enabled and args.dry_run:
        # Try to load cached EPG even in dry-run (for stats)
        epg_mapping, _ = load_cached_epg()
        epg_source_urls = [s["url"] for s in config.get("epg", {}).get("sources", [])]
        if epg_mapping:
            print(f"\n[EPG] Using cached EPG mapping ({len(epg_mapping)} channels)")

    # ── Step 2: Load sources ──
    print("\n[2/4] Loading channel sources...")
    all_entries = load_all_sources()

    # ── Step 3: Match ──
    print("\n[3/4] Matching against your channel config...")
    selected_entries = match_channels(all_entries, config)

    if not selected_entries:
        print("\n❌ No channels matched your config. Check my_channels.json.")
        sys.exit(1)

    # ── Step 4: Re-verify (optional) ──
    url_results = None
    if not args.quick and not args.dry_run:
        url_results = asyncio.run(re_verify(selected_entries))
    elif args.quick:
        print("\n[4/4] Skipping re-verification (--quick mode)")

    # ── Step 5: Generate output ──
    if not args.dry_run:
        print("\n[4/4] Generating personalized TV list...")
        output_file = generate_m3u8(selected_entries, config, url_results,
                                    args.output, epg_mapping, epg_source_urls)

        stats = {
            "last_updated": date.today().strftime("%Y-%m-%d"),
            "total_entries": len(set(e["url"] for e in selected_entries)),
            "channels_in_output": None,  # will be calculated
        }

        # Count unique channels in output
        if output_file:
            with open(output_file, 'r', encoding='utf-8') as f:
                channel_count = sum(1 for line in f if line.startswith('#EXTINF:'))
            stats["channels_in_output"] = channel_count

        print(f"\n  {'='*50}")
        print(f"  ✅ Update complete!")
        print(f"  {'='*50}")
        print(f"     Source entries: {len(all_entries)}")
        if url_results:
            working = sum(1 for r in url_results.values() if r["status"])
            print(f"     Verified working: {working}/{len(url_results)}")
        print(f"     Output channels: {stats['channels_in_output']}")
        print(f"     File: {output_file}")
    else:
        # Dry-run: just show what would be included
        unique_names = sorted(set(
            re.sub(r'\s*\[(OK|\?)\]', '', e["name"]).strip()
            for e in selected_entries
        ))
        print(f"\n  📋 Dry run — would include {len(unique_names)} channels:")
        for name in unique_names:
            print(f"     ✅ {name}")
        print(f"\n  Run without --dry-run to generate the M3U8 file.")


if __name__ == "__main__":
    main()
