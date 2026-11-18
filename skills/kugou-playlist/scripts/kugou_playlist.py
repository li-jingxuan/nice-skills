#!/usr/bin/env python3
"""Read KuGou playlists through a short-lived local KuGouMusicApi process."""

import argparse
import base64
import binascii
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from uuid import uuid4


SCRIPTS = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
# 歌单交接的格式单独版本化，登录会话变更时仍能沿用已导出的清单。
MANIFEST_SCHEMA_VERSION = 1
QR_LIFETIME = 300
MAX_PAGES = 200
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PLAYLIST_ROUTE = '/user/playlist'
TRACK_ROUTE = '/playlist/track/all/new'
FALLBACK_ROUTE = '/playlist/track/all'
DEVICE_COOKIES = {
    'KUGOU_API_PLATFORM', 'KUGOU_API_MID', 'KUGOU_API_GUID', 'KUGOU_API_DEV',
    'KUGOU_API_MAC', 'KUGOU_API_WEBGL', 'dfid',
}
SESSION_COOKIES = DEVICE_COOKIES | {'token', 'userid', 't1'}
REQUEST_ID = re.compile(r'[0-9a-f]{32}')
AUDIO_EXTENSION = re.compile(r'\.(?:mp3|flac|m4a|aac|wav|ape|ogg|opus|aiff|wma)$', re.I)


class SkillError(Exception):
    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def text(value):
    if isinstance(value, str):
        return value.strip() or None
    return None


def identifier(value):
    if type(value) is int:
        return str(value) if value > 0 else None
    result = text(value)
    return result if result != '0' else None


def number(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 12:
        value = int(value)
    return value if type(value) is int and value >= 0 else None


def object_value(value):
    return value if isinstance(value, dict) else {}


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise SkillError('state_error', '技能数据目录须由运行账号所有，权限设为 0700。')
    return path


def save_private(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_json(path, content):
    save_private(path, (json.dumps(content, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def load_state(path):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'r', encoding='utf-8') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SkillError('state_error', '登录状态文件须由运行账号所有，权限设为 0600。')
            result = json.load(file)
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise SkillError('state_error', '本地登录文件无法读取，可以重新扫码登录。') from exc
    if not isinstance(result, dict) or result.get('schema_version') != SCHEMA_VERSION:
        raise SkillError('state_error', '本地登录文件格式不受支持，可以重新扫码登录。')
    return result


@contextmanager
def state_lock(directory):
    with os.fdopen(os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'wb') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SkillError('busy', '另一个酷狗歌单命令正在运行，请稍后重试。') from exc
        yield


def configuration():
    supplied = os.environ.get('KUGOU_API_DIR')
    path = Path(supplied).expanduser() if supplied else SCRIPTS.parent / '.cache' / 'KuGouMusicApi'
    path = path.resolve()
    if not (path / 'server.js').is_file():
        raise SkillError('configuration', '未找到 KuGouMusicApi 的 server.js，请把依赖装到技能目录的 .cache/KuGouMusicApi，或设置 KUGOU_API_DIR。')
    node = shutil.which('node')
    if not node:
        raise SkillError('configuration', '技能运行环境需要安装 Node.js。')
    return path, node


@contextmanager
def local_api(cookies):
    directory, node = configuration()
    with subprocess.Popen(
        [node, str(SCRIPTS / 'start_api.cjs'), str(directory)], cwd=directory,
        env={**os.environ, 'platform': platform()},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(10):
                    raise SkillError('startup_failed', 'KuGouMusicApi 启动超时。')
                ready = os.read(process.stdout.fileno(), 4096)
            try:
                port = number(object_value(json.loads(ready)).get('port'))
            except ValueError:
                port = None
            if not port or port > 65535 or process.poll() is not None:
                raise SkillError('startup_failed', 'KuGouMusicApi 未能启动，请检查版本和运行依赖。')
            yield Api(port, cookies)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def retry_delay(header, attempt):
    if header:
        seconds = number(header)
        if seconds is None:
            try:
                seconds = max(0, parsedate_to_datetime(header).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                seconds = None
        if seconds is not None:
            # A long server delay is best retried by a later skill invocation.
            return seconds if seconds <= 10 else None
    return 2 ** attempt


class Api:
    def __init__(self, port, cookies):
        self.port = port
        self.cookies = {key: value for key, value in cookies.items()
                        if key in SESSION_COOKIES and isinstance(value, str)}

    def request(self, route, *, attempts=3, **parameters):
        for attempt in range(attempts):
            try:
                return self.request_once(route, **parameters)
            except SkillError as exc:
                if exc.code not in {'network_error', 'rate_limited', 'service_unavailable'} or attempt + 1 == attempts:
                    raise
                delay = retry_delay(exc.detail.get('retry_after'), attempt)
                if delay is None:
                    raise
            time.sleep(delay)

    def request_once(self, route, **parameters):
        parameters['timestamp'] = time.time_ns() // 1_000_000
        # Upstream decodes each Cookie value on receipt. Keep secrets out of argv and URLs.
        headers = {'Cookie': '; '.join(f'{key}={quote(value, safe="")}' for key, value in self.cookies.items())}
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=8)
        try:
            connection.request('GET', route + '?' + urlencode(parameters), headers=headers)
            with connection.getresponse() as response:
                status = response.status
                if status == 429:
                    raise SkillError('rate_limited', '酷狗接口限流，请稍后重试。', retry_after=response.getheader('Retry-After'))
                if status == 401:
                    raise auth_required()
                if 300 <= status < 400:
                    raise SkillError('upstream_error', 'KuGouMusicApi 返回了意外的重定向。', http_status=status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
                cookie_headers = response.headers.get_all('Set-Cookie', [])
        except (OSError, http.client.HTTPException) as exc:
            raise SkillError('network_error', '无法完成酷狗接口请求，请检查网络后重试。') from exc
        finally:
            connection.close()

        if len(body) > MAX_RESPONSE_BYTES:
            raise SkillError('invalid_response', '酷狗接口响应超过大小上限。')
        try:
            result = json.loads(body)
        except ValueError:
            result = None
        business_code = number(object_value(result).get('error_code'))
        if business_code is None:
            business_code = number(object_value(result).get('errcode'))
        if business_code == 401:
            raise auth_required()
        if business_code == 429:
            raise SkillError('rate_limited', '酷狗接口限流，请稍后重试。')
        if status >= 500 and not business_code:
            raise SkillError('service_unavailable', '酷狗接口暂时不可用。', http_status=status)
        if status != 200 or business_code or object_value(result).get('status') in (0, '0'):
            raise SkillError('upstream_error', '酷狗接口返回业务错误。', http_status=status, upstream_code=business_code)
        if not isinstance(result, dict):
            raise SkillError('invalid_response', '酷狗接口未返回有效的 JSON 对象。')

        for header in cookie_headers:
            key, _, value = header.partition(';')[0].partition('=')
            if key.strip() in SESSION_COOKIES and value not in ('undefined', 'null'):
                self.cookies[key.strip()] = value.strip()
        return result


def auth_required():
    return SkillError('auth_required', '需要酷狗登录授权，请用手机酷狗扫码。')


def platform():
    return os.environ.get('platform', '')


def compatible_state(state):
    if state is None or state.get('platform') != platform():
        raise auth_required()
    return object_value(state.get('cookies'))


def authenticated(cookies):
    return bool(text(cookies.get('token')) and number(cookies.get('userid')))


def clear_pending(directory, request_id=None):
    for path in directory.iterdir():
        if path.suffix in ('.json', '.png') and REQUEST_ID.fullmatch(path.stem):
            if request_id is None or request_id == path.stem:
                path.unlink(missing_ok=True)


def qr_image(payload, key):
    try:
        url = urlsplit(payload.get('url', ''))
        header, image = payload.get('base64', '').split(',', 1)
        if (url.scheme, url.netloc, url.path) != ('https', 'h5.kugou.com', '/apps/loginQRCode/html/index.html'):
            raise ValueError
        if parse_qs(url.query).get('qrcode') != [key] or header != 'data:image/png;base64' or len(image) > 500_000:
            raise ValueError
        content = base64.b64decode(image, validate=True)
        if not content.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError
        return content
    except (ValueError, TypeError, AttributeError, binascii.Error) as exc:
        raise SkillError('invalid_response', '酷狗接口未返回有效的官方扫码二维码。') from exc


def login_start(pending):
    with local_api({}) as api:
        response = api.request('/login/qr/key', attempts=1)
        key = text(object_value(response.get('data')).get('qrcode'))
        if not key or len(key) > 256:
            raise SkillError('invalid_response', '酷狗接口未返回有效的二维码标识。')
        payload = object_value(api.request('/login/qr/create', attempts=1, key=key, qrimg=1).get('data'))
        image = qr_image(payload, key)
        cookies = {name: value for name, value in api.cookies.items() if name in DEVICE_COOKIES}
    request_id = uuid4().hex
    qr_path = pending / f'{request_id}.png'
    request_path = pending / f'{request_id}.json'
    clear_pending(pending)
    save_private(qr_path, image)
    try:
        save_json(request_path, {
            'schema_version': SCHEMA_VERSION, 'platform': platform(), 'created_at': int(time.time()),
            'key': key, 'cookies': cookies,
        })
    except BaseException:
        qr_path.unlink(missing_ok=True)
        raise
    return {'status': 'scan_required', 'request_id': request_id, 'qr_path': str(qr_path)}


def login_finish(pending, session_path, request_id):
    if not REQUEST_ID.fullmatch(request_id):
        raise SkillError('invalid_request', '请使用 login-start 返回的扫码请求 ID。')
    request_path = pending / f'{request_id}.json'
    state = load_state(request_path)
    created_at = number(state.get('created_at')) if state is not None else None
    if created_at is None or time.time() - created_at >= QR_LIFETIME or state.get('platform') != platform():
        clear_pending(pending, request_id)
        raise SkillError('qr_expired', '此扫码请求已失效，请重新生成二维码。')
    cookies = compatible_state(state)

    with local_api(cookies) as api:
        if not authenticated(cookies):
            key = text(state.get('key'))
            if not key:
                raise SkillError('state_error', '扫码状态文件缺少二维码信息，请重新扫码。')
            scan = wait_for_scan(api, key)
            status = number(scan.get('status'))
            if status == 0:
                clear_pending(pending, request_id)
                raise SkillError('qr_expired', '酷狗二维码已过期，请重新生成。')
            if status in (1, 2):
                return {'status': 'scan_pending', 'request_id': request_id, 'waiting_for': 'scan' if status == 1 else 'confirmation'}
            token, userid = text(scan.get('token')), identifier(scan.get('userid'))
            if status != 4 or not token or not userid or not number(userid):
                raise SkillError('invalid_response', '酷狗扫码响应未返回有效的登录会话。')
            api.cookies.update(token=token, userid=userid)
            # The QR may be consumed after the first successful check. Keep a short-lived receipt
            # privately until playlist verification succeeds, including after a transient failure.
            save_json(request_path, {
                'schema_version': SCHEMA_VERSION, 'platform': platform(), 'created_at': created_at,
                'cookies': api.cookies,
            })
            (pending / f'{request_id}.png').unlink(missing_ok=True)

        try:
            page_data(api.request(PLAYLIST_ROUTE, page=1, pagesize=30))
        except SkillError as exc:
            if exc.code == 'auth_required':
                clear_pending(pending, request_id)
            raise
        cookies = api.cookies
    save_json(session_path, {
        'schema_version': SCHEMA_VERSION, 'platform': platform(), 'saved_at': int(time.time()),
        'cookies': cookies,
    })
    clear_pending(pending, request_id)
    return {'status': 'authenticated'}


def wait_for_scan(api, key):
    for attempt in range(4):
        scan = object_value(api.request('/login/qr/check', attempts=1, key=key).get('data'))
        if number(scan.get('status')) not in (1, 2) or attempt == 3:
            return scan
        time.sleep(2)


def page_data(body, *fields):
    data = object_value(body.get('data'))
    for field in fields or ('info',):
        if isinstance(data.get(field), list):
            return data, data[field]
    raise SkillError('invalid_response', '酷狗接口未返回可识别的列表。')


def reported_count(data):
    for key in ('count', 'total', 'total_count'):
        value = number(data.get(key))
        if value is not None:
            return value
    return None


def playlist_entry(row):
    row = object_value(row)
    list_id = identifier(row.get('listid')) or identifier(row.get('list_create_listid'))
    global_id = identifier(row.get('global_collection_id'))
    name = text(row.get('name'))
    if not name or not (list_id or global_id):
        return None
    return {'id': list_id or global_id, 'name': name, 'count': reported_count(row),
            'list_id': list_id, 'global_collection_id': global_id}


def playlists(api):
    result, seen = [], set()
    total = None
    page_size = 30
    for page in range(1, MAX_PAGES + 1):
        data, rows = page_data(api.request(PLAYLIST_ROUTE, page=page, pagesize=page_size))
        current_total = reported_count(data)
        if total is None:
            total = current_total
        if current_total not in (None, total) or number(data.get('page')) not in (None, page):
            break
        for row in rows:
            playlist = playlist_entry(row)
            if not playlist:
                raise SkillError('invalid_response', '酷狗歌单缺少可用名称或 ID。')
            if playlist['id'] in seen:
                raise SkillError('catalog_incomplete', '歌单列表出现重复页或 ID，请重试。')
            result.append(playlist)
            seen.add(playlist['id'])
        if len(result) == total or (total is None and len(rows) < page_size):
            return result
        if not rows or (total is not None and len(result) > total):
            break
    raise SkillError('catalog_incomplete', '未能完整读取酷狗歌单列表，请重试。')


def select_playlist(catalog, name, playlist_id):
    if name is not None:
        matches = [row for row in catalog if row['name'] == name.strip()]
    else:
        matches = [row for row in catalog if playlist_id.strip() in (row['id'], row['global_collection_id'])]
    if not matches:
        raise SkillError('playlist_not_found', '账号歌单里未找到指定歌单。')
    if len(matches) != 1:
        raise SkillError('ambiguous_playlist', '找到多张同名歌单，请选择歌单 ID。', playlists=matches)
    return matches[0]


def first_text(record, *fields):
    return next((value for field in fields if (value := text(record.get(field)))), None)


def track_entry(row, position):
    row = object_value(row)
    audio = object_value(row.get('audio_info'))
    album = object_value(row.get('album_info')) or object_value(row.get('albuminfo'))
    filename = first_text(row, 'filename', 'file_name', 'audio_name', 'name', 'ori_audio_name') or first_text(audio, 'filename', 'name')
    title = first_text(row, 'songname', 'song_name', 'title') or first_text(audio, 'songname')
    artist = first_text(row, 'singername', 'singer_name', 'artist_name', 'author_name', 'singer') or first_text(audio, 'author_name', 'singername')
    combined = AUDIO_EXTENSION.sub('', filename or '')
    parsed_artist, separator, parsed_title = combined.partition(' - ')
    if separator and text(parsed_artist) and text(parsed_title):
        if not title or AUDIO_EXTENSION.sub('', title) == combined:
            title = parsed_title.strip()
        artist = artist or parsed_artist.strip()
    id_type = next((key for key in ('album_audio_id', 'mixsongid', 'audio_id', 'song_id', 'songid') if identifier(row.get(key))), None)
    return {
        'position': position, 'track_id': identifier(row.get(id_type)), 'track_id_type': id_type,
        'audio_hash': first_text(row, 'hash', 'audio_hash') or first_text(audio, 'hash_128', 'hash'),
        'title': title, 'artist': artist, 'album': first_text(row, 'album_name', 'albumname') or text(album.get('album_name')),
        'version': text(row.get('version')), 'source_filename': filename,
    }


def read_track_pages(api, route, id_key, playlist_id, count):
    rows, issues, fingerprints = [], [], set()
    version, total = None, None
    is_cloud_list = route == TRACK_ROUTE
    fields = ('info',) if is_cloud_list else ('songs', 'info')
    identity_key = 'listid' if is_cloud_list else 'global_collection_id'
    for page in range(1, MAX_PAGES + 1):
        try:
            body = api.request(route, page=page, pagesize=100, **{id_key: playlist_id})
            data, items = page_data(body, *fields)
        except SkillError as exc:
            if exc.code == 'auth_required':
                raise
            issues.append({'code': exc.code, 'page': page, **exc.detail})
            break
        actual_page = number(data.get('page'))
        source_version = identifier(data.get('list_ver'))
        if identifier(data.get(identity_key)) not in (None, playlist_id):
            issues.append({'code': 'playlist_id_mismatch', 'page': page})
            break
        if actual_page not in (None, page):
            issues.append({'code': 'page_mismatch', 'page': page, 'reported_page': actual_page})
            break

        current_total = reported_count(data)
        if (version is not None and source_version not in (None, version)) or (total is not None and current_total not in (None, total)):
            issues.append({'code': 'playlist_changed', 'page': page})
            break
        version = version or source_version
        if total is None:
            total = current_total
        expected = count if count is not None else total
        if not items:
            if len(rows) != expected:
                issues.append({'code': 'early_empty_page', 'page': page})
            break
        fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=True).encode()).digest()
        # Matching source rows on distinct confirmed pages can be legitimate repeats.
        page_marker = actual_page if actual_page is not None else identifier(data.get('cursor'))
        signature = (page_marker, fingerprint)
        if signature in fingerprints:
            issues.append({'code': 'repeated_page', 'page': page})
            break
        fingerprints.add(signature)
        rows.extend(items)
        if expected is not None and len(rows) >= max(expected, total if total is not None else expected):
            break
    else:
        issues.append({'code': 'page_limit'})

    expected = count if count is not None else total
    if count is not None and total is not None and count != total:
        issues.append({'code': 'count_disagreement', 'catalog_count': count, 'track_count': total})
    if expected is None:
        issues.append({'code': 'unknown_count'})
    elif expected != len(rows):
        issues.append({'code': 'count_mismatch', 'expected_count': expected, 'fetched_count': len(rows)})
    return {'rows': rows, 'expected_count': expected, 'list_version': version, 'issues': issues}


def read_tracks(api, playlist):
    routes = []
    if playlist['list_id']:
        routes.append((TRACK_ROUTE, 'listid', playlist['list_id']))
    if playlist['global_collection_id']:
        routes.append((FALLBACK_ROUTE, 'id', playlist['global_collection_id']))

    attempts = []
    for route in routes:
        attempt = read_track_pages(api, *route, playlist['count'])
        if any(issue['code'] == 'playlist_changed' for issue in attempt['issues']):
            attempt = read_track_pages(api, *route, playlist['count'])
        attempts.append(attempt)
        if not attempt['issues']:
            return attempt
    best = max(attempts, key=lambda attempt: len(attempt['rows']))
    if not best['rows']:
        raise SkillError('tracks_unavailable', '未能读取指定歌单的曲目。', issues=best['issues'])
    return best


def export_tracks(directory, playlist, result, *, for_download=False):
    rows = result['rows']
    summary = {'read_status': 'partial' if result['issues'] else 'complete',
               'expected_count': result['expected_count'], 'fetched_count': len(rows), 'issues': result['issues']}
    name = {'id': playlist['id'], 'name': playlist['name'], 'list_version': result['list_version']}
    response = {'status': 'ok', 'playlist': name, 'manifest_path': None, **summary}
    if for_download and not rows:
        return response
    path = private_directory(directory / ('kugou-handoff' if for_download else 'manifests')) / f'{uuid4().hex}.json'
    save_json(path, {
        'schema_version': MANIFEST_SCHEMA_VERSION, 'source': 'kugou', 'playlist': name, **summary,
        'tracks': [track_entry(row, position) for position, row in enumerate(rows, 1)],
    })
    response['manifest_path'] = str(path)
    return response


def run(args, directory):
    session_path = directory / 'session.json'
    with state_lock(directory):
        if args.command in ('login-start', 'login-finish', 'logout'):
            pending = private_directory(directory / 'pending')
            if args.command == 'login-start':
                return login_start(pending)
            if args.command == 'login-finish':
                return login_finish(pending, session_path, args.request)
            session_path.unlink(missing_ok=True)
            clear_pending(pending)
            return {'status': 'logged_out'}

        cookies = compatible_state(load_state(session_path))
        if not authenticated(cookies):
            raise auth_required()
        with local_api(cookies) as api:
            catalog = playlists(api)
            if args.command == 'playlists':
                return {'status': 'ok', 'playlists': catalog}
            playlist = select_playlist(catalog, args.playlist_name, args.playlist_id)
            result = read_tracks(api, playlist)
        return export_tracks(directory, playlist, result, for_download=args.for_download)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('login-start', help='生成手机酷狗登录二维码')
    commands.add_parser('login-finish', help='确认扫码并保存会话').add_argument('--request', required=True)
    commands.add_parser('playlists', help='列出当前账号的歌单')
    tracks = commands.add_parser('tracks', help='将指定歌单的曲目导出为 JSON')
    selector = tracks.add_mutually_exclusive_group(required=True)
    selector.add_argument('--playlist-name')
    selector.add_argument('--playlist-id')
    tracks.add_argument('--for-download', action='store_true', help='生成建立下载批次后可清理的临时清单')
    commands.add_parser('logout', help='清除本机酷狗会话')
    return parser.parse_args()


def interrupt(_signal, _frame):
    raise KeyboardInterrupt


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    directory = Path(os.environ.get('KUGOU_STATE_DIR') or SCRIPTS.parent / 'data').expanduser().absolute()
    exit_code = 0
    try:
        result = run(args, private_directory(directory))
    except SkillError as exc:
        result = {'status': 'error', 'error_code': exc.code, 'message': str(exc), **exc.detail}
        exit_code = 1
    except OSError:
        result = {'status': 'error', 'error_code': 'local_error', 'message': '无法读写技能数据或启动本地进程，请检查运行权限。'}
        exit_code = 1
    except KeyboardInterrupt:
        result = {'status': 'error', 'error_code': 'interrupted', 'message': '酷狗歌单任务已中断。'}
        exit_code = 130
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
