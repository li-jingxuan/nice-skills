# 执行参考

在运行 OpenClaw 的飞牛 NAS 或容器中准备 Python 3.9+、Node.js 和 KuGouMusicApi。技能目录内运行下文命令。涉及用户输入时，优先用参数数组执行，分别传入名称、ID 等值。

## 在飞牛上准备运行环境

将 KuGouMusicApi 固定到参考提交，并在部署阶段安装依赖。下载源码和安装依赖都只需做一次。Docker 部署时可构建进 OpenClaw 镜像，也可把准备好的依赖目录挂载到运行环境。按下面的例子调整路径，或让管理员在现有容器里设置等效配置。 [参考提交](https://github.com/MakcRe/KuGouMusicApi/tree/443d4a416dd344ccf7b022b9fb932e324afc77b4)

```bash
git clone https://github.com/MakcRe/KuGouMusicApi.git /opt/kugou-music-api
git -C /opt/kugou-music-api checkout --detach 443d4a416dd344ccf7b022b9fb932e324afc77b4
pnpm --dir /opt/kugou-music-api install --prod --frozen-lockfile
export KUGOU_API_DIR=/opt/kugou-music-api
```

- `KUGOU_API_DIR` 指向含 `server.js` 和运行依赖的目录。未设置时，技能使用自身目录下的 `.cache/KuGouMusicApi`。OpenClaw 容器需要能连接酷狗上游。每个读取命令在本机回环地址使用动态端口启动服务，退出时回收进程。
- `platform` 可选，默认使用常规平台。如需使用 `lite`，在 OpenClaw 运行环境中显式设置 `platform=lite`，更改平台后重新扫码。
- 登录会话默认写到技能目录的 `data/session.json`。请挂载可持久化的 `data/`。技能目录只读或升级时会整目录替换，可设置 `KUGOU_STATE_DIR=/var/lib/openclaw/kugou` 并挂载该目录。目录须归运行 OpenClaw 的账号所有并设为 `0700`。脚本以 `0600` 创建会话、扫码和清单文件。
- 技能打包时带上 `SKILL.md`、`scripts/` 和 `references/`，运行数据单独留在飞牛上。二维码只向当前账号授权人展示，token 在本地会话文件中保存。首次扫码或会话失效时重新授权。

## 在本项目的 OpenCode 使用

把项目的 `.opencode/skills/kugou-playlist` 链接到 `skills/kugou-playlist`。将同一固定版本的 KuGouMusicApi 和生产依赖安装在 `skills/kugou-playlist/.cache/KuGouMusicApi`，即可从 OpenCode 调用，无需额外配置环境变量。

## 调用示例

```bash
python3 scripts/kugou_playlist.py playlists
python3 scripts/kugou_playlist.py login-start
python3 scripts/kugou_playlist.py login-finish --request 'login-start 返回的 request_id'
python3 scripts/kugou_playlist.py tracks --playlist-name '我喜欢'
python3 scripts/kugou_playlist.py tracks --playlist-id '选中的歌单 ID'
python3 scripts/kugou_playlist.py logout
```

每条有效命令返回一行 JSON。`playlists` 返回 `playlists` 数组，其中 `id` 可继续传给 `tracks`。`login-start` 返回 `scan_required`、`request_id`、`qr_path`。扫码和手机确认后执行 `login-finish`，成功时返回 `authenticated`。发现 `scan_pending` 时保留请求 ID，用户操作后再次确认。新生成的码会替换之前待确认的码。`logout` 清除本机会话和扫码文件，已导出的清单可以继续使用。

`tracks` 返回 `manifest_path`、`read_status`、`expected_count`（未知时为 `null`）、`fetched_count` 和 `issues`。清单是带 `schema_version: 1` 的 UTF-8 JSON 文件，保留原顺序及重复项。曲目包含 `position`、`track_id`、`track_id_type`、`audio_hash`、`title`、`artist`、`album`、`version` 和 `source_filename`，未知字段为 `null`。`expected_count` 以歌单显示数为先。`fetched_count` 统计读取的原始条目，方便报告部分读取。`source_filename` 是酷狗返回的元数据。音质由下载流程决定。

执行失败时退出码为 `1`，JSON 包含 `status: error`、`error_code` 和 `message`。`auth_required` 可以进入扫码流程，`ambiguous_playlist` 附带候选列表。`upstream_error` 附带可用的 HTTP 状态与业务错误码。上游限流、网络故障、未知业务错误时会话文件保留。手动切换账号也可以运行 `login-start`。`read_status: partial` 属于已导出部分结果，退出码为 `0`，下载前须确认是否处理已读取的曲目。

## 交给 `music-download`

在 `tracks` 命令加 `--for-download`，把返回的 `manifest_path` 传给 `music-download batch-add --consume-manifest`。保存目录和音质用命令参数指定。部分读取时先告诉用户已读取数、预期数和 `issues`，确认处理后加 `--allow-partial`。空歌单返回 `manifest_path: null`。缺少歌名时，`batch-add` 会报告原位置，补齐后再导入。

临时清单写入技能数据目录的 `kugou-handoff/`。只有批次写入 SQLite 成功后，`--consume-manifest` 才删除清单。批次创建失败时可用原清单重试。批次建好后，下载和续跑使用批次 ID。普通 `tracks` 导出的 JSON 仍可用 `batch-add --input` 导入并保留文件。

版本 1 清单示例：

```json
{
  "schema_version": 1,
  "source": "kugou",
  "playlist": {"id": "123", "name": "我喜欢", "list_version": null},
  "read_status": "complete",
  "expected_count": 1,
  "fetched_count": 1,
  "issues": [],
  "tracks": [
    {
      "position": 1,
      "title": "成都",
      "artist": "赵雷",
      "album": "无法长大",
      "version": null,
      "track_id": "456",
      "track_id_type": "album_audio_id",
      "audio_hash": null,
      "source_filename": "赵雷 - 成都"
    }
  ]
}
```

`fetched_count` 等于 `tracks` 长度，包含重复项。`complete` 时 `expected_count` 与之相等。`partial` 时预期数可以未知，原因见 `issues`。下载队列按数组顺序用 `title`、`artist`、`album` 和 `version` 匹配歌曲，保留重复曲目的编号。酷狗 `track_id` 与 `audio_hash` 用于追溯源条目。下载流程中的 `batch-select --track-id` 接收下歌吧条目 ID。
