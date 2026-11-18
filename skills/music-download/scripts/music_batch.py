"""批量下载的 SQLite 队列。查库、网盘传输和入库沿用单曲实现。

每首歌的状态、下次尝试时间和下载恢复点一起写入 SQLite。调度线程领取任务，
工作线程各自打开连接，在提交网盘操作或发布音频等关键步骤前保存恢复点。
"""

import argparse
import errno
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from uuid import uuid4


MAX_WORKERS = 10
MAX_RETRIES = 5
NEEDS_REVIEW_LIMIT = 10
BATCH_MATCHER_VERSION = 4
PLAYLIST_SCHEMA_VERSION = 1
OTHER_VERSION = re.compile(r"\b(?:dj|remix|cover|live|instrumental)\b|翻唱|现场|現場|伴奏|混音|重混|加速|纯享", re.IGNORECASE)
STATES = ("queued", "running", "retrying", "waiting_transfer", "saved", "already_in_library", "duplicate", "needs_review",
          "no_source", "failed")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, options TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
    job TEXT NOT NULL, position INTEGER NOT NULL, title_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    details TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (job, position)
);
CREATE INDEX IF NOT EXISTS items_queue ON items (job, status, retry_at, position);
"""


def bounded_int(value, maximum):
    number = int(value)
    if not 1 <= number <= maximum:
        raise argparse.ArgumentTypeError(f"请输入 1 到 {maximum} 之间的整数。")
    return number


def add_commands(commands, formats, default_format):
    add = commands.add_parser("batch-add", help="从歌名清单建立持久化下载批次")
    inputs = add.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", help="UTF-8 文本、JSON 曲目数组或酷狗导出的 JSON 清单")
    inputs.add_argument("--consume-manifest", type=Path, help="导入酷狗临时清单，批次提交后清理文件")
    add.add_argument("--allow-partial", action="store_true", help="确认导入部分读取的歌单")
    add.add_argument("--directory")
    add.add_argument("--format", choices=formats, type=str.lower, default=default_format)
    add.add_argument("--flac-only", action="store_true", help="FLAC 超限时保留无损要求")
    run = commands.add_parser("batch-run", help="开始或恢复批量下载")
    run.add_argument("--workers", type=lambda value: bounded_int(value, MAX_WORKERS), default=MAX_WORKERS)
    run.add_argument("--continue-review", action="store_true", help="用户确认后继续处理待确认项之外的剩余队列")
    status = commands.add_parser("batch-status", help="查看批次统计和逐曲记录")
    status.add_argument("--state", choices=STATES)
    status.add_argument("--offset", type=int, default=0)
    status.add_argument("--limit", type=lambda value: bounded_int(value, 1000), default=50)
    select = commands.add_parser("batch-select", help="为待确认曲目选定来源并重新排队")
    select.add_argument("--item", type=int, required=True, help="批次内的曲目编号")
    select.add_argument("--track-id", required=True, help="已确认的下歌吧条目 ID")
    select.add_argument("--url", help="指定已确认的夸克或百度网盘分享，与 --file-fid 一起传入")
    select.add_argument("--file-fid")
    select.add_argument("--folder-fid", default="0")
    select.add_argument("--source-dir", help="百度分享子目录的 path")
    select.add_argument("--passcode", help="分享提取码")
    select.add_argument("--page", type=int, default=1)
    for command in (run, status, select):
        command.add_argument("--job", required=True)


def dumps(data):
    return json.dumps(data, ensure_ascii=False)


@contextmanager
def open_queue(path):
    # 曲目续接使用的分享链接可能含提取码，限制新建数据库文件的读取权限。
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    with closing(sqlite3.connect(path, timeout=30)) as db:
        db.executescript(SCHEMA)
        yield db


@contextmanager
def run_lock(path):
    # 系统锁只由执行进程持有。进程退出后可接管，查询进度无需取得此锁。
    import fcntl

    with path.with_name(path.name + ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("当前队列已有运行中的批次，可用 batch-status 查看进度。") from None
        yield


def playlist_rows(manifest, allow_partial):
    """核对酷狗导出清单的读取状态，按原顺序返回曲目。"""
    version = manifest.get("schema_version")
    if manifest.get("source") != "kugou" or type(version) is not int or version != PLAYLIST_SCHEMA_VERSION:
        raise ValueError("JSON 对象须为版本 1 的酷狗歌单清单。")
    rows = manifest.get("tracks")
    status = manifest.get("read_status")
    expected = manifest.get("expected_count")
    fetched = manifest.get("fetched_count")
    if (not isinstance(rows, list) or type(fetched) is not int or fetched != len(rows)
            or (expected is not None and (type(expected) is not int or expected < 0))):
        raise ValueError("酷狗清单的曲目计数或 tracks 数组无效。")
    if status not in ("complete", "partial") or (status == "complete" and expected != fetched):
        raise ValueError("酷狗清单的读取状态与曲目计数不符。")
    if status == "partial" and not allow_partial:
        expected_label = "未知" if expected is None else str(expected)
        raise ValueError(f"酷狗歌单只读取了部分曲目（实际 {fetched} 首，预期 {expected_label} 首）。"
                         "确认处理已读取曲目后，请加 --allow-partial。")

    # 位置按原清单的数组顺序编号。集中报告缺歌名的位置，便于一次核对或补齐。
    missing = []
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"酷狗清单第 {position} 首应为曲目对象。")
        title = row.get("title")
        if title is None or (isinstance(title, str) and not title.strip()):
            missing.append(position)
    if missing:
        positions = "、".join(map(str, missing))
        raise ValueError(f"酷狗清单第 {positions} 首缺少歌名，请补齐后再导入。")
    return rows


def load_input(path, music, *, allow_partial=False, require_playlist=False):
    raw = Path(path).expanduser().read_text(encoding="utf-8-sig")
    if require_playlist or raw.lstrip().startswith(("[", "{")):
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = playlist_rows(rows, allow_partial)
        elif require_playlist:
            raise ValueError("临时清单须为酷狗歌单 JSON 对象。")
        elif not isinstance(rows, list):
            raise ValueError("JSON 清单应为曲目数组或酷狗歌单清单。")
    else:
        rows = [line.strip() for line in raw.splitlines() if line.strip()]
    if not rows:
        raise ValueError("清单没有曲目。")
    for position, row in enumerate(rows, 1):
        if isinstance(row, str):
            artist, separator, title = row.partition(" - ")
            row = {"artist": artist, "title": title} if separator else {"title": row}
        if not isinstance(row, dict):
            raise ValueError(f"第 {position} 首的清单格式无效。")
        track = {}
        for field in ("title", "artist", "album", "version"):
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"第 {position} 首的 {field} 需为字符串。")
            track[field] = (value or "").strip()
        track["title"], version = music.song_version(track["title"], track["version"])
        track["version"] = version or ""
        if not track["title"]:
            raise ValueError(f"第 {position} 首缺少歌名。")
        yield track


def add_job(db, args, music):
    if args.flac_only and args.format != "flac":
        raise ValueError("--flac-only 需与 FLAC 格式一起使用。")
    target = music.destination_dir(args.directory)
    handoff = args.consume_manifest
    if handoff is not None:
        handoff = handoff.expanduser()
        if handoff.parent.name != "kugou-handoff" or not re.fullmatch(r"[0-9a-f]{32}\.json", handoff.name):
            raise ValueError("--consume-manifest 需要 tracks --for-download 生成的临时清单路径。")
        if handoff.is_symlink():
            raise ValueError("临时清单路径须指向文件本身。")
    job = handoff.stem if handoff is not None else uuid4().hex
    input_path = handoff if handoff is not None else args.input
    options = {"root": str(music.library_root()), "directory": str(target), "format": args.format,
               "flac_only": args.flac_only, "matcher_version": BATCH_MATCHER_VERSION}
    seen = {}
    # 清单解析和写入共用一个事务。后半段有无效条目时，前半段也会回滚。
    # 重复曲目仍保留原编号，方便按用户提供的清单核对结果。
    with db:
        prior = None
        if handoff is not None:
            # 同一份交接清单在重试时复用已提交的批次，即使文件已清理。
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT options FROM jobs WHERE id = ?", (job,)).fetchone()
        if prior is not None:
            if json.loads(prior[0]) != options:
                raise ValueError(f"该清单已创建批次 {job}，请沿用原目录和格式参数。")
        else:
            db.execute("INSERT INTO jobs VALUES (?, ?)", (job, dumps(options)))
            tracks = load_input(input_path, music, allow_partial=args.allow_partial, require_playlist=handoff is not None)
            for position, track in enumerate(tracks, 1):
                key = tuple(music.song_key(value) for value in track.values())
                first = seen.setdefault(key, position)
                state = {"track": track, "errors": []}
                if position != first:
                    state["duplicate_of"] = first
                db.execute("INSERT INTO items(job, position, title_key, status, details) VALUES (?, ?, ?, ?, ?)",
                           (job, position, key[0], "queued" if first == position else "duplicate", dumps(state)))
    result = report(db, job)
    if handoff is not None:
        try:
            handoff.unlink(missing_ok=True)
        except OSError:
            result["manifest_cleanup_warning"] = "批次已创建，临时清单未能删除。可用原参数重试清理。"
    return result


def options_for(db, job):
    row = db.execute("SELECT options FROM jobs WHERE id = ?", (job,)).fetchone()
    if row is None:
        raise ValueError("没有找到该批次。")
    return json.loads(row[0])


def report(db, job, args=None):
    info = options_for(db, job)
    counts = dict.fromkeys(STATES, 0)
    counts.update(db.execute("SELECT status, COUNT(*) FROM items WHERE job = ? GROUP BY status", (job,)))
    result = {"job": job, "directory": info["directory"], "format": info["format"], "counts": counts,
              "total": sum(counts.values()), "max_retries": MAX_RETRIES}
    if args is None:
        return result
    if args.offset < 0:
        raise ValueError("--offset 不能小于 0。")
    where = "job = ?"
    params = (job,)
    if args.state:
        where += " AND status = ?"
        params += (args.state,)
    rows = db.execute(f"SELECT position, status, details, retry_at FROM items WHERE {where} "
                      "ORDER BY position LIMIT ? OFFSET ?", (*params, args.limit + 1, args.offset)).fetchall()
    result["items"] = []
    for position, status, raw, retry_at in rows[:args.limit]:
        details = json.loads(raw)
        entry = {key: details[key] for key in ("track", "errors", "result", "staging", "previous_staging", "stage",
                                                "reason", "candidates", "duplicate_of", "limit_bytes",
                                                "format_fallback") if key in details}
        # 分享参数可能包含提取码。详情只输出判断与续接需要的字段。
        if retry_at:
            entry["retry_at"] = retry_at
        result["items"].append({"position": position, "status": status, **entry})
    result["next_offset"] = args.offset + args.limit if len(rows) > args.limit else None
    return result


def select_item(db, args, music, *, allow_queued_review=False):
    options_for(db, args.job)
    music.require_id(args.track_id, "歌曲 ID", pattern=music.ID_PATTERN)
    selection = {"track_id": args.track_id}
    if args.url or args.file_fid or args.passcode or args.source_dir:
        provider = music.share_provider(args.url)
        if not provider or args.page < 1:
            raise ValueError("请同时提供有效的 --url、--file-fid 和页码。")
        if provider == "baidu":
            if args.folder_fid != "0":
                raise ValueError("百度网盘分享子目录请用 --source-dir。")
            music.require_id(args.file_fid, "分享文件 ID", pattern=music.BAIDU_FILE_ID)
        else:
            if args.source_dir:
                raise ValueError("夸克分享子目录请用 --folder-fid。")
            music.require_id(args.file_fid, "分享文件 ID")
            music.require_id(args.folder_fid, "分享目录 ID")
        selection["file"] = {"url": args.url, "file_fid": args.file_fid,
                             "folder_fid": args.folder_fid, "source_dir": args.source_dir,
                             "page": args.page, "passcode": args.passcode}
    # 在写事务内读取并更新状态，让人工选源和调度领取按顺序生效。
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status, details FROM items WHERE job = ? AND position = ?",
                         (args.job, args.item)).fetchone()
        if not row:
            raise ValueError("请选择待确认或无音源的曲目编号。")
        prior = json.loads(row[1])
        queued_review = (allow_queued_review and row[0] == "queued" and prior.get("candidates")
                         and not (prior.get("selection") or prior.get("file") or prior.get("staging")))
        if row[0] not in {"needs_review", "no_source"} and not queued_review:
            raise ValueError("请选择待确认或无音源的曲目编号。")
        details = {"track": prior["track"], "selection": selection, "errors": prior["errors"]}
        write_item(db, args.job, args.item, "queued", details)
    return report(db, args.job)


def resolve_track(music, track, selection):
    """根据人工选择读取详情，或按清单的歌手、专辑和版本搜集候选。"""
    if selection:
        item = music.track_detail(argparse.Namespace(id=selection["track_id"]))
        title, version = music.lyric_song_version(item["title"], track["version"])
        version = item.get("version") or version or ""
        if title_key(music, title) != title_key(music, track["title"]) or not item.get("artist"):
            raise ValueError("选中的条目与歌名不符，或缺少歌手。")
        return [(item, version)]
    page = 1
    choices = {}
    while page is not None and page <= 10:
        result = music.search_tracks(argparse.Namespace(title=track["title"], artist=track["artist"], page=page))
        for item in result["tracks"]:
            title, version = music.lyric_song_version(item["title"] or "", track["version"])
            version = item.get("version") or version or ""
            if title_key(music, title) != title_key(music, track["title"]) or not item.get("artist"):
                continue
            if track["artist"] and music.artist_key(item["artist"]) != music.artist_key(track["artist"]):
                continue
            if music.song_key(version) != music.song_key(track["version"]):
                continue
            if not track["version"] and OTHER_VERSION.search(item.get("album") or ""):
                continue
            if track["album"] and item.get("album") and music.song_key(item["album"]) != music.song_key(track["album"]):
                continue
            choices[item["id"]] = (item, version)
        page = result["next_page"]
    return list(choices.values())


def title_key(music, title):
    return re.sub(r" +(?=\()", "", music.song_key(title))


def confirmed_release(music, track):
    """仅在两个独立曲库都能核实同一个正式专辑条目时，自动补齐演唱者。"""
    releases = []
    title_key = music.song_key(track["title"])
    for source in ("netease", "qq"):
        try:
            rows = music.lyric_sources.search(source, track["title"], "", 0)
        except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
            return None
        releases.append({(music.artist_key(row["artist"]), music.song_key(row["album"]))
                         for row in rows if row["artist"] and row["album"]
                         and music.song_key(row["title"]) == title_key
                         and music.song_key(row["album"]) != title_key
                         and not OTHER_VERSION.search(row["album"])})
    shared = releases[0] & releases[1]
    return next(iter(shared)) if len(shared) == 1 else None


def file_title(music, filename, track):
    stem = Path(filename).stem.strip()
    stem = re.sub(r"^\d+[ ._、-]+", "", stem)
    return music.lyric_song_version(stem, track["version"])


def matching_filename(music, filename, track):
    base, version = file_title(music, filename, track)
    if version and music.song_key(version) != music.song_key(track["version"]):
        return False
    key = title_key(music, base)
    aliases = [track["title"]]
    for divider in (" - ", "-", " – ", " — ", "—"):
        aliases.extend((track["artist"] + divider + track["title"], track["title"] + divider + track["artist"]))
    return key in {title_key(music, alias) for alias in aliases}


def find_file(music, context, track, audio_format, url, limit):
    """逐页核对同一个分享。百度用目录路径，夸克用 FID。"""
    context["stage"] = "configuration"
    baidu = music.share_provider(url) == "baidu"
    args = argparse.Namespace(url=url, folder_fid="0", source_dir=None, passcode=None)
    client = music.share_client(args, context)
    if not baidu:
        client.capture_status = True
    queue = deque([(None if baidu else "0", 1)])
    visited = set()
    excluded = {file["file_fid"] for file in context.get("oversized_files", ()) if file["url"] == url}
    while queue and len(visited) < 30:
        folder, page = queue.popleft()
        if (folder, page) in visited:
            continue
        visited.add((folder, page))
        args.page = page
        if baidu:
            args.source_dir = folder
        else:
            args.folder_fid = folder
        response = music.browse_share(client, args)
        found = []
        for item in response["files"]:
            if item["type"] == "folder":
                child = item.get("path" if baidu else "fid")
                if child:
                    queue.append((child, 1))
            elif (item["format"] == audio_format and item["fid"] and item["fid"] not in excluded
                  and isinstance(item["size"], int) and item["size"] > 0
                  and (baidu or limit is None or item["size"] <= limit)
                  and matching_filename(music, item["filename"], track)):
                found.append(item)
        if found:
            # 同页有多个匹配文件时，优先采用文件名也带歌手的那份。
            title = title_key(music, track["title"])
            item = min(found, key=lambda entry: title_key(music, file_title(music, entry["filename"], track)[0]) == title)
            return {"url": url, "file_fid": item["fid"], "folder_fid": args.folder_fid, "source_dir": args.source_dir,
                    "page": page, "filename": item["filename"], "size": item["size"]}, None
        if response["next_page"]:
            queue.append((folder, response["next_page"]))
    return None, [] if queue else None


def local_match(music, track, audio_format):
    args = argparse.Namespace(**track)
    for row in music.lookup_library(args)["tracks"]:
        if (row["format"] != audio_format or music.song_key(row["version"] or "") != music.song_key(track["version"])
                or (not track["version"] and OTHER_VERSION.search(row["album"] or ""))
                or (track["album"] and music.song_key(row["album"] or "") != music.song_key(track["album"]))):
            continue
        # 命中已有文件时仍沿用单曲的歌词流程，已有同名 LRC 由该流程保留。
        row["lyrics"] = music.save_lyrics(Path(row["path"]), args)
        return row
    return None


def restore_published(music, args, record, part):
    """核对上次的发布记录，必要时发布已经校验过的本地暂存文件。"""
    path = Path(record["path"])
    # 进程可能在发布音频后、写入索引前退出。优先核对已存在的目标文件。
    # 哈希不一致时保留现有文件，由队列记下这次失败。
    source = path if os.path.lexists(path) else part
    if source is None or not os.path.lexists(source):
        return None
    if (not source.is_file() or source.is_symlink() or source.stat().st_size != record["size"]
            or music.sha256(source).hex() != record["sha256"]):
        raise ValueError("中断期间目标文件内容发生变化，请检查目标路径。")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("恢复音频校验需要 ffprobe。")
    audio = music.validate_audio(source, args.format, record["size"], ffprobe)
    if source == part:
        music.publish(part, path)
    return music.finish_download(args, path, audio, status="saved")


def source_order(options):
    if options["format"] == "flac" and not options["flac_only"]:
        return (("quark", "flac"), ("quark", "mp3"), ("baidu", "flac"), ("baidu", "mp3"))
    return (("quark", options["format"]), ("baidu", options["format"]))


def choose_source(music, context, track, options, state):
    """返回可写入的选源恢复点，或结束当前曲目的状态和原因。"""
    context["stage"] = "search"
    selection = state.get("selection")
    limit = state.get("limit_bytes")
    choices = resolve_track(music, track, selection)
    if not choices:
        return "no_source", {"reason": "没有找到匹配歌曲的网盘来源。"}

    # 只给歌名时，使用两个曲库共同指向的一条录音发行信息来缩小候选范围。
    # 任一来源无法核实、交叉结果不唯一或候选缺少专辑时，保留人工确认入口。
    if not (track["artist"] or selection or track["version"]):
        release = confirmed_release(music, track)
        verified = [(item, version) for item, version in choices if item.get("album")
                    and (music.artist_key(item["artist"]), music.song_key(item["album"])) == release]
        if verified:
            choices = verified
            track = {**track, "artist": verified[0][0]["artist"], "album": verified[0][0]["album"]}

    if not selection and not track["artist"]:
        return "needs_review", {"reason": "需要确认演唱者。",
                                "candidates": [item for item, _ in choices]}

    requested_format = options["format"]
    supplied_file = selection.get("file") if selection and not limit else None
    sources = {pair: [] for pair in source_order(options)}
    source_errors = []
    detail_conflict = None
    recordings = []
    for item, version in sorted(choices, key=lambda choice: not choice[0].get("album")):
        resolved = {"title": track["title"], "artist": item["artist"], "version": version,
                    "album": item.get("album") or track["album"]}
        if supplied_file:
            return None, {"resolved": resolved, "file": supplied_file, "file_format": requested_format}

        context["stage"] = "detail"
        try:
            detail = item if "downloads" in item else music.track_detail(argparse.Namespace(id=item["id"]))
        except (music.SkillError, OSError, HTTPException, ValueError) as error:
            # 排查另一个条目失败时，仍可使用已经确认的分享。
            source_errors.append((error, context.copy(), item, None))
            continue
        detail_title, detail_version = music.lyric_song_version(detail["title"], version)
        album = detail.get("album") or ""
        version_key = music.song_key(version)
        # 专辑只在请求明确指定时限制选源。歌名、歌手和已标注的版本仍需一致。
        if (title_key(music, detail_title) != title_key(music, resolved["title"])
                or music.artist_key(detail["artist"] or "") != music.artist_key(resolved["artist"])
                or music.song_key(detail.get("version") or detail_version or "") != version_key
                or (detail_version and music.song_key(detail_version) != version_key)
                or (not selection and not version and OTHER_VERSION.search(album))
                or (not selection and track["album"] and music.song_key(album or item.get("album") or "")
                    != music.song_key(track["album"]))):
            if detail_conflict is None:
                detail_conflict = [music.track_fields(item), music.track_fields(detail)]
            continue
        resolved["album"] = album or resolved["album"]
        recordings.append(resolved)
        for url, file_format in dict.fromkeys((entry["url"], entry["format"]) for entry in detail["downloads"]):
            provider = music.share_provider(url)
            for pair, candidates in sources.items():
                if pair[0] == provider and file_format in (None, pair[1]):
                    candidates.append((url, resolved, item))

    incomplete = None
    checked_library = set()
    context["oversized_files"] = state.get("oversized_files", ())
    for (provider, audio_format), candidates in sources.items():
        if audio_format not in checked_library:
            checked_library.add(audio_format)
            context["stage"] = "lookup"
            for resolved in recordings:
                if existing := local_match(music, resolved, audio_format):
                    return "already_in_library", {"result": existing}
        if provider == "quark" and audio_format == state.get("quark_limited_format"):
            continue
        checked = set()
        for url, resolved, item in candidates:
            link = (url, resolved["artist"], resolved["version"])
            if link in checked:
                continue
            checked.add(link)
            # 一个分享失效、缺提取码或网盘尚未配置时，仍可尝试同录音同格式的其他链接。
            attempt = context.copy()
            try:
                selected_file, ambiguous = find_file(music, attempt, resolved, audio_format, url, limit)
            except (music.SkillError, music.BaiduError, subprocess.TimeoutExpired) as error:
                if getattr(error, "kind", None) != "expired":
                    source_errors.append((error, attempt, item, url))
                continue
            if ambiguous is not None:
                incomplete = incomplete or {**music.track_fields(item), "files": ambiguous}
            if selected_file:
                return None, {"resolved": resolved, "file": selected_file, "file_format": audio_format}
    if incomplete is not None:
        return "needs_review", {"reason": "未能完整核对分享目录。", "candidates": [incomplete]}
    if detail_conflict is not None:
        return "needs_review", {"reason": "搜索条目与歌曲详情的演唱者或版本信息不一致。",
                                "candidates": detail_conflict}
    if source_errors:
        error, attempt, item, url = next((entry for entry in source_errors if getattr(entry[0], "kind", None) != "passcode"),
                                        source_errors[0])
        if getattr(error, "kind", None) == "passcode":
            return "needs_review", {"reason": str(error), "candidates": [{**music.track_fields(item),
                                      "share": url.split("?", 1)[0].split("#", 1)[0]}]}
        context.update(attempt)
        raise error
    formats = "、".join(dict.fromkeys(audio_format.upper() for _, audio_format in sources))
    return "no_source", {"reason": f"没有找到与歌名、歌手及指定版本匹配的 {formats} 文件。"}


def process_track(music, options, state, context, checkpoint):
    """沿用已落盘的文件，重新选源时按网盘和音质优先级推进。"""
    while True:
        try:
            status, outcome = download_track(music, options, state, context, checkpoint)
        except music.DownloadSizeLimit as error:
            previous_staging = state.get("staging")
            context.pop("quark_staging", None)
            file = state["file"]
            oversized = [*state.get("oversized_files", ()), {key: file[key] for key in ("url", "file_fid")}]
            checkpoint(file=None, staging=None, part=None, publish=None, file_format=None,
                       limit_bytes=error.limit_bytes, oversized_files=oversized,
                       previous_staging=previous_staging or state.get("previous_staging"))
            continue

        if status == "no_source" and (state.get("oversized_files") or state.get("quark_limited_format")):
            return "failed", {"reason": f"夸克文件超过单文件上限（{state['limit_bytes']} 字节），且没有其他匹配来源。",
                              "stage": "download"}
        return status, outcome


def resume_format(options, state):
    if state.get("file_format"):
        return state["file_format"]
    # 旧队列通过降级记录推断在途文件的格式。更早的队列只记录夸克上限。
    legacy_fallback = state.get("limit_bytes") and not state.get("quark_limited_format")
    if options["format"] == "flac" and (state.get("format_fallback") or legacy_fallback):
        return "mp3"
    return options["format"]


def download_track(music, options, state, context, checkpoint):
    track = state.get("resolved") or state["track"]
    audio_format = resume_format(options, state)
    context["stage"] = "lookup"
    part = Path(state["part"]) if state.get("part") else None
    result = None
    if record := state.get("publish"):
        args = argparse.Namespace(**track, format=audio_format)
        result = restore_published(music, args, record, part)
        staging = state.get("staging") or {}
        if result is not None:
            music.cleanup_staging(result, staging, checkpoint=checkpoint)
        elif staging.get("cleanup_started"):
            # NAS 文件已缺失，原网盘副本可能已删除。从已选分享重新转存。
            checkpoint(publish=None, part=None, staging=None)
    # 没有发布记录时，残留的 .part 可能只写了一部分。删除后从网盘恢复点继续。
    if part is not None:
        part.unlink(missing_ok=True)
    if result is None:
        if track["artist"] and not (state.get("selection") or state.get("file") or state.get("staging")):
            track = state["track"]
            audio_format = options["format"]
            if existing := local_match(music, track, audio_format):
                return "already_in_library", {"result": existing}
        if not state.get("file"):
            status, outcome = choose_source(music, context, state["track"], options, state)
            if status is not None:
                return status, outcome
            audio_format = outcome["file_format"]
            outcome["format_fallback"] = None
            if audio_format != options["format"]:
                outcome["format_fallback"] = {"from": options["format"], "to": audio_format,
                                               "reason": "按网盘和音质优先级选择可用来源。"}
            # 将录音信息与分享文件一起写入，恢复时按同一录音继续。
            checkpoint(**outcome)
            track = state["resolved"]
        staging = state.get("staging") or {}
        share = {key: state["file"].get(key) for key in ("file_fid", "folder_fid", "source_dir", "page", "url", "passcode")}
        args = argparse.Namespace(**track, **share, directory=options["directory"], format=audio_format,
                                  staging_fid=staging.get("fid"), _staging=staging, _checkpoint=checkpoint)
        result = music.download_file(args, context, limit_bytes=state.get("limit_bytes"))
    # 音频已经发布但索引写入失败时，留在队列中，下次从发布记录恢复并补写索引。
    if result.get("index_warning"):
        return "paused", {"reason": result["index_warning"]}
    return "saved", {"result": result}


def write_item(db, job, position, status, details, retry_at=0):
    # 由调用方决定事务范围。状态、恢复点和下次重试时间始终一起更新。
    db.execute("UPDATE items SET status = ?, details = ?, retry_at = ? WHERE job = ? AND position = ?",
               (status, dumps(details), retry_at, job, position))


def run_item(path, job, position, state, options, music):
    with closing(sqlite3.connect(path, timeout=30)) as db:
        context = {"stage": "lookup"}

        def checkpoint(**fields):
            # 单曲流程在关键步骤传入可序列化字段。每次写入提交后才继续下一步。
            state.update(fields)
            with db:
                write_item(db, job, position, "running", state)

        retry_at = 0
        cooldown_until = 0
        try:
            status, outcome = process_track(music, options, state, context, checkpoint)
        except (music.SkillError, OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError,
                subprocess.TimeoutExpired, sqlite3.Error) as error:
            stage = context["stage"]
            message = "等待外部命令超时。" if isinstance(error, subprocess.TimeoutExpired) else str(error)
            message = re.sub(r"https?://\S+", "[链接]", message)[:300]
            quark_code = context.get("quark_code")
            kind = error.kind if isinstance(error, music.BaiduError) else None
            shared_failure = (quark_code in (-103, -104, 32003, 41035, 41014) or "未登录" in message or "未授权" in message
                              or kind in {"auth", "configuration"}
                              or isinstance(error, sqlite3.Error) or stage == "configuration"
                              or (isinstance(error, OSError) and error.errno in
                                  {errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM}))
            limited = (429 in (getattr(error, "code", None), quark_code) or "限流" in message
                       or "too many requests" in message.lower())
            outcome = {"reason": message, "stage": stage}
            if kind in {"pending", "busy"}:
                # 异步转存不占用下载重试。下一次运行通过原暂存点核对，服务端忙时至少等五分钟。
                status = "waiting_transfer"
                retry_at = time.time() + 300 if kind == "busy" else 0
            elif kind == "passcode":
                status = "needs_review"
            elif shared_failure:
                # 账号、存储或索引故障影响整批。通知调度器暂停，本曲暂不消耗重试次数。
                status = "paused"
            else:
                # errors 记录每次失败，含首次尝试。退避时间持久化，重启后仍按原时间重试。
                errors = state["errors"]
                errors.append({"attempt": len(errors) + 1, "stage": stage, "message": message, "time": time.time()})
                status = "retrying" if len(errors) <= MAX_RETRIES else "failed"
                delay = 2 ** min(len(errors), MAX_RETRIES)
                retry_after = (getattr(error, "headers", None) or {}).get("Retry-After", "")
                if limited and retry_after:
                    try:
                        # HTTP Retry-After 可以是秒数或绝对日期。至少保留本地退避时长。
                        server_delay = (int(retry_after) if retry_after.isdecimal()
                                        else parsedate_to_datetime(retry_after).timestamp() - time.time())
                        delay = max(delay, server_delay)
                    except (ValueError, OverflowError):
                        pass
                deadline = time.time() + delay
                if status == "retrying":
                    retry_at = deadline
                if limited:
                    cooldown_until = deadline

        if "reason" not in outcome:
            for key in ("reason", "stage", "candidates"):
                state.pop(key, None)
        state.update(outcome)
        with db:
            write_item(db, job, position, "queued" if status == "paused" else status, state, retry_at)
        return outcome["reason"] if status == "paused" else None, cooldown_until


def claim_next(db, job, active_titles):
    # 查询和标记 running 使用同一写事务。仅领取已到期且当前没有同名任务的曲目。
    exclude = ",".join("?" for _ in active_titles)
    filter_active = f" AND title_key NOT IN ({exclude})" if active_titles else ""
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT position, title_key, details FROM items WHERE job = ? AND status IN ('queued', 'retrying') "
                         f"AND retry_at <= ?{filter_active} ORDER BY retry_at, position LIMIT 1",
                         (job, time.time(), *active_titles)).fetchone()
        if row:
            db.execute("UPDATE items SET status = 'running', retry_at = 0 WHERE job = ? AND position = ?", (job, row[0]))
    return row


def recover_queue(db, job, root):
    with db:
        # 上个进程已退出。Running 只说明任务被领取，继续使用已落盘的阶段和文件。
        db.execute("UPDATE items SET status = 'queued' WHERE job = ? AND (status = 'running' "
                   "OR (status = 'waiting_transfer' AND retry_at <= ?))", (job, time.time()))
        # NAS 上的已入库文件可能被移动或移除。复查路径和大小，沿用原恢复点重新排队。
        rows = db.execute("SELECT position, details FROM items WHERE job = ? AND status IN ('saved', 'already_in_library')",
                          (job,)).fetchall()
        for position, raw in rows:
            state = json.loads(raw)
            result = state["result"]
            path = Path(result["path"])
            if path.resolve() == path and path.is_relative_to(root) and path.is_file() and path.stat().st_size == result["size"]:
                continue
            state.pop("result")
            state["reason"] = "上次记录的音频已缺失或大小变化，需要重新核对。"
            write_item(db, job, position, "queued", state)


def run_job(path, db, args, music):
    job = args.job
    with run_lock(path):
        options = options_for(db, job)
        if str(music.library_root()) != options["root"]:
            raise ValueError("当前 MUSIC_LIBRARY_ROOT 与创建批次时不同。")
        if str(music.destination_dir(options["directory"])) != options["directory"]:
            raise ValueError("批次保存目录已改变，请恢复原路径。")
        if options.get("matcher_version") != BATCH_MATCHER_VERSION:
            # 重新核对旧批次的无音源条目，曾因夸克超限跳过的百度 FLAC 也可重新检查。
            options["matcher_version"] = BATCH_MATCHER_VERSION
            with db:
                db.execute("UPDATE jobs SET options = ? WHERE id = ?", (dumps(options), job))
                for position, raw in db.execute("SELECT position, details FROM items WHERE job = ? AND status = 'no_source'",
                                                (job,)).fetchall():
                    state = json.loads(raw)
                    state.pop("format_fallback", None)
                    if options["format"] == "flac" and state.get("limit_bytes") and not state.get("quark_limited_format"):
                        state["quark_limited_format"] = "flac"
                    write_item(db, job, position, "queued", state)
        recover_queue(db, job, Path(options["root"]))
        active = {}
        paused = None
        workers = args.workers
        cooldown = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                # 同名曲目可能匹配到同一目的文件。已有同名下载时，先领取其他曲目。
                while (not paused and time.time() >= cooldown and len(active) < workers
                       and (row := claim_next(db, job, tuple(active.values())))):
                    position, title_key, raw = row
                    task = pool.submit(run_item, path, job, position, json.loads(raw), options, music)
                    active[task] = title_key
                if active:
                    # 任意任务完成就释放一个位置。遇到限流时让在途任务收尾，冷却后降低并发。
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for task in done:
                        active.pop(task)
                        reason, retry_after = task.result()
                        paused = paused or reason
                        review_count = db.execute(
                            "SELECT COUNT(*) FROM items WHERE job = ? AND status = 'needs_review'", (job,)
                        ).fetchone()[0]
                        if not args.continue_review and not paused and review_count >= NEEDS_REVIEW_LIMIT:
                            paused = f"待确认曲目已达到 {NEEDS_REVIEW_LIMIT} 首，请先确认后再继续。"
                        if retry_after:
                            cooldown = max(cooldown, retry_after)
                            workers = max(1, workers // 2)
                    continue
                if paused:
                    break
                due = db.execute("SELECT MIN(retry_at) FROM items WHERE job = ? AND status IN ('queued', 'retrying')",
                                 (job,)).fetchone()[0]
                if due is None:
                    break
                # 没有可立即运行的曲目时等待最早可派发时间，分段等待以便发现人工补选。
                time.sleep(max(0, min(10, max(due, cooldown) - time.time())))
        result = report(db, job)
        if paused:
            result["paused"] = paused
        return result


def dispatch(args, music):
    override = os.environ.get("MUSIC_BATCH_DB")
    path = Path(override).expanduser() if override else music.index_path().with_name("batches.sqlite3")
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open_queue(path) as db:
        if args.command == "batch-add":
            return add_job(db, args, music)
        if args.command == "batch-select":
            return select_item(db, args, music)
        if args.command == "batch-status":
            return report(db, args.job, args)
        return run_job(path, db, args, music)
