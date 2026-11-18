# 执行参考

所有路径和命令都以运行 OpenClaw 的机器或容器为准。从技能目录运行 `python3 scripts/music_download.py`。使用命令行时按实际值传参，优先使用支持参数数组的执行工具。如需使用 shell，应对每个参数单独转义，尤其是带 `&` 的链接、歌名和目录。

## 运行配置

- 搜索需要 Python 3.9+ 及访问 `https://so.liumingye.cn` 的网络。脚本使用 Python 标准库。
- 转存和下载使用 Node.js 运行已配置的夸克网盘 Skill。`MUSIC_QUARK_CLI` 填 `quark-drive.cjs` 的绝对路径。夸克 CLI 1.0.11 和 1.0.25 会先检查当前运行环境。在 OpenCode 中直接运行时，实测返回 `-104`。请通过夸克可识别的实际运行环境调用，例如 OpenClaw 或 Codex。可先用 `resolve-agent` 检查环境，再通过夸克官方授权流程登录。脚本复用 CLI 的登录状态。
- `MUSIC_LIBRARY_ROOT` 指定已存在的音乐库根目录，例如 `/music`。`MUSIC_LIBRARY_DEFAULT_DIR` 可指定默认目录，例如 `/music/华语`。本次使用 `--directory` 可覆盖默认值。相对路径以音乐库根目录为基准，目标可以是根目录或其子目录。容器内应配置已挂载、可写且支持硬链接的目录。
- `MUSIC_LIBRARY_DB` 可指定 SQLite 文件路径。默认在技能目录下运行时生成 `data/library.sqlite3`。如果技能目录只读或者升级时会替换整个技能，建议指向 OpenClaw 可写的持久化目录，例如 `/var/lib/openclaw/music/library.sqlite3`。SQLite 使用 Python 标准库。
- 入库还需要 `ffprobe`。下载使用系统临时目录，可通过 `TMPDIR` 调整位置。夸克网盘每次在默认位置创建一个 `music-download-…` 暂存目录；也可预先通过 `MUSIC_QUARK_STAGING_PARENT_FID` 指定它的父目录 ID。网盘暂存副本留在夸克网盘中，本地临时音频由脚本清理。
- 脚本复用已准备好的 CLI，通过命令参数传入所需链接、文件 ID 和路径。夸克 CLI 的 `--session-input` 是可选的质量追踪参数，脚本省略原始对话正文。
- 歌词需要 `ffprobe` 及通往 `lrclib.net`、`music.163.com`、`c.y.qq.com`、`lyrics.kugou.com` 的网络。已实测四个来源均可匿名获取赵雷《少女》的时间轴歌词，无需 Cookie、账号登录或 VIP。QQ 请求带网页来源请求头。酷狗的 `accesskey` 来自歌词搜索响应，与账号授权无关。平台接口可能限流、调整或只为部分歌曲返回歌词。

## 调用示例

### 查询和刷新索引

```bash
python3 scripts/music_download.py lookup --title '成都' --artist '赵雷'
python3 scripts/music_download.py reindex
```

`lookup` 只需要可读的 `MUSIC_LIBRARY_ROOT` 和可写的 SQLite 存储。传基础歌名，省略 `--artist` 时会返回所有同名歌曲。查询按全角半角、大小写和空白归一化后的词精确匹配。`tracks` 包含 `artist`、`title`、`version`、`album`、`format`、`path` 和 `size`，其中未知的版本和专辑为 `null`。返回所有命中的音频格式和版本，供聊天流程判断是否符合要求。

每个音乐库首次查询时建立索引，之后只查询数据库并验证命中文件仍存在且大小和修改时间一致。`reindex` 扫描所有子目录并返回 `indexed` 文件数。首次扫描及手动刷新识别 `歌手 - 歌名.格式`，支持从常见的括号或短横线后缀识别版本，例如 `赵雷 - 成都 (Live).flac`。扫描跳过隐藏文件、隐藏目录和符号链接。手动加入、移动或修改的文件在下次刷新后可查到，命名不规则的文件需要先调整文件名。刷新会保留未变化的技能入库文件已有的专辑和版本信息。

### 下载

下载请求先用 `lookup` 查重，实际需要下载时用 `destination` 检查目录。用户指定目录时，`destination` 和 `download` 都传同一个 `--directory` 参数。省略参数时使用默认目录。目录检查只解析本机路径，无需夸克授权。通过时返回 `path` 和 `default_configured`。需要配置时，`required_setting` 指出应设置的环境变量，配置生效后可继续当前请求。

```bash
python3 scripts/music_download.py destination
python3 scripts/music_download.py destination --directory '/music/华语'
```

搜索支持 `--page`，响应的 `next_page` 有值时可继续查询。只有夸克标签的条目会列入候选。详情结果中的 `downloads` 包含 `url` 和 `format`，格式未知时 `format` 为 `null`。

```bash
python3 scripts/music_download.py search --title '成都' --artist '赵雷'
python3 scripts/music_download.py detail --id '选中的歌曲ID'
python3 scripts/music_download.py share --url 'https://pan.quark.cn/s/example'
```

`share` 输出可用的音频和目录。下一页用 `--page`，子目录用 `--folder-fid`。链接有提取码时保留 URL 中的 `pwd`，或传 `--passcode`。`download` 沿用选中文件所在的分享链接、`--page`、`--folder-fid` 和提取码，复查选中的 `--file-fid`。可传 `--version '现场版'`、`--album '专辑名'` 将确认过的信息写入索引。歌名已经带有版本后缀时沿用全名。如果歌名没有后缀且传了 `--version`，脚本会在文件名末尾附上版本。

`download` 默认使用 FLAC。其他格式须由用户指定或确认，例如 `--format mp3`。

```bash
python3 scripts/music_download.py download \
  --url 'https://pan.quark.cn/s/example' --file-fid '所选文件ID' \
  --artist '赵雷' --title '成都' --directory '/music/华语'
```

已确认转存成功但后续步骤失败时，保留原分享和文件参数，并传 `--staging-fid` 指定结果中 `quark_staging.fid` 对应的目录。脚本直接定位该目录中的文件，避免重复转存。夸克 CLI 1.0.25 可用 `browse` 按目录读取结果，旧版使用搜索结果核对父目录、文件名和字节数。

脚本返回一行 JSON。入库成功时返回 `status`、`path`、`format` 和 `size`。同名且内容相同返回 `exists`，同名冲突返回 `error`。`saved` 和 `exists` 都会写入索引。若 NAS 文件已就绪但索引写入失败，结果保留成功状态并附上 `index_warning`。错误的 `stage` 标识失败环节。若已创建夸克暂存目录，响应附带 `quark_staging`。执行工具的超时应覆盖转存和下载，长任务可用后台模式等待结果。遇到超时或转存后尚未搜到文件时，先根据 `quark_staging` 检查远端状态，再决定是否重试。

### 查找并保存 LRC

`download` 在发布音频和更新索引后尝试匹配歌词，结果写入 `lyrics` 字段。对已有文件可直接调用 `lyrics`，接收音乐库内的绝对路径或相对于 `MUSIC_LIBRARY_ROOT` 的路径。这一步可以脱离夸克 CLI 和 SQLite 单独运行。

```bash
python3 scripts/music_download.py lyrics --path '/music/华语/赵雷 - 少女.flac'
python3 scripts/music_download.py lyrics --path '/music/华语/赵雷 - 少女.flac' \
  --source kugou --id '用户确认的候选ID'
```

脚本优先使用音频标签，标签缺失时使用 `--title`、`--artist`、`--album`、`--version` 和 `歌手 - 歌名` 文件名。匹配时核对歌名、歌手、已知专辑和版本，保留时长差不超过 3 秒的候选。自动保存优先选同专辑、时长差在 1 秒内的带时间轴候选。专辑一致时可接受最多 3 秒的差值。专辑未知时自动保存要求时长差不超过 1 秒。显式指定的版本如果无法根据候选核实，需要人工确认。

默认按 `lrclib`、`netease`、`qq`、`kugou` 的顺序查找。`--source` 可限定一家，选定 ID 时需要同时传 `--source`。同一可信等级有多个不同时间轴时返回 `needs_review`，可用其中的 `source` 和 `id` 续接。只返回纯文本、缺少有效时间戳或时间轴超出音频时长的歌词会跳过。

`saved` 返回 `.lrc` 路径、来源、ID、候选元数据和有效时间戳条数。歌词使用 UTF-8，通过同目录暂存后发布。已有同名 `.lrc` 时返回 `exists`。`not_found` 表示没有匹配的时间轴，`unavailable` 表示有来源暂时未能查询，`error` 表示本地读取或写入失败。以上歌词状态独立于音频的下载、校验和入库结果。
