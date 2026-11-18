#!/usr/bin/env python3
"""通过夸克网盘下载单曲并入库，为音频匹配同名 LRC。"""

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
import tempfile
import unicodedata
from contextlib import closing
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import lyric_sources


# 官网地址：https://so.liumingye.cn/
API = "https://so.liumingye.cn/api/music"
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
VERSION_SUFFIX = re.compile(r"(?:\s*[(（\[【]([^()（）\[\]【】]+)[)）\]】]| - ([^-]+))$")
VERSION_LABEL = re.compile(
    r"\b(live|acoustic|instrumental|remaster(?:ed)?|remix|demo|cover|radio edit)\b"
    r"|现场|演唱会|不插电|伴奏|重制|重录|混音|翻唱|录音室|原版", re.IGNORECASE,
)
LRC_TIME = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
LRC_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\]", re.IGNORECASE | re.MULTILINE)

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


def require_id(value, label, *, pattern=QUARK_FID_PATTERN):
    # 夸克新版返回带签名的 FID。下歌吧调用传入单独的 ID 规则。
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SkillError(f"{label}无效。")
    return value


def quark_url(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
    except ValueError:
        return False
    return (
        url.scheme == "https" and url.netloc.lower() == "pan.quark.cn"
        and bool(re.fullmatch(r"/s/[A-Za-z0-9]+/?", url.path))
    )


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


def search_tracks(args):
    title = args.title.strip()
    if not title:
        raise SkillError("请提供歌名。")
    query = f"{title} {args.artist.strip()}".rstrip()
    data = source_json(f"{API}/search?" + urlencode({"q": query, "page": args.page, "pageSize": 25}))
    results = data.get("data")
    if not isinstance(results, list):
        raise SkillError("下歌吧的搜索结果缺少歌曲列表。")
    tracks = []
    # 搜索只保留有夸克来源的条目，实际可用格式要在详情和分享文件中确认。
    for track in results:
        labels = [label for label in track.get("quality") or [] if isinstance(label, str) and "夸克" in label]
        if labels:
            tracks.append({**track_fields(track), "quality": labels})
    return {
        "tracks": tracks,
        "next_page": args.page + 1 if args.page < (data.get("totalPages") or 0) else None,
    }


def track_detail(args):
    track_id = require_id(args.id, "歌曲 ID", pattern=ID_PATTERN)
    track = source_json(f"{API}/{track_id}")
    if track.get("id") != track_id:
        raise SkillError("下歌吧未返回指定歌曲。")
    downloads = []
    for item in track.get("downloads") or []:
        url = item.get("url")
        if not quark_url(url):
            continue
        label = item.get("quality") or ""
        # 「夸克网盘」这类标签没有格式信息，留给分享列表确认。
        formats = [part for part in re.findall(r"[a-z0-9]+", label.lower()) if part in FORMATS]
        option = {"url": url, "quality": label, "format": formats[0] if len(formats) == 1 else None}
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
            if final.get("code") == -104:
                raise SkillError("夸克 CLI 无法识别当前运行环境。请在夸克支持的 OpenClaw 或 Codex 环境中运行。")
            # 分享链接可能带提取码，错误提示只保留 CLI 的其他内容。
            message = str(final.get("msg") or "夸克 CLI 未返回成功结果。")
            message = re.sub(r"https?://[^\s]+", "[链接]", message)
            raise SkillError(message[:300])
        data = final.get("data")
        if not isinstance(data, dict):
            raise SkillError("夸克 CLI 返回的数据格式无法识别。")
        return data, artifact_path

    def supports_browse(self):
        help_text = subprocess.run([*self.command, "browse", "--help"], capture_output=True, text=True, timeout=15)
        return help_text.returncode == 0 and "--task-dir" in help_text.stdout


def share_url(args):
    if not quark_url(args.url):
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


def open_index():
    override = os.environ.get("MUSIC_LIBRARY_DB")
    path = Path(override).expanduser() if override else Path(__file__).resolve().parents[1] / "data/library.sqlite3"
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


def scan_library(root, previous):
    def fail(error):
        raise error

    for folder, directories, files in os.walk(root, onerror=fail):
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
            if separator and artist.strip() and title.strip():
                yield track_record(root, path, info, artist, title, audio_format)


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


def lookup_library(args):
    root = library_root()
    title, _ = song_version(args.title)
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


def locate_saved_file(quark, folder_fid, selected, local_dir):
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
        matches = [
            file for file in map(json.loads, lines)
            if (browse or str(file.get("parent_fid")) == folder_fid)
            and str(file.get("file_type", "1")) == "1"
            and file.get("filename") == name and file.get("size") == selected["size"]
        ]
    if len(matches) != 1:
        raise SkillError("尚未在本次暂存目录唯一定位到转存文件，请检查夸克网盘中的转存结果。")
    return require_id(matches[0].get("fid"), "转存文件 ID")


def save_to_quark(quark, args, selected):
    # 每次使用独立的网盘暂存目录，同名的历史文件和其他并发请求不参与匹配。
    folder_name = f"music-download-{uuid4().hex[:16]}"
    parent = os.environ.get("MUSIC_QUARK_STAGING_PARENT_FID")
    folder_options = ["--parent-fid", require_id(parent, "网盘暂存父目录 ID")] if parent else []
    folder, _ = quark.run("create-folder", "--dir-path", folder_name, *folder_options, stage="create_staging")
    quark.context["quark_staging"] = {"name": folder_name, "path": folder.get("full_path"), "fid": folder.get("fid")}
    folder_id = require_id(folder.get("fid"), "网盘暂存目录 ID")
    saved, _ = quark.run(
        "saveas", "--url", share_url(args), "--fid-list", selected["fid"],
        "--to-pdir-fid", folder_id, timeout=960,
    )
    saved_folder = (saved.get("save_as") or {}).get("to_pdir_fid")
    if saved_folder is not None and str(saved_folder) != folder_id:
        raise SkillError("夸克返回的转存目录与本次暂存目录不一致。")
    return folder_id


def transfer(quark, args, selected, local_dir):
    if args.staging_fid:
        folder_id = require_id(args.staging_fid, "网盘暂存目录 ID")
        quark.context["quark_staging"] = {"fid": folder_id}
    else:
        folder_id = save_to_quark(quark, args, selected)
    file_fid = locate_saved_file(quark, folder_id, selected, local_dir)
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


def find_lyrics(title, artist, album, version, duration, source=None, lyric_id=None):
    pending = []
    unavailable = []
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
                        matches.setdefault(cues, (summary, text))
                if len(matches) == 1 and (lyric_id or not rank[0]):
                    summary, text = next(iter(matches.values()))
                    return summary, text
                if matches:
                    pending.extend(summary for summary, _ in matches.values())
                    # 同一个可信等级有不同时间轴时，交给用户选择。
                    if not rank[0]:
                        return {"status": "needs_review", "candidates": pending}, None
        except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
            # 超时、限流和响应结构变化时继续查下一家。响应内容和下载参数留在脚本内部。
            unavailable.append(name)
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
        match, text = find_lyrics(*metadata, duration, source, lyric_id)
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


def download(args, context):
    context["stage"] = "destination"
    target = destination_dir(args.directory)
    context["stage"] = "configuration"
    file_fid = require_id(args.file_fid, "分享文件 ID")
    filename = library_filename(args)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("校验音频需要 ffprobe。")
    quark = Quark(context)
    # 复查聊天里选中的 FID 和格式，确保转存前分享内容仍与选择一致。
    candidates = share_page(quark, args)["files"]
    selected = next((file for file in candidates if file["fid"] == file_fid), None)
    if not selected or selected["format"] != args.format:
        raise SkillError(f"选中文件不在指定分享页，或格式不是 {args.format.upper()}。")
    if not isinstance(selected["size"], int) or selected["size"] <= 0:
        raise SkillError("夸克分享缺少有效的文件大小，无法核对下载和转存文件。")

    context["stage"] = "destination"
    target.mkdir(parents=True, exist_ok=True)
    destination = target / filename
    staged = target / f".music-download-{uuid4().hex}.part"
    # 先检验目录是否可写。使用 .part 后缀，让播放器只在发布后看到成品。
    output = staged.open("xb")
    try:
        with output, tempfile.TemporaryDirectory(prefix="music-download-") as folder:
            source = transfer(quark, args, selected, Path(folder).resolve())
            context["stage"] = "validate_audio"
            audio_info = validate_audio(source, args.format, selected["size"], ffprobe)
            context["stage"] = "publish"
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
        status = publish(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
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
    lookup = commands.add_parser("lookup", help="查询 NAS 本地音乐索引")
    lookup.add_argument("--title", required=True)
    lookup.add_argument("--artist", default="")
    commands.add_parser("reindex", help="扫描整个 NAS 音乐库并刷新本地索引")
    commands.add_parser("detail", help="查询歌曲的夸克分享链接").add_argument("--id", required=True)
    commands.add_parser("destination", help="检查入库目录配置").add_argument("--directory")
    share = commands.add_parser("share", help="浏览夸克分享文件")
    download = commands.add_parser("download", help="转存、下载、校验并入库")
    for command in (share, download):
        command.add_argument("--url", required=True)
        command.add_argument("--page", type=positive_int, default=1)
        command.add_argument("--folder-fid", default="0")
        command.add_argument("--passcode")
    download.add_argument("--file-fid", required=True)
    download.add_argument("--title", required=True)
    download.add_argument("--artist", required=True)
    download.add_argument("--version")
    download.add_argument("--album")
    download.add_argument("--format", default=DEFAULT_FORMAT, type=str.lower, choices=FORMATS, help="音频格式，默认 FLAC")
    download.add_argument("--directory")
    download.add_argument("--staging-fid", help="从已确认转存成功的暂存目录续接下载")
    lyrics = commands.add_parser("lyrics", help="为音乐库内的音频匹配和保存同名 LRC")
    lyrics.add_argument("--path", required=True, help="库内音频路径")
    for field in ("title", "artist", "album", "version"):
        lyrics.add_argument(f"--{field}", help="补充音频中缺少的标签")
    lyrics.add_argument("--source", choices=lyric_sources.SOURCES, help="只查询指定来源")
    lyrics.add_argument("--id", help="使用用户确认的歌词候选 ID，需同时指定 --source")
    args = parser.parse_args()
    if args.command == "lyrics" and args.id and not args.source:
        lyrics.error("--id 必须和 --source 一起使用。")
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
            result = share_page(Quark(context), args)
        else:
            result = download(args, context)
    except (SkillError, OSError, ValueError, KeyError, TypeError, AttributeError,
            sqlite3.Error, subprocess.TimeoutExpired) as error:
        if isinstance(error, SkillError) and error.required_setting:
            context["required_setting"] = error.required_setting
        message = "等待外部命令超时，请检查当前步骤的结果。" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        print(json.dumps({"status": "error", **context, "message": message}, ensure_ascii=False))
        return 1
    if "quark_staging" in context:
        result["quark_staging"] = context["quark_staging"]
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return int(result.get("status") == "error")


if __name__ == "__main__":
    raise SystemExit(main())
