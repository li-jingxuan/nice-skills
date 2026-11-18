#!/usr/bin/env python3
"""通过夸克或百度网盘下载歌曲并入库，为音频匹配同名 LRC。"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import closing
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import lyric_sources
import music_batch
from music_baidu import Baidu, BaiduError, FILE_ID as BAIDU_FILE_ID


# 官网地址：https://so.liumingye.cn/
API = "https://so.liumingye.cn/api/music"
SEARCH_SOURCE_PAGE_SIZE = 20
SHARE_PAGE_SIZE = 50
DEFAULT_FORMAT = "flac"
# 键是文件扩展名，值用于匹配 ffprobe 返回的容器名。Opus 音频通常封装在 Ogg 容器里。
FORMATS = {
    "flac": "flac", "mp3": "mp3", "wav": "wav", "m4a": "m4a",
    "aac": "aac", "ape": "ape", "ogg": "ogg", "opus": "ogg", "aiff": "aiff",
}
CODEC_MATCH_FORMATS = {"flac", "mp3", "aac", "ape", "opus"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
QUARK_FID_PATTERN = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_-]*|~[A-Za-z0-9_-]+\|[A-Za-z0-9_-]+)")
QUARK_DOWNLOAD_LIMIT = re.compile(r"\bdownload file size limit\s*\[\s*([1-9][0-9]{0,19})\s*\]", re.IGNORECASE)
VERSION_SUFFIX = re.compile(r"(?:\s*[(（\[【]([^()（）\[\]【】]+)[)）\]】]| - ([^-]+))$")
VERSION_LABEL = re.compile(
    r"\b(live|acoustic|instrumental|remaster(?:ed)?|remix|demo|cover|radio edit)\b"
    r"|现场|演唱会|不插电|伴奏|重制|重录|混音|翻唱|录音室|原版", re.IGNORECASE,
)
LRC_TIME = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
LRC_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\]", re.IGNORECASE | re.MULTILINE)
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
KANA_OR_HANGUL = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]")
LRC_CREDIT = re.compile(r"^(?:作[词詞曲]|[词詞曲]|[编編]曲|歌手|演唱|[录錄]音|混音|製作|制作)[\s:：/]|"
                        r"^(?:未经|未經|版权所有|版權所有)")
# 歌词中常见的简繁特征字。两种字形都常用的字（例如「后」「里」）不参与判断。
SIMPLIFIED_MARKERS = frozenset(
    "万与东丝丢两个为丽么义乐习乡书买乱争云亲仅从们优会伤伞体偿儿兰关兴养兽冲决冻净凉凤创删"
    "剧劝办务动劳势华协单卖卫厅历压厌叹听员呜响哟唤团园围国图圆圣场坏块坚坠墙壮声处备头夺"
    "奖妈妇娇孙学宁宝实宠宽对寻导尘尝层岁岛币帅师帐帘带帮广庆应废开异弃张弯弹归当录彻径忆忧怀"
    "态怜总恋恼悬惊惧惯愤戏战户执扩扫扬护报拟择挣挥损摇撑敌数断无旧时显晓晕暂术机杀杂权条来"
    "枫枪栏树样桥梦楼欢残毕气汉汤沟没沧泪洁浅浊测济浓润涨涩渊湾湿满滨滩灭灯灵灾灿炉点烂烛"
    "烦烧热爱爷牵犹狮猎独猫献环现电画疗疯监盘睁矿码砖确礼离种积称稳穷窝竞笔笼简签篮类粮紧"
    "纠红约级纪纯纸纹线练细织终绊经绑结绕绘给绝统绣继绪续维绵绿缓编缘缝缠缩网联聪肤胆胶脉"
    "脑脸腾艰艺节药莲获萤营蓝虑虽蛮补装裤见观规视览觉触订计认讨让议讯记讲许论设访诀证识诉"
    "词译试诗诚话该语误说请诺读谁调谎谜谢谣谦谱负财败账货质贫贵贺资赏赛赞赠赵赶跃踪躯车轨"
    "转轮软轻载较辆输辞边达迁过迈运还这进远违连迟选递遗遥邮邻酿释针钟钢钱铁铃铜银铺链锁锋"
    "锐错锦镇镜长门闪闭闯间闷闹闻阅队阳阴阵际陆陈险随隐难雾顶顺顾顿颂预领频颗题颜额颠颤风"
    "飘飞饥饭饮饱饼馆马驰驱驶骂骄验骑骗骤鱼鲜鸟鸡鸣鸦鹊鹤鹰麦齐齿龄龙龟发"
)
TRADITIONAL_MARKERS = frozenset(
    "萬與東絲丟兩個為麗麼義樂習鄉書買亂爭雲親僅從們優會傷傘體償兒蘭關興養獸沖決凍淨涼鳳創刪"
    "劇勸辦務動勞勢華協單賣衛廳歷壓厭嘆聽員嗚響喲喚團園圍國圖圓聖場壞塊堅墜牆壯聲處備頭奪"
    "獎媽婦嬌孫學寧寶實寵寬對尋導塵嘗層歲島幣帥師帳簾帶幫廣慶應廢開異棄張彎彈歸當錄徹徑憶憂懷"
    "態憐總戀惱懸驚懼慣憤戲戰戶執擴掃揚護報擬擇掙揮損搖撐敵數斷無舊時顯曉暈暫術機殺雜權條來"
    "楓槍欄樹樣橋夢樓歡殘畢氣漢湯溝沒滄淚潔淺濁測濟濃潤漲澀淵灣濕滿濱灘滅燈靈災燦爐點爛燭"
    "煩燒熱愛爺牽猶獅獵獨貓獻環現電畫療瘋監盤睜礦碼磚確禮離種積稱穩窮窩競筆籠簡簽籃類糧緊"
    "糾紅約級紀純紙紋線練細織終絆經綁結繞繪給絕統繡繼緒續維綿綠緩編緣縫纏縮網聯聰膚膽膠脈"
    "腦臉騰艱藝節藥蓮獲螢營藍慮雖蠻補裝褲見觀規視覽覺觸訂計認討讓議訊記講許論設訪訣證識訴"
    "詞譯試詩誠話該語誤說請諾讀誰調謊謎謝謠謙譜負財敗賬貨質貧貴賀資賞賽讚贈趙趕躍蹤軀車軌"
    "轉輪軟輕載較輛輸辭邊達遷過邁運還這進遠違連遲選遞遺遙郵鄰釀釋針鐘鋼錢鐵鈴銅銀鋪鏈鎖鋒"
    "銳錯錦鎮鏡長門閃閉闖間悶鬧聞閱隊陽陰陣際陸陳險隨隱難霧頂順顧頓頌預領頻顆題顏額顛顫風"
    "飄飛飢飯飲飽餅館馬馳驅駛罵驕驗騎騙驟魚鮮鳥雞鳴鴉鵲鶴鷹麥齊齒齡龍龜臺裏裡發髮後隻"
)

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS indexed_roots (root TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS library_tracks (
    root TEXT NOT NULL,
    path TEXT NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    artist_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    version TEXT,
    album TEXT,
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    PRIMARY KEY (root, path)
);
CREATE INDEX IF NOT EXISTS library_song ON library_tracks (root, title_key, artist_key);
"""
SAVE_TRACK = """
INSERT INTO library_tracks (
    root, path, artist, title, artist_key, title_key, version, album, format, size, modified_ns
) VALUES (
    :root, :path, :artist, :title, :artist_key, :title_key,
    :version, :album, :format, :size, :modified_ns
)
ON CONFLICT (root, path) DO UPDATE SET
    artist = excluded.artist, title = excluded.title,
    artist_key = excluded.artist_key, title_key = excluded.title_key,
    version = excluded.version,
    album = COALESCE(excluded.album, CASE
        WHEN library_tracks.size = excluded.size AND library_tracks.modified_ns = excluded.modified_ns
        THEN library_tracks.album
    END),
    format = excluded.format, size = excluded.size, modified_ns = excluded.modified_ns
"""


class SkillError(Exception):
    def __init__(self, message, *, required_setting=None):
        super().__init__(message)
        self.required_setting = required_setting


class DownloadSizeLimit(SkillError):
    def __init__(self, message, limit_bytes):
        super().__init__(message)
        self.limit_bytes = limit_bytes


def require_id(value, label, *, pattern=QUARK_FID_PATTERN):
    # 夸克新版返回带签名的 FID。下歌吧调用传入单独的 ID 规则。
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SkillError(f"{label}无效。")
    return value


def share_provider(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme == "https":
        if url.netloc.lower() == "pan.quark.cn" and re.fullmatch(r"/s/[A-Za-z0-9]+/?", url.path):
            return "quark"
        if url.netloc.lower() == "pan.baidu.com" and re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", url.path):
            return "baidu"
    return None


def label_format(label):
    formats = {part for part in re.findall(r"[a-z0-9]+", label.lower()) if part in FORMATS}
    return next(iter(formats)) if len(formats) == 1 else None


def source_json(url):
    request = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    })
    with urlopen(request, timeout=20) as response:
        raw = response.read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise SkillError("下歌吧返回了未知的数据格式。")
    return data


def track_fields(track):
    return {key: track.get(key) for key in ("id", "title", "artist", "album", "version")}


def search_page(query, page, page_size):
    data = source_json(f"{API}/search?" + urlencode({"q": query, "page": page, "pageSize": page_size}))
    results = data.get("data")
    if not isinstance(results, list):
        raise SkillError("下歌吧的搜索结果缺少歌曲列表。")
    tracks = []
    # 标签只提示来源和可能的格式，文件格式由分享列表确认。
    for track in results:
        labels = [label for label in track.get("quality") or []
                  if isinstance(label, str) and any(name in label for name in ("夸克", "百度"))]
        if labels:
            tracks.append({**track_fields(track), "quality": labels})
    return {
        "tracks": tracks,
        "next_page": page + 1 if page < (data.get("totalPages") or 0) else None,
    }


def search_tracks(args):
    title = args.title.strip()
    if not title:
        raise SkillError("请提供歌名。")
    query = f"{title} {args.artist.strip()}".rstrip()
    page_size = getattr(args, "page_size", None)
    if page_size is None:
        return search_page(query, args.page, 25)

    # 按筛选后的候选分页。多读一条就能确认是否还有下一页。
    skip = (args.page - 1) * page_size
    tracks = []
    page = 1
    while page is not None and len(tracks) <= page_size:
        result = search_page(query, page, SEARCH_SOURCE_PAGE_SIZE)
        batch = result["tracks"]
        tracks.extend(batch[skip:])
        skip = max(0, skip - len(batch))
        page = result["next_page"]
    return {"tracks": tracks[:page_size], "next_page": args.page + 1 if len(tracks) > page_size else None}


def track_detail(args):
    track_id = require_id(args.id, "歌曲 ID", pattern=ID_PATTERN)
    track = source_json(f"{API}/{track_id}")
    if track.get("id") != track_id:
        raise SkillError("下歌吧未返回指定歌曲。")
    downloads = []
    for item in track.get("downloads") or []:
        url = item.get("url")
        if not share_provider(url):
            continue
        label = item.get("quality") or ""
        option = {"url": url, "quality": label, "format": label_format(label)}
        if option not in downloads:
            downloads.append(option)
    return {**track_fields(track), "downloads": downloads}


class Quark:
    """复用已配置的夸克 Skill，命令和选项以参数数组传给 Node.js。"""

    def __init__(self, context):
        self.context = context
        cli = os.environ.get("MUSIC_QUARK_CLI", "")
        if not Path(cli).is_absolute() or not Path(cli).is_file():
            raise SkillError("请将 MUSIC_QUARK_CLI 配置为夸克 CLI 的绝对路径。")
        node = shutil.which("node")
        if not node:
            raise SkillError("运行夸克 CLI 需要 Node.js。")
        self.command = [node, cli]

    def run(self, command, *options, stage=None, timeout=90):
        self.context["stage"] = stage or command
        result = subprocess.run(
            [*self.command, command, *options], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        # CLI 逐行输出 JSON。只取最终结果和搜索产物，跳过进度与其他日志。
        final = {}
        artifact_path = None
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "result":
                final = record
            elif record.get("type") == "artifact":
                artifact_path = (record.get("data") or {}).get("file_path")
        if result.returncode or final.get("code") != 0:
            if getattr(self, "capture_status", False):
                # 批量队列用错误码判断暂停条件，用转存任务的终态判断是否可以补交。
                self.context["quark_code"] = final.get("code")
                data = final.get("data")
                self.context["quark_task_status"] = data.get("status") if isinstance(data, dict) else None
            if final.get("code") == -104:
                raise SkillError("夸克 CLI 无法识别当前运行环境。请在夸克支持的 OpenClaw 或 Codex 环境中运行。")
            # 分享链接可能带提取码，错误提示只保留 CLI 的其他内容。
            message = str(final.get("msg") or "夸克 CLI 未返回成功结果。")
            limit = QUARK_DOWNLOAD_LIMIT.search(message) if command == "download" else None
            message = re.sub(r"https?://[^\s]+", "[链接]", message)
            if limit:
                raise DownloadSizeLimit(message[:300], int(limit[1]))
            raise SkillError(message[:300])
        data = final.get("data")
        if not isinstance(data, dict):
            raise SkillError("夸克 CLI 返回的数据格式无法识别。")
        return data, artifact_path

    def supports_browse(self):
        help_text = subprocess.run([*self.command, "browse", "--help"], capture_output=True, text=True, timeout=15)
        return help_text.returncode == 0 and "--task-dir" in help_text.stdout


def share_url(args):
    if share_provider(args.url) != "quark":
        raise SkillError("请提供有效的 HTTPS 夸克网盘分享链接。")
    if args.passcode:
        # 使用 URL 携带提取码，让夸克 CLI 的各分页路径都能解析。
        url = urlsplit(args.url)
        return url._replace(query=urlencode({"pwd": args.passcode}), fragment="").geturl()
    return args.url


def share_page(quark, args):
    folder = require_id(args.folder_fid, "分享目录 ID")
    data, _ = quark.run(
        "share-detail", "--url", share_url(args), "--pdir-fid", folder,
        "--page", str(args.page), "--size", str(SHARE_PAGE_SIZE), stage="share",
    )
    entries = data.get("files")
    if not isinstance(entries, list):
        raise SkillError("夸克未返回分享文件列表。")
    files = []
    for item in entries:
        name = item.get("filename") or ""
        kind = str(item.get("file_type"))
        audio_format = Path(name).suffix.lower().lstrip(".")
        if kind == "0" or (kind == "1" and audio_format in FORMATS):
            files.append({
                "fid": item.get("fid"), "filename": name, "size": item.get("size"),
                "type": "folder" if kind == "0" else "audio",
                "format": audio_format if kind == "1" else None,
            })
    return {"files": files, "next_page": args.page + 1 if len(entries) >= SHARE_PAGE_SIZE else None}


def share_client(args, context):
    provider = share_provider(args.url)
    if provider == "baidu":
        if args.folder_fid != "0" or getattr(args, "staging_fid", None):
            raise SkillError("百度网盘子目录请用 --source-dir，续接暂存目录请用 --staging-dir。")
        return Baidu(context)
    if provider == "quark":
        if getattr(args, "source_dir", None) or getattr(args, "staging_dir", None):
            raise SkillError("夸克子目录请用 --folder-fid，续接暂存目录请用 --staging-fid。")
        return Quark(context)
    raise SkillError("请提供有效的 HTTPS 夸克或百度网盘分享链接。")


def browse_share(client, args):
    if isinstance(client, Baidu):
        return client.share_page(args, FORMATS)
    return share_page(client, args)


def library_root():
    root = Path(os.environ.get("MUSIC_LIBRARY_ROOT", ""))
    if not root.is_absolute() or not root.is_dir():
        raise SkillError(
            "请将 MUSIC_LIBRARY_ROOT 配置为已存在的音乐库绝对路径。", required_setting="MUSIC_LIBRARY_ROOT",
        )
    return root.resolve()


def destination_dir(directory):
    """在 OpenClaw 运行环境中解析路径。先解析符号链接，再检查是否仍在音乐库内。"""
    root = library_root()
    target = directory or os.environ.get("MUSIC_LIBRARY_DEFAULT_DIR")
    if not target:
        raise SkillError(
            "请指定本次保存目录，或配置 MUSIC_LIBRARY_DEFAULT_DIR。", required_setting="MUSIC_LIBRARY_DEFAULT_DIR",
        )
    target = (root / target).resolve()
    if not target.is_relative_to(root):
        message = "目标目录必须位于配置的音乐库根目录内。"
    elif target.exists() and not target.is_dir():
        message = "目标路径已被文件占用，请指定目录。"
    else:
        return target
    raise SkillError(message, required_setting=None if directory else "MUSIC_LIBRARY_DEFAULT_DIR")


def song_key(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def song_version(title, version=None):
    title = title.strip()
    version = (version or "").strip() or None
    match = VERSION_SUFFIX.search(title)
    if match:
        label = (match[1] or match[2]).strip()
        base = title[:match.start()].strip()
        if base and (VERSION_LABEL.search(label) or (version and song_key(label) == song_key(version))):
            return base, version or label
    return title, version


def index_path():
    override = os.environ.get("MUSIC_LIBRARY_DB")
    return Path(override).expanduser() if override else Path(__file__).resolve().parents[1] / "data/library.sqlite3"


def open_index():
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=30)
    try:
        database.row_factory = sqlite3.Row
        database.executescript(INDEX_SCHEMA)
    except sqlite3.Error:
        database.close()
        raise
    return database


def track_record(root, path, info, artist, title, audio_format, version=None, album=None):
    title, version = song_version(title, version)
    return {
        "root": str(root), "path": str(path), "artist": artist.strip(), "title": title,
        "artist_key": song_key(artist), "title_key": song_key(title), "version": version,
        "album": (album or "").strip() or None, "format": audio_format,
        "size": info.st_size, "modified_ns": info.st_mtime_ns,
    }


def same_file_info(row, info):
    return stat.S_ISREG(info.st_mode) and info.st_size == row["size"] and info.st_mtime_ns == row["modified_ns"]


def scan_library(root, previous, *, directory=None):
    def fail(error):
        raise error

    ffprobe = shutil.which("ffprobe")
    for folder, directories, files in os.walk(directory or root, onerror=fail):
        directories[:] = [name for name in directories if not name.startswith(".")]
        for name in files:
            path = Path(folder) / name
            audio_format = path.suffix.lower().lstrip(".")
            if name.startswith(".") or audio_format not in FORMATS:
                continue
            try:
                str(path).encode("utf-8")
            except UnicodeEncodeError:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or not info.st_size:
                continue
            old = previous.get(str(path))
            if old and same_file_info(old, info):
                yield dict(old)
                continue
            artist, separator, title = path.stem.partition(" - ")
            if not (separator and artist.strip() and title.strip()):
                if directory is None and old is None:
                    continue
                artist, title = "", path.stem
            tags = {}
            if ffprobe and (directory is not None or old is not None):
                try:
                    _, _, tags = validate_audio(path, audio_format, info.st_size, ffprobe)
                except (SkillError, OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
                    # 损坏的标签或无法读取的容器仍可按文件名建立索引。
                    pass
            _, file_version = song_version(title)
            tagged_title = tags.get("title") or title
            _, version = song_version(tagged_title, tags.get("version"))
            yield track_record(root, path, info, tags.get("artist") or artist, tagged_title,
                               audio_format, version or file_version, tags.get("album"))


def indexed(database, root):
    return database.execute("SELECT 1 FROM indexed_roots WHERE root = ?", (str(root),)).fetchone() is not None


def refresh_index(database, root, *, first_lookup=False):
    with database:
        # 串行化刷新和入库更新，查询仍可读取刷新前的索引。
        database.execute("BEGIN IMMEDIATE")
        if first_lookup and indexed(database, root):
            return None
        previous = {row["path"]: row for row in database.execute(
            "SELECT * FROM library_tracks WHERE root = ?", (str(root),),
        )}
        rows = list(scan_library(root, previous))
        database.execute("DELETE FROM library_tracks WHERE root = ?", (str(root),))
        database.executemany(SAVE_TRACK, rows)
        database.execute("INSERT OR IGNORE INTO indexed_roots VALUES (?)", (str(root),))
    return len(rows)


def scan_directory(database, root, directory):
    # SQLite 按二进制排序时，'0' 紧跟 '/'。这个范围只包含指定目录的子路径。
    prefix = str(directory).rstrip("/") + "/"
    query = "SELECT * FROM library_tracks WHERE root = ? AND path >= ? AND path < ?"
    values = (str(root), prefix, prefix[:-1] + "0")
    previous = {row["path"]: row for row in database.execute(query, values)}
    rows = list(scan_library(root, previous, directory=directory))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    with database:
        # 读取标签时不持有写锁，提交时按最新索引判重，保留并发入库确认的元数据。
        database.execute("BEGIN IMMEDIATE")
        current = {row["path"]: row for row in database.execute(query, values)}
        for row in rows:
            path = Path(row["path"])
            try:
                info = path.lstat()
                if path.resolve() != path or not same_file_info(row, info):
                    continue
            except (FileNotFoundError, NotADirectoryError):
                continue
            old = current.get(row["path"])
            if old is not None and same_file_info(old, info):
                counts["unchanged"] += 1
                continue
            database.execute(SAVE_TRACK, row)
            counts["inserted" if old is None else "updated"] += 1
    return {"directory": str(directory), "scanned": len(rows), **counts, "skipped": len(rows) - sum(counts.values())}


def lookup_library(args):
    root = library_root()
    title, _ = song_version(args.title, getattr(args, "version", None))
    if not title:
        raise SkillError("请提供歌名。")
    query = "SELECT * FROM library_tracks WHERE root = ? AND title_key = ?"
    values = [str(root), song_key(title)]
    if args.artist.strip():
        query += " AND artist_key = ?"
        values.append(song_key(args.artist))
    query += " ORDER BY artist_key, version, format, path"
    with closing(open_index()) as database:
        if not indexed(database, root):
            refresh_index(database, root, first_lookup=True)
        entries = database.execute(query, values).fetchall()
    tracks = []
    for row in entries:
        path = Path(row["path"])
        try:
            if path.resolve() != path or not path.is_relative_to(root) or not same_file_info(row, path.lstat()):
                continue
        except (FileNotFoundError, NotADirectoryError):
            continue
        tracks.append({key: row[key] for key in ("artist", "title", "version", "album", "format", "path", "size")})
    return {"tracks": tracks}


def library_filename(args):
    # 用已确认的歌手和歌名生成文件名，替换路径分隔符及 NAS 上常见的非法字符。
    title = args.title.strip()
    base, version = song_version(title, args.version)
    if title and title == base and version:
        title = f"{title} ({version})"
    parts = []
    for text in (args.artist, title):
        part = re.sub(r'[\x00-\x1f\x7f/\\<>:"|?*]+', "_", text).strip(" .")
        if not part:
            raise SkillError("歌手和歌名都不能为空。")
        parts.append(part)
    name = f"{' - '.join(parts)}.{args.format}"
    if len(os.fsencode(name)) > 255:
        raise SkillError("入库文件名过长。")
    return name


def locate_saved_file(quark, folder_fid, selected, local_dir, *, allow_empty=False):
    """在已确认的暂存目录内按原文件名和字节数找到转存后的 FID。"""
    name = selected["filename"]
    quark.context["stage"] = "locate"
    browse = quark.supports_browse()
    if browse:
        # 新版可能用签名 FID 表示条目的父目录。按目录查询可由服务端限定范围。
        data, _ = quark.run("browse", "--parent-fid", folder_fid, "--task-dir", str(local_dir), "--all", stage="locate")
        if str(data.get("parent_fid")) != folder_fid or data.get("hasMore"):
            raise SkillError("夸克未返回指定暂存目录的完整文件列表。")
        artifact = data.get("file_path")
    else:
        # 旧版没有 browse，只能读取完整搜索结果并检查父目录 FID。
        keyword = Path(name).stem.strip()[:50]
        if not keyword:
            raise SkillError("分享文件名无法用于定位转存后的文件。")
        _, artifact = quark.run("search", "--keyword", keyword, "--stdout-only", stage="locate")
    if not artifact:
        raise SkillError("夸克未返回完整文件列表，暂时无法定位转存文件。")
    with Path(artifact).open(encoding="utf-8") as lines:
        entries = list(map(json.loads, lines))
        matches = [
            file for file in entries
            if (browse or str(file.get("parent_fid")) == folder_fid)
            and str(file.get("file_type", "1")) == "1"
            and file.get("filename") == name and file.get("size") == selected["size"]
        ]
    # 只有按目录查到完整的空列表，才可在原暂存目录内补做转存。
    if allow_empty and browse and not entries:
        return None
    if len(matches) != 1:
        raise SkillError("尚未在本次暂存目录唯一定位到转存文件，请检查夸克网盘中的转存结果。")
    return require_id(matches[0].get("fid"), "转存文件 ID")


def create_staging_folder(quark, args, selected):
    # 每次使用独立的网盘暂存目录，同名的历史文件和其他并发请求不参与匹配。
    folder_name = f"music-download-{uuid4().hex[:16]}"
    parent = os.environ.get("MUSIC_QUARK_STAGING_PARENT_FID")
    folder_options = ["--parent-fid", require_id(parent, "网盘暂存父目录 ID")] if parent else []
    folder, _ = quark.run("create-folder", "--dir-path", folder_name, *folder_options, stage="create_staging")
    quark.context["quark_staging"] = {"name": folder_name, "path": folder.get("full_path"), "fid": folder.get("fid"),
                                      "filename": selected["filename"], "size": selected["size"]}
    folder_id = require_id(folder.get("fid"), "网盘暂存目录 ID")
    if checkpoint := getattr(args, "_checkpoint", None):
        checkpoint(staging={**quark.context["quark_staging"], "saved": False})
    return folder_id


def save_share_file(quark, args, selected, folder_id):
    checkpoint = getattr(args, "_checkpoint", None)
    url = share_url(args)
    if checkpoint:
        # CLI 提交转存后会轮询。提前记下可能已提交，进程中断时先查询原目录。
        checkpoint(staging={**quark.context["quark_staging"], "submitted": True})
    try:
        saved, _ = quark.run("saveas", "--url", url, "--fid-list", selected["fid"],
                             "--to-pdir-fid", folder_id, timeout=960)
    except SkillError:
        # 超时或未知结果可能仍在后台转存。只有明确未提交或任务终态失败才允许重新提交。
        if checkpoint and (quark.context.get("quark_code") in {-103, -104, -1106, -1107, 32003, 41035, 41014}
                           or quark.context.get("quark_task_status") == 3):
            checkpoint(staging={**quark.context["quark_staging"], "submitted": False})
        raise
    saved_folder = (saved.get("save_as") or {}).get("to_pdir_fid")
    if saved_folder is not None and str(saved_folder) != folder_id:
        raise SkillError("夸克返回的转存目录与本次暂存目录不一致。")
    if checkpoint:
        checkpoint(staging={**quark.context["quark_staging"], "saved": True})


def transfer(quark, args, selected, local_dir):
    """优先从网盘恢复点下载。需要补做转存时，先核对原暂存目录。"""
    staging = getattr(args, "_staging", None) or {}
    if staging.get("file_fid"):
        quark.context["quark_staging"] = staging
        file_fid = require_id(staging["file_fid"], "转存文件 ID")
    else:
        file_fid = None
        if args.staging_fid:
            folder_id = require_id(args.staging_fid, "网盘暂存目录 ID")
            quark.context["quark_staging"] = staging or {"fid": folder_id, "filename": selected["filename"]}
            # 批量恢复时，确认此前未提交且目录为空，才在原目录补做转存。
            allow_empty = bool(staging) and not (staging.get("saved") or staging.get("submitted"))
            file_fid = locate_saved_file(quark, folder_id, selected, local_dir, allow_empty=allow_empty)
        else:
            folder_id = create_staging_folder(quark, args, selected)
        if file_fid is None:
            save_share_file(quark, args, selected, folder_id)
            file_fid = locate_saved_file(quark, folder_id, selected, local_dir)
    if checkpoint := getattr(args, "_checkpoint", None):
        checkpoint(staging={**quark.context["quark_staging"], "saved": True, "file_fid": file_fid,
                            "size": selected["size"]})
    downloaded, _ = quark.run(
        "download", "--fid", file_fid, "--output-dir", str(local_dir), timeout=900,
    )
    # CLI 返回的路径必须是本次临时目录中的普通文件，后续才可校验和复制。
    source = Path(downloaded.get("filePath") or "")
    if not source.is_absolute() or source.is_symlink() or source.resolve().parent != local_dir or not source.is_file():
        raise SkillError("夸克未返回有效的暂存文件。")
    return source


def validate_audio(source, audio_format, expected_size, ffprobe):
    """先核对字节数，再读取媒体信息确认容器、音轨和时长。"""
    size = source.stat().st_size
    if size != expected_size:
        raise SkillError("下载文件的字节数与分享文件不一致。")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=format_name,duration:format_tags:stream=codec_name,codec_type:stream_tags",
         "-of", "json", str(source)], capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode or result.stderr:
        raise SkillError("音频读取校验失败。")
    probe = json.loads(result.stdout)
    info = probe.get("format", {})
    streams = [item for item in probe.get("streams", []) if item.get("codec_type") == "audio"]
    codecs = {item.get("codec_name") for item in streams}
    formats = info.get("format_name", "").split(",")
    # FLAC 等格式还要求音轨编码匹配，避免仅凭扩展名或容器判定。
    codec_mismatch = audio_format in CODEC_MATCH_FORMATS and audio_format not in codecs
    duration = float(info.get("duration", 0))
    if (FORMATS[audio_format] not in formats or not codecs or not math.isfinite(duration) or duration <= 0
            or codec_mismatch):
        raise SkillError(f"实际音频格式或时长校验失败，期望 {audio_format.upper()}。")
    # M4A 等容器可能把标签写在音轨上。容器级同名标签优先。
    tags = {key.lower(): value.strip() for item in [*streams, info]
            for key, value in (item.get("tags") or {}).items() if isinstance(value, str) and value.strip()}
    return size, duration, tags


def lyric_metadata(path, args, tags):
    file_artist, separator, file_title = path.stem.partition(" - ")
    _, file_version = song_version(file_title)
    _, supplied_version = song_version(args.title or "", args.version)
    fallback_version = supplied_version or file_version
    title, tag_version = lyric_song_version(tags.get("title") or args.title or file_title or path.stem,
                                           fallback_version)
    artist = tags.get("artist") or args.artist or (file_artist if separator else "")
    if not title or not artist.strip():
        raise SkillError("识别歌词需要歌名和歌手，可用 --title 和 --artist 补充。")
    return title, artist.strip(), tags.get("album") or args.album or "", tag_version or fallback_version


def lyric_key(value):
    return song_key(value.replace("\u200b", "").replace("\ufeff", ""))


def artist_key(value):
    # 平台会把合唱歌手拆成列表，本地标签也可能使用斜线或分号分隔。
    return tuple(sorted(part.strip() for part in re.split(r"[/;、]", lyric_key(value))))


def lyric_song_version(title, known_version):
    base, version = song_version(title)
    # 已知版本只辅助识别自定义后缀，候选本身标注的现场、混音等版本优先。
    if not version and known_version:
        custom_base, _ = song_version(title, known_version)
        if custom_base != base:
            return custom_base, known_version
    return base, version


def lyric_match(row, title, artist, album, version, duration):
    candidate_title, candidate_version = lyric_song_version(row["title"], version)
    if (not row["id"] or lyric_key(candidate_title) != lyric_key(title)
            or artist_key(row["artist"]) != artist_key(artist)):
        return None
    if candidate_version and lyric_key(candidate_version) != lyric_key(version or ""):
        return None
    expected_album = lyric_key(album.strip().strip("《》"))
    actual_album = lyric_key(row["album"].strip().strip("《》"))
    if expected_album and actual_album and expected_album != actual_album:
        return None
    candidate_duration = float(row["duration"] or 0)
    difference = abs(candidate_duration - duration)
    if candidate_duration <= 0 or not math.isfinite(difference) or difference > 3:
        return None
    album_matches = bool(expected_album and actual_album == expected_album)
    near_duration = difference <= 1
    # 无法核实显式版本时交给用户确认。专辑未知时，自动匹配要求时长差不超过 1 秒。
    auto = (not version or bool(candidate_version)) and (album_matches or near_duration)
    return not auto, not album_matches, not near_duration


def lrc_cues(text, duration):
    """校验带正文的时间戳，并用实际时间和正文比较候选的时间轴。"""
    offset = LRC_OFFSET.search(text)
    offset_ms = int(offset[1]) if offset else 0
    cues = set()
    for line in text.splitlines():
        line = line.lstrip()
        times = []
        position = 0
        while (match := LRC_TIME.match(line, position)):
            minute, second, fraction = match.groups()
            times.append((int(minute) * 60 + int(second)) * 1000 + int((fraction or "").ljust(3, "0")))
            position = match.end()
        lyric = line[position:].strip()
        if lyric:
            # LRC 的正 offset 表示提前显示。
            cues.update((time - offset_ms, lyric) for time in times)
    distinct_times = {time for time, _ in cues}
    if len(distinct_times) < 2 or max(distinct_times) > (duration + 3) * 1000:
        return ()
    return tuple(sorted(cues))


def chinese_lyric_script(cues):
    # 只判断时间轴正文。去掉署名和版权行，避免把带中文制作信息的英文歌判成中文。
    body = "\n".join({line for _, line in cues if not LRC_CREDIT.match(line)})
    han = HAN.findall(body)
    if (len(han) < 2 or KANA_OR_HANGUL.search(body)
            or len(han) <= sum(character.isalpha() for character in body) - len(han)):
        return None
    characters = set(han)
    simplified = not SIMPLIFIED_MARKERS.isdisjoint(characters)
    traditional = not TRADITIONAL_MARKERS.isdisjoint(characters)
    if simplified != traditional:
        return "zh-Hans" if simplified else "zh-Hant"
    return "zh"


def lyric_choices(matches):
    if len(matches) == 1:
        return matches[0]
    return {"status": "needs_review", "candidates": [summary for summary, _ in matches]}, None


def find_lyrics(title, artist, album, version, duration, source=None, lyric_id=None, *, prefer_simplified=True):
    pending = []
    unavailable = []
    chinese_fallback = None
    for name in (source,) if source else lyric_sources.SOURCES:
        try:
            ranked = {}
            for row in lyric_sources.search(name, title, artist, duration):
                if lyric_id and row["id"] != lyric_id:
                    continue
                rank = lyric_match(row, title, artist, album, version, duration)
                if rank is not None:
                    ranked.setdefault(rank, []).append(row)
            for rank, rows in sorted(ranked.items()):
                # 简体优先只在可信度相当的候选中生效，不能为字形降低匹配标准。
                if chinese_fallback is not None and rank > chinese_fallback[0]:
                    break
                matches = {}
                for row in rows:
                    text = lyric_sources.fetch(name, row)
                    if not isinstance(text, str):
                        continue
                    text = text.lstrip("\ufeff").strip()
                    cues = lrc_cues(text, duration)
                    if cues:
                        summary = {key: row[key] for key in ("id", "title", "artist", "album", "duration")}
                        summary.update(source=name, timestamps=len(cues))
                        script = chinese_lyric_script(cues)
                        if script:
                            summary["language"] = script
                        matches.setdefault(cues, (summary, text))
                if not matches:
                    continue
                choices = list(matches.values())
                if lyric_id:
                    return lyric_choices(choices)
                if rank[0]:
                    pending.extend(summary for summary, _ in choices)
                    continue
                if not prefer_simplified:
                    return lyric_choices(choices)
                scripts = {summary.get("language") for summary, _ in choices}
                if None in scripts:
                    if chinese_fallback is None:
                        return lyric_choices(choices)
                    break
                simplified = [choice for choice in choices if choice[0]["language"] == "zh-Hans"]
                if simplified:
                    return lyric_choices(simplified)
                # 先留存当前中文歌词，继续查其他来源是否有同等可信度的简体版。
                if chinese_fallback is None:
                    chinese_fallback = (rank, choices)
                break
        except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
            # 超时、限流和响应结构变化时继续查下一家。响应内容和下载参数留在脚本内部。
            unavailable.append(name)
    if chinese_fallback is not None:
        return lyric_choices(chinese_fallback[1])
    if pending:
        return {"status": "needs_review", "candidates": pending}, None
    if unavailable:
        return {"status": "unavailable", "sources": unavailable}, None
    return {"status": "not_found"}, None


def save_lyrics(path, args, *, audio_info=None, source=None, lyric_id=None):
    sidecar = path.with_suffix(".lrc")
    result = {"path": str(sidecar)}
    try:
        if os.path.lexists(sidecar):
            return {**result, "status": "exists"}
        original = path.stat()
        if audio_info is None:
            ffprobe = shutil.which("ffprobe")
            if not ffprobe:
                raise SkillError("读取音频标签和时长需要 ffprobe。")
            audio_info = validate_audio(path, path.suffix.lower()[1:], original.st_size, ffprobe)
        _, duration, tags = audio_info
        metadata = lyric_metadata(path, args, tags)
        language = song_key(tags.get("language", "")).replace("_", "-").split("-", 1)[0]
        explicit_english = language in {"en", "eng", "english", "英语", "英文"}
        match, text = find_lyrics(*metadata, duration, source, lyric_id, prefer_simplified=not explicit_english)
        if text is None:
            return {**result, **match}
        # 同目录暂存，再一次性发布，避免覆盖已有歌词，也避免播放器读到半个文件。
        staged = sidecar.with_name(f".music-lyrics-{uuid4().hex}.part")
        output = staged.open("x", encoding="utf-8")
        try:
            with output as out:
                out.write(text.replace("\r\n", "\n").replace("\r", "\n") + "\n")
                out.flush()
                os.fsync(out.fileno())
            current = path.stat()
            if (not os.path.samestat(original, current) or original.st_mtime_ns != current.st_mtime_ns
                    or original.st_size != current.st_size):
                raise SkillError("查询歌词期间音频发生变化，请重新匹配。")
            try:
                os.link(staged, sidecar)
            except FileExistsError:
                return {**result, "status": "exists"}
        finally:
            staged.unlink(missing_ok=True)
        return {**result, **match, "status": "saved"}
    except (SkillError, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as error:
        return {**result, "status": "error", "message": str(error)}


def lyrics_for_file(args):
    root = library_root()
    path = (root / args.path).resolve()
    if not path.is_relative_to(root) or path.suffix.lower()[1:] not in FORMATS or not path.is_file():
        raise SkillError("请指定音乐库内已存在的音频文件。")
    return save_lyrics(path, args, source=args.source, lyric_id=args.id)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def publish(staged, destination):
    # 在同一文件系统用硬链接一次发布成品。并发写同名时，失败方核对内容再报告结果。
    try:
        os.link(staged, destination)
    except FileExistsError:
        if not destination.is_symlink() and destination.is_file() and sha256(staged) == sha256(destination):
            return "exists"
        raise SkillError("目标目录存在同名文件且内容不同。") from None
    return "saved"


def cleanup_staging(result, staging, *, checkpoint=None, client=None):
    if not staging:
        return
    location = {key: staging[key] for key in ("path", "name", "fid", "filename") if staging.get(key)}
    if staging.get("provider") != "baidu":
        result["staging_cleanup"] = {
            **location, "provider": "quark", "status": "manual",
            "message": "夸克官方 CLI 暂不支持删除，可在网盘客户端将本次暂存副本移入回收站。",
        }
        return
    cleanup = result["staging_cleanup"] = {**location, "provider": "baidu", "status": "removed"}
    if not staging.get("remote_fsid"):
        cleanup.update(status="manual", message="缺少百度网盘暂存副本的文件 ID，请核对后手动清理。")
        return
    if checkpoint and not staging.get("cleanup_started"):
        # 删除前记下恢复点。若进程中断，可用已验证的 NAS 文件恢复并再次核对网盘。
        checkpoint(staging={**staging, "cleanup_started": True})
    try:
        (client if client is not None else Baidu({})).remove_staged_file(staging)
    except (BaiduError, OSError, TypeError, KeyError, AttributeError) as error:
        cleanup.update(status="warning", message=str(error))


def download(args, context):
    try:
        return download_file(args, context)
    except DownloadSizeLimit as error:
        mp3_fid = getattr(args, "mp3_file_fid", None)
        if args.format != "flac" or not mp3_fid:
            raise
        # 新文件需要独立转存。保留 FLAC 的暂存信息，避免误用其目录续接 MP3。
        fallback = context["fallback"] = {"from_format": "flac", "to_format": "mp3", "limit_bytes": error.limit_bytes}
        if "quark_staging" in context:
            fallback["quark_staging"] = context.pop("quark_staging")
        mp3_args = argparse.Namespace(**{**vars(args), "format": "mp3", "file_fid": mp3_fid, "staging_fid": None})
        return download_file(mp3_args, context, limit_bytes=error.limit_bytes)


def download_file(args, context, *, limit_bytes=None):
    context["stage"] = "destination"
    target = destination_dir(args.directory)
    context["stage"] = "configuration"
    baidu = share_provider(args.url) == "baidu"
    file_fid = require_id(args.file_fid, "分享文件 ID", pattern=BAIDU_FILE_ID if baidu else QUARK_FID_PATTERN)
    filename = library_filename(args)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("校验音频需要 ffprobe。")
    checkpoint = getattr(args, "_checkpoint", None)
    client = share_client(args, context)
    if not baidu:
        client.capture_status = bool(checkpoint)
    saved = getattr(args, "_staging", None) or {}
    if saved.get("file_fid") or (saved.get("filename") and (saved.get("saved") or saved.get("submitted"))):
        # 网盘副本已定位时直接用其 FID，否则用已记录的文件名和大小查询暂存目录。
        # 这两种恢复方式都可以在原分享失效后继续。
        selected = {"format": args.format, "size": saved["size"], "filename": saved.get("filename"), "fid": file_fid}
    else:
        # 复查聊天里选中的 FID 和格式，确保转存前分享内容仍与选择一致。
        candidates = browse_share(client, args)["files"]
        selected = next((file for file in candidates if file["fid"] == file_fid), None)
    if not selected or selected["format"] != args.format:
        raise SkillError(f"选中文件不在指定分享页，或格式不是 {args.format.upper()}。")
    if not isinstance(selected["size"], int) or selected["size"] <= 0:
        raise SkillError("网盘分享缺少有效的文件大小，无法核对下载和转存文件。")
    if not baidu and limit_bytes is not None and selected["size"] > limit_bytes:
        raise DownloadSizeLimit(f"候选 {args.format.upper()} 超过夸克的单文件下载上限。", limit_bytes)

    context["stage"] = "destination"
    target.mkdir(parents=True, exist_ok=True)
    destination = target / filename
    staged = target / f".music-download-{uuid4().hex}.part"
    if checkpoint:
        # 创建文件前记下路径，进程异常退出后可以处理残留的暂存文件。
        checkpoint(part=str(staged))
    # 先检验目录是否可写。使用 .part 后缀，让播放器只在发布后看到成品。
    output = staged.open("xb")
    try:
        with output, tempfile.TemporaryDirectory(prefix="music-download-") as folder:
            local_dir = Path(folder).resolve()
            source = client.transfer(args, selected, local_dir) if baidu else transfer(client, args, selected, local_dir)
            context["stage"] = "validate_audio"
            audio_info = validate_audio(source, args.format, selected["size"], ffprobe)
            context["stage"] = "publish"
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
        if checkpoint:
            # 音频校验及写盘完成后记录哈希，续跑时据此确认暂存或已发布音频的内容。
            checkpoint(publish={"path": str(destination), "size": audio_info[0], "sha256": sha256(staged).hex()})
        status = publish(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    result = finish_download(args, destination, audio_info, status=status)
    staging = context.get("baidu_staging" if baidu else "quark_staging")
    cleanup_staging(result, staging, checkpoint=checkpoint, client=client)
    return result


def finish_download(args, destination, audio_info, *, status):
    result = {"status": status, "path": str(destination), "format": args.format, "size": audio_info[0]}
    try:
        root = library_root()
        track = track_record(root, destination, destination.lstat(), args.artist, args.title,
                             args.format, args.version, args.album)
        with closing(open_index()) as database, database:
            database.execute(SAVE_TRACK, track)
    except (SkillError, OSError, sqlite3.Error, UnicodeError) as error:
        # NAS 已发布成品。保留成功状态，便于用户刷新索引，无需重新下载。
        result["index_warning"] = str(error)
    result["lyrics"] = save_lyrics(destination, args, audio_info=audio_info)
    return result


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("页码必须大于 0。")
    return number


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="搜索下歌吧")
    search.add_argument("--title", required=True)
    search.add_argument("--artist", default="")
    search.add_argument("--page", type=positive_int, default=1)
    search.add_argument("--page-size", type=positive_int, help="每页返回的候选数，必要时合并下歌吧多页结果")
    lookup = commands.add_parser("lookup", help="查询 NAS 本地音乐索引")
    lookup.add_argument("--title", required=True)
    lookup.add_argument("--artist", default="")
    commands.add_parser("reindex", help="扫描整个 NAS 音乐库并刷新本地索引")
    scan = commands.add_parser("scan", help="递归扫描指定音乐目录并更新 SQLite 索引")
    scan.add_argument("--directory", required=True, help="已存在的库内目录，可用绝对路径或相对于音乐库根目录的路径")
    commands.add_parser("detail", help="查询歌曲的夸克和百度网盘分享链接").add_argument("--id", required=True)
    commands.add_parser("destination", help="检查入库目录配置").add_argument("--directory")
    share = commands.add_parser("share", help="浏览夸克或百度网盘分享文件")
    download = commands.add_parser("download", help="转存、下载、校验并入库")
    for command in (share, download):
        command.add_argument("--url", required=True)
        command.add_argument("--page", type=positive_int, default=1)
        command.add_argument("--folder-fid", default="0", help="夸克分享子目录 ID")
        command.add_argument("--source-dir", help="百度分享子目录的 path")
        command.add_argument("--passcode")
    download.add_argument("--file-fid", required=True)
    download.add_argument("--title", required=True)
    download.add_argument("--artist", required=True)
    download.add_argument("--version")
    download.add_argument("--album")
    download.add_argument("--format", default=DEFAULT_FORMAT, type=str.lower, choices=FORMATS, help="音频格式，默认 FLAC")
    download.add_argument("--directory")
    download.add_argument("--staging-fid", help="从已确认转存成功的暂存目录续接下载")
    download.add_argument("--staging-dir", help="从 baidu_staging.path 指向的百度暂存目录续接下载")
    download.add_argument("--mp3-file-fid", help="FLAC 超过夸克下载上限时使用的同页、同版本 MP3 分享文件 ID")
    lyrics = commands.add_parser("lyrics", help="为音乐库内的音频匹配和保存同名 LRC")
    lyrics.add_argument("--path", required=True, help="库内音频路径")
    for field in ("title", "artist", "album", "version"):
        lyrics.add_argument(f"--{field}", help="补充音频中缺少的标签")
    lyrics.add_argument("--source", choices=lyric_sources.SOURCES, help="只查询指定来源")
    lyrics.add_argument("--id", help="使用用户确认的歌词候选 ID，需同时指定 --source")
    music_batch.add_commands(commands, FORMATS, DEFAULT_FORMAT)
    args = parser.parse_args()
    if args.command == "lyrics" and args.id and not args.source:
        lyrics.error("--id 必须和 --source 一起使用。")
    if args.command == "download" and args.mp3_file_fid:
        if args.format != "flac" or share_provider(args.url) != "quark":
            download.error("--mp3-file-fid 仅适用于夸克 FLAC 下载。")
        if not QUARK_FID_PATTERN.fullmatch(args.mp3_file_fid) or args.mp3_file_fid == args.file_fid:
            download.error("请提供不同于 FLAC 的有效 MP3 分享文件 ID。")
    return args


def main():
    args = parse_args()
    context = {"stage": args.command}
    try:
        if args.command == "search":
            result = search_tracks(args)
        elif args.command == "lookup":
            result = lookup_library(args)
        elif args.command == "reindex":
            root = library_root()
            with closing(open_index()) as database:
                result = {"indexed": refresh_index(database, root)}
        elif args.command == "scan":
            root = library_root()
            if not args.directory:
                raise SkillError("请指定音乐库内已存在的扫描目录。")
            directory = destination_dir(args.directory)
            if not directory.is_dir():
                raise SkillError("请指定音乐库内已存在的扫描目录。")
            with closing(open_index()) as database:
                result = scan_directory(database, root, directory)
        elif args.command == "detail":
            result = track_detail(args)
        elif args.command == "lyrics":
            result = lyrics_for_file(args)
        elif args.command == "destination":
            result = {
                "path": str(destination_dir(args.directory)),
                "default_configured": bool(os.environ.get("MUSIC_LIBRARY_DEFAULT_DIR")),
            }
        elif args.command == "share":
            context["stage"] = "configuration"
            result = browse_share(share_client(args, context), args)
        elif args.command.startswith("batch-"):
            result = music_batch.dispatch(args, sys.modules[__name__])
        else:
            result = download(args, context)
    except BaiduError as error:
        status = "waiting_transfer" if error.kind == "pending" else "error"
        print(json.dumps({"status": status, **context, "error_code": error.kind or error.code,
                          "message": str(error)}, ensure_ascii=False))
        return int(status == "error")
    except (SkillError, OSError, ValueError, KeyError, TypeError, AttributeError,
            sqlite3.Error, subprocess.TimeoutExpired) as error:
        if isinstance(error, SkillError) and error.required_setting:
            context["required_setting"] = error.required_setting
        if isinstance(error, DownloadSizeLimit):
            context.update(error_code="download_size_limit", limit_bytes=error.limit_bytes)
        message = "等待外部命令超时，请检查当前步骤的结果。" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        print(json.dumps({"status": "error", **context, "message": message}, ensure_ascii=False))
        return 1
    for field in ("quark_staging", "baidu_staging", "fallback"):
        if field in context:
            result[field] = context[field]
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return int(result.get("status") == "error")


if __name__ == "__main__":
    raise SystemExit(main())
