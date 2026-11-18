"""把各平台的匿名歌词接口转换为统一的候选信息。"""

import base64
import html
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOURCES = ("lrclib", "netease", "qq", "kugou")
LRCLIB = "https://lrclib.net/api"
NETEASE = "https://music.163.com/api"
QQ = "https://c.y.qq.com"
KUGOU = "https://lyrics.kugou.com"


def get_json(url, **params):
    headers = {"Accept": "application/json", "User-Agent": "music-download/1.0"}
    if url.startswith(QQ + "/"):
        headers["Referer"] = "https://y.qq.com/"
    request = Request(url + "?" + urlencode(params), headers=headers)
    with urlopen(request, timeout=7) as response:
        return json.load(response)


def checked(data, field, expected):
    if not isinstance(data, dict) or data.get(field) != expected:
        raise ValueError("歌词来源返回异常状态。")
    return data


def candidate(id_, title, artist, album, duration, **extra):
    return {"id": str(id_ or ""), "title": title or "", "artist": artist or "",
            "album": album or "", "duration": duration, **extra}


def search(source, title, artist, duration):
    query = f"{artist} {title}"
    if source == "lrclib":
        rows = get_json(f"{LRCLIB}/search", track_name=title, artist_name=artist)
        if not isinstance(rows, list):
            raise ValueError("LRCLIB 未返回候选列表。")
        # LRCLIB 搜索已经附带歌词，可先过滤只有纯文本的条目。
        return [candidate(row.get("id"), row.get("trackName"), row.get("artistName"),
                          row.get("albumName"), row.get("duration"), lyrics=row.get("syncedLyrics"))
                for row in rows if row.get("syncedLyrics")]
    if source == "netease":
        data = checked(get_json(f"{NETEASE}/search/get/web", s=query, type=1, limit=10), "code", 200)
        return [candidate(row.get("id"), row.get("name"), "/".join(a["name"] for a in row["artists"]),
                          (row.get("album") or {}).get("name"), float(row.get("duration") or 0) / 1000)
                for row in (data.get("result") or {}).get("songs") or []]
    if source == "qq":
        data = checked(get_json(f"{QQ}/soso/fcgi-bin/client_search_cp", format="json", w=query, p=1, n=10),
                       "code", 0)
        return [candidate(row.get("songmid"), html.unescape(row.get("songname") or ""),
                          "/".join(html.unescape(a["name"]) for a in row["singer"]),
                          html.unescape(row.get("albumname") or ""), row.get("interval"))
                for row in ((data.get("data") or {}).get("song") or {}).get("list") or []]
    # 酷狗歌词搜索的时长为毫秒，accesskey 由搜索响应提供，用于获取该条歌词。
    data = checked(get_json(f"{KUGOU}/search", ver=1, man="yes", client="pc",
                            keyword=f"{artist} - {title}", duration=round(duration * 1000)), "status", 200)
    return [candidate(row.get("id"), row.get("song"), row.get("singer"), None,
                      float(row.get("duration") or 0) / 1000, accesskey=row["accesskey"])
            for row in data.get("candidates") or [] if row.get("accesskey")]


def fetch(source, row):
    if source == "lrclib":
        return row["lyrics"]
    if source == "netease":
        data = checked(get_json(f"{NETEASE}/song/lyric", id=row["id"], lv=1, kv=1, tv=1), "code", 200)
        return (data.get("lrc") or {}).get("lyric")
    if source == "qq":
        data = checked(get_json(f"{QQ}/lyric/fcgi-bin/fcg_query_lyric_new.fcg", format="json",
                                nobase64=1, songmid=row["id"]), "code", 0)
        return html.unescape(data.get("lyric") or "")
    data = checked(get_json(f"{KUGOU}/download", ver=1, client="pc", id=row["id"],
                            accesskey=row["accesskey"], fmt="lrc", charset="utf8"), "status", 200)
    if data.get("fmt") == "lrc" and data.get("content"):
        return base64.b64decode(data["content"], validate=True).decode("utf-8-sig")
    return None
