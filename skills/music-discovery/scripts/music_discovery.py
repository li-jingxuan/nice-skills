"""读取网易云榜单，输出可交给 music-download 的 JSON 数组。"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from html import unescape
from http.client import HTTPException
from urllib.request import urlopen


CHARTS = {"hot": ("3778678", "热歌榜"), "rising": ("19723756", "飙升榜")}
TOPLIST = "https://music.163.com/discover/toplist?id={}"
RSS = "https://rsshub.rssforever.com/163/music/playlist/{}"
TRACK_DATA = re.compile(r'<textarea\b[^>]*\bid="song-list-pre-data"[^>]*>(.*?)</textarea>', re.S)
UPDATED = re.compile(r"最近更新[：:]\s*(\d{1,2}月\d{1,2}日)")


def fetch(url):
    max_bytes = 2 * 1024 * 1024
    with urlopen(url, timeout=15) as response:
        content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("响应超过榜单大小上限。")
    return content.decode("utf-8-sig")


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("曲目缺少歌名或歌手。")
    return value.strip()


def official_tracks(page, limit):
    match = TRACK_DATA.search(page)
    if not match:
        raise ValueError("官网页面缺少曲目数据。")
    rows = json.loads(unescape(match[1]))
    if not isinstance(rows, list) or not rows:
        raise ValueError("官网没有返回有效的榜单曲目。")

    tracks = []
    for row in rows[:limit]:
        title = required_text(row.get("name"))
        artists = [required_text(artist.get("name")) for artist in row["artists"]]
        artist = required_text("/".join(artists))
        track = {"title": title, "artist": artist}
        album = (row.get("album") or {}).get("name")
        if isinstance(album, str) and album.strip():
            track["album"] = album.strip()
        tracks.append(track)
    return tracks


def rss_tracks(feed, name, limit):
    channel = ET.fromstring(feed).find("channel")
    if channel is None or channel.findtext("title") != name:
        raise ValueError("RSS 没有返回所请求的榜单。")
    items = channel.findall("item")
    if not items:
        raise ValueError("RSS 没有返回榜单曲目。")

    tracks = []
    for item in items[:limit]:
        label = required_text(item.findtext("title"))
        artist = required_text(item.findtext("author"))
        suffix = f" - {artist}"
        if label.endswith(suffix):
            title = label[:-len(suffix)]
        else:
            # RSSHub 会截短条目标题。仅在分隔符和歌手前缀都还在时恢复。
            title, separator, artist_tail = label.rpartition(" - ")
            if not (separator and artist_tail.endswith("...") and artist_tail[:-3]
                    and artist.startswith(artist_tail[:-3])):
                raise ValueError("RSS 条目缺少可核实的完整歌名。")
        tracks.append({"title": required_text(title), "artist": artist})
    return tracks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chart", choices=CHARTS, help="hot 为热歌榜，rising 为飙升榜")
    parser.add_argument("--limit", type=int, default=5, help="返回曲目数，默认 5")
    parser.add_argument("--rss", action="store_true", help="从第三方 RSS 订阅读取")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须是正整数。")

    chart_id, name = CHARTS[args.chart]
    url = (RSS if args.rss else TOPLIST).format(chart_id)
    try:
        content = fetch(url)
        if args.rss:
            tracks = rss_tracks(content, name, args.limit)
            source = f"网易云{name}（第三方 RSS）"
        else:
            tracks = official_tracks(content, args.limit)
            updated = UPDATED.search(content)
            source = f"网易云{name}" + (f"（官网最近更新：{updated[1]}）" if updated else "")
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as exc:
        print(f"读取网易云{name}失败（{url}）：{exc}", file=sys.stderr)
        return 1

    print(f"{source} · {url}", file=sys.stderr)
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(tracks, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
