"""通过官方 bdpan 浏览分享、转存选定音频并下载网盘副本。"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4


FILE_ID = re.compile(r"[1-9][0-9]*")
APP_ROOT = "/apps/bdpan/"
STAGING_DIR = re.compile(r"/apps/bdpan/music-download-[a-f0-9]{32}")
STAGING_LIST_LIMIT = 1000
AUTH_CODES = {-6, 13998, 13080, 13081}
REJECTED_CODES = {13003, 13004, 13041, 13061, 13071, 13072, 13073, 20013}


class BaiduError(ValueError):
    def __init__(self, message, kind=None, *, code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code


def numeric_field(value):
    # Python 的 JSON 整数可精确保存大 ID。拒绝浮点数，避免把已丢失精度的值当成 ID。
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value)):
        return int(value)
    return None


def share_options(args):
    return ["-p", args.passcode] if args.passcode else []


def valid_filename(name):
    return isinstance(name, str) and bool(name) and name not in (".", "..") and not re.search(r"[/\x00-\x1f\x7f]", name)


class Baidu:
    def __init__(self, context):
        self.context = context
        self.command = shutil.which(os.environ.get("MUSIC_BAIDU_CLI") or "bdpan")
        if not self.command:
            raise BaiduError("请安装官方 bdpan，或用 MUSIC_BAIDU_CLI 指定其可执行文件。", "configuration")

    def run(self, *options, stage, timeout=90):
        self.context["stage"] = stage
        try:
            result = subprocess.run([self.command, *options, "--json"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            # 命令参数可能含提取码，不把异常中的整条命令交给上层输出。
            raise BaiduError(f"等待百度网盘命令超时（{stage}）。") from None
        # 列表可能直接返回 JSON 数组，其他命令也可能使用 code/data 信封。
        # 有些版本把错误 JSON 写到 stderr，只读取结构化字段，避免回显凭据及分享参数。
        data = None
        for stream in (result.stdout, result.stderr):
            try:
                data = json.loads(stream)
            except ValueError:
                continue
            break
        envelope = data if isinstance(data, dict) else {}
        payload = envelope.get("data", data)
        record = payload if isinstance(payload, dict) else {}
        error = envelope.get("error")
        error_fields = error if isinstance(error, dict) else {}
        codes = [numeric_field(row.get(key)) for row in (record, envelope, error_fields)
                 for key in ("errno", "error_code", "code")]
        code = next((value for value in codes if value not in (0, 1, None)), None)
        failed = result.returncode or any(codes) or error or not isinstance(payload, (dict, list))
        diagnostic = result.stdout + "\n" + result.stderr if failed else ""
        if failed and code is None:
            # 部分版本只有文本错误码也要保留，后续据此区分是否需要重试或续查。
            match = re.search(r'(?:\berrno["\s]*[:=]\s*"?|错误码\s*[:：]?\s*)(-?[0-9]+)', diagnostic, re.I)
            if match:
                code = int(match[1])
        task = record.get("task_id") or envelope.get("task_id") or error_fields.get("task_id")
        task_id = str(task) if type(task) in (int, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(task)) else None
        submitted = "submitted" in (record.get("status"), envelope.get("status"))
        # 忙碌响应可能涉及其他任务。只有明确提交或属于本请求的任务信息才进入目录复查。
        if stage == "saveas" and (submitted or code == 13070 or (task_id and code not in REJECTED_CODES)):
            return {"status": "submitted", "task_id": task_id}
        if failed:
            if code == 13003:
                raise BaiduError("百度网盘分享需要有效的提取码。", "passcode", code=code)
            if code == 13004:
                raise BaiduError("百度网盘分享已失效或不存在。", "expired", code=code)
            if code == 13071:
                raise BaiduError("百度网盘有其他转存任务正在进行，请约 5 分钟后继续。", "busy", code=code)
            if code in AUTH_CODES or re.search(r"未登录|未授权|请先登录|not (?:logged in|authorized)", diagnostic, re.I):
                raise BaiduError("请通过官方 baidu-drive Skill 登录或检查百度网盘授权。", "auth", code=code)
            if re.search(r"unknown (?:command|flag)|未知命令|未知参数", diagnostic, re.I):
                if stage == "cleanup":
                    raise BaiduError("当前 bdpan 不支持自动删除暂存副本。", "configuration", code=code)
                raise BaiduError("请升级到支持 transfer list 和 transfer select 的官方 bdpan。", "configuration", code=code)
            suffix = f"（错误码 {code}）" if code is not None else ""
            raise BaiduError(f"百度网盘命令未返回成功结果{suffix}。", code=code)
        return payload

    def share_page(self, args, formats):
        source_dir = getattr(args, "source_dir", None)
        options = ["--source-dir", source_dir] if source_dir else []
        data = self.run("transfer", "list", args.url, *share_options(args), *options,
                        "--page", str(args.page), "--page-size", "100", stage="share")
        entries = None
        if isinstance(data, dict):
            entries = next((data[key] for key in ("items", "list", "files") if key in data), None)
        if not isinstance(entries, list) or data.get("has_more") not in (True, False, 0, 1):
            raise BaiduError("百度网盘未返回可分页的分享文件列表。")
        files = []
        for item in entries:
            path = item.get("path") or ""
            name = item.get("server_filename") or item.get("name") or Path(path).name
            directory = item.get("isdir", item.get("is_dir")) in (True, 1, "1")
            audio_format = Path(name).suffix.lower().lstrip(".")
            if not directory and audio_format not in formats:
                continue
            fid = item.get("fs_id") or item.get("fsid")
            fid = str(fid) if type(fid) in (int, str) else ""
            if not FILE_ID.fullmatch(fid) or not name or (directory and not path.startswith("/")):
                raise BaiduError("百度网盘分享条目缺少有效的文件 ID、名称或目录路径。")
            files.append({"fid": fid, "filename": name, "size": numeric_field(item.get("size")), "path": path,
                          "type": "folder" if directory else "audio", "format": None if directory else audio_format})
        return {"files": files, "next_page": args.page + 1 if entries and data["has_more"] else None}

    def locate(self, staging, *, cleanup=False):
        options = ["--limit", str(STAGING_LIST_LIMIT)] if cleanup else []
        entries = self.run("ls", staging["path"], *options, stage="cleanup" if cleanup else "locate")
        if not isinstance(entries, list):
            raise BaiduError("百度网盘未返回暂存目录的文件列表。")
        if cleanup and len(entries) >= STAGING_LIST_LIMIT:
            raise BaiduError("百度网盘未返回暂存目录的完整文件列表。")
        # 暂存目录仅接收一个文件，核对直属路径、名称和大小后才允许下载。
        path = staging["path"] + "/" + staging["filename"]
        named = [item for item in entries if item.get("path") == path]
        if not named and (cleanup or not entries):
            return None
        if (len(named) != 1 or named[0].get("isdir", named[0].get("is_dir")) not in (False, 0, "0")
                or numeric_field(named[0].get("size")) != staging["size"]):
            raise BaiduError("百度网盘暂存目录未能唯一匹配所选音频，请核对文件名和大小。")
        return named[0]

    def remove_staged_file(self, staging):
        name = staging.get("filename")
        if not STAGING_DIR.fullmatch(staging.get("path") or "") or not valid_filename(name) or name != name.strip():
            raise BaiduError("百度网盘暂存路径无效。")
        entry = self.locate(staging, cleanup=True)
        if entry is None:
            return
        fid = numeric_field(entry.get("fs_id") or entry.get("fsid"))
        if fid is None or fid <= 0 or fid != staging.get("remote_fsid"):
            raise BaiduError("百度网盘暂存文件 ID 已变化，请核对副本。")
        failure = None
        try:
            self.run("rm", entry["path"], "--force", stage="cleanup")
        except BaiduError as error:
            # 命令超时或响应不完整时，仍可用网盘目录确认删除是否生效。
            failure = error
        if self.locate(staging, cleanup=True) is not None:
            raise failure or BaiduError("百度网盘暂存文件仍可见，暂时无法确认删除结果。")

    def transfer(self, args, selected, local_dir):
        checkpoint = getattr(args, "_checkpoint", None)
        staging = dict(getattr(args, "_staging", None) or {})
        manual_path = getattr(args, "staging_dir", None)

        def remember(**fields):
            staging.update(fields)
            self.context["baidu_staging"] = dict(staging)
            if checkpoint:
                checkpoint(staging=dict(staging))

        name = selected["filename"]
        if not valid_filename(name):
            raise BaiduError("百度网盘分享未返回有效的音频文件名。")
        folder = staging.get("path") or manual_path or f"{APP_ROOT}music-download-{uuid4().hex}"
        if not STAGING_DIR.fullmatch(folder):
            raise BaiduError("请使用 baidu_staging.path 返回的本次暂存目录。")
        if not (staging or manual_path):
            self.run("mkdir", folder, stage="create_staging")
        remember(provider="baidu", path=folder, filename=name, size=selected["size"])

        delays = (0,)
        if not (manual_path or staging.get("submitted") or staging.get("saved")):
            # 在发出转存前记下可能已提交。进程中断、超时或结果不明确时只复查此目录。
            remember(submitted=True)
            try:
                result = self.run("transfer", "select", args.url, "--fsid", selected["fid"],
                                  "--dir", folder, *share_options(args), stage="saveas", timeout=960)
            except BaiduError as error:
                if error.code in REJECTED_CODES or error.kind == "configuration":
                    remember(submitted=False)
                    raise
                # 权限错误可能来自提交后的任务查询。保留原目录，再尝试用 ls 取证。
            else:
                if isinstance(result, dict) and result.get("task_id"):
                    remember(task_id=result["task_id"])
            delays = (0, 2, 4, 8)

        for delay in delays:
            if delay:
                time.sleep(delay)
            if remote := self.locate(staging):
                remember(saved=True, remote_fsid=numeric_field(remote.get("fs_id") or remote.get("fsid")))
                local = local_dir / f"audio.{args.format}"
                self.run("download", remote["path"], str(local), stage="download", timeout=900)
                if local.is_symlink() or not local.is_file():
                    raise BaiduError("百度网盘未在本地暂存目录生成音频文件。")
                return local
        raise BaiduError("百度网盘转存尚待核对，请稍后沿用原暂存目录继续。", "pending")
