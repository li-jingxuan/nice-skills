# 执行参考

所有路径和命令都以运行 OpenClaw 的机器或容器为准。从技能目录运行 `python3 scripts/music_download.py`。使用命令行时按实际值传参，优先使用支持参数数组的执行工具。如需使用 shell，应对每个参数单独转义，尤其是带 `&` 的链接、歌名和目录。

## 运行配置

- 搜索需要 Python 3.9+ 及访问 `https://so.liumingye.cn` 的网络。脚本使用 Python 标准库。
- `MUSIC_SOURCE_REQUESTS_PER_MINUTE` 控制下歌吧搜索和详情的总请求速率，默认 10，须为正整数。默认每两次请求至少间隔 6 秒，重试也计入限额。使用同一缓存目录的单曲命令和批量进程共享请求间隔及冷却时间，请给这些进程使用同一速率配置。限流状态保存在 `$XDG_CACHE_HOME/nice-skills/music-download/source-rate.sqlite3`，未指定 `XDG_CACHE_HOME` 时用 `~/.cache`。
- 转存和下载使用 Node.js 运行已配置的夸克网盘 Skill。`MUSIC_QUARK_CLI` 填 `quark-drive.cjs` 的绝对路径。夸克 CLI 1.0.11 和 1.0.25 会先检查当前运行环境。在 OpenCode 中直接运行时，实测返回 `-104`。请通过夸克可识别的实际运行环境调用，例如 OpenClaw 或 Codex。可先用 `resolve-agent` 检查环境，再通过夸克官方授权流程登录。脚本复用 CLI 的登录状态。
- 百度分享使用官方 `bdpan`，从 `PATH` 查找，可以用 `MUSIC_BAIDU_CLI` 指定可执行文件。安装官方 `baidu-drive` Skill 后，按其登录流程给当前运行账号授权。CLI 需支持 `transfer list`、`transfer select` 和 `rm`，官方 Skill 1.7.7 对应 `bdpan` 3.8.9。每个百度任务使用 `/apps/bdpan/music-download-…` 下的独立暂存目录。 [官方安装与授权文档](https://github.com/baidu-netdisk/bdpan-storage)
- `MUSIC_LIBRARY_ROOT` 指定已存在的音乐库根目录，例如 `/music`。`MUSIC_LIBRARY_DEFAULT_DIR` 可指定默认目录，例如 `/music/华语`。本次使用 `--directory` 可覆盖默认值。相对路径以音乐库根目录为基准，目标可以是根目录或其子目录。容器内应配置已挂载、可写且支持硬链接的目录。
- `MUSIC_LIBRARY_DB` 可指定 SQLite 文件路径。默认在技能目录下运行时生成 `data/library.sqlite3`。如果技能目录只读或者升级时会替换整个技能，建议指向 OpenClaw 可写的持久化目录，例如 `/var/lib/openclaw/music/library.sqlite3`。SQLite 使用 Python 标准库。
- 入库还需要 `ffprobe`。下载使用系统临时目录，可通过 `TMPDIR` 调整位置。夸克网盘每次在默认位置创建一个 `music-download-…` 暂存目录。也可预先通过 `MUSIC_QUARK_STAGING_PARENT_FID` 指定它的父目录 ID。脚本清理本地临时音频。
- 脚本复用已准备好的 CLI，通过命令参数传入所需链接、文件 ID 和路径。夸克 CLI 的 `--session-input` 是可选的质量追踪参数，脚本省略原始对话正文。
- 歌词需要 `ffprobe` 及通往 `lrclib.net`、`music.163.com`、`c.y.qq.com`、`lyrics.kugou.com` 的网络。已实测四个来源均可匿名获取赵雷《少女》的时间轴歌词，无需 Cookie、账号登录或 VIP。QQ 请求带网页来源请求头。酷狗的 `accesskey` 来自歌词搜索响应，与账号授权无关。平台接口可能限流、调整或只为部分歌曲返回歌词。

## 调用示例

### 查询和刷新索引

```bash
python3 scripts/music_download.py lookup --title '成都' --artist '赵雷'
python3 scripts/music_download.py reindex
python3 scripts/music_download.py scan --directory '/music/华语'
```

`lookup` 只需要可读的 `MUSIC_LIBRARY_ROOT` 和可写的 SQLite 存储。传基础歌名，省略 `--artist` 时会返回所有同名歌曲。查询按全角半角、大小写和空白归一化后的词精确匹配。`tracks` 包含 `artist`、`title`、`version`、`album`、`format`、`path` 和 `size`，其中未知的版本和专辑为 `null`。返回所有命中的音频格式和版本，供聊天流程判断是否符合要求。

每个音乐库首次查询时建立索引，之后只查询数据库并验证命中文件仍存在且大小和修改时间一致。`reindex` 扫描所有子目录并返回 `indexed` 文件数。全库扫描可识别新加入的 `歌手 - 歌名.格式`，例如 `赵雷 - 成都 (Live).flac`，也会保留未变化文件已有的元数据。全库刷新可清理已失效的索引记录。扫描跳过隐藏文件、隐藏子目录和符号链接。

`scan` 递归导入指定目录。可用绝对路径或相对音乐库根目录的路径，例如 `--directory '华语'`。扫描新增或改动的文件时，优先用 `ffprobe` 读取歌名、歌手、专辑和版本。缺少 `ffprobe` 或标签时，用 `歌手 - 歌名` 或文件名补充可识别的字段。歌曲可通过同一个 SQLite 索引的 `lookup` 查询。

同一音乐库内的同一绝对路径只对应一条索引记录。扫描重复路径时跳过未变化的文件，大小或修改时间变化时更新对应记录。不同路径的音频各自保留。命令返回 `directory`、`scanned`、`inserted`、`updated`、`unchanged` 和 `skipped`。`skipped` 记录扫描到但在提交前消失或变化的文件。指定目录扫描仅新增和更新记录，索引中的失效路径可用 `reindex` 清理。

### 下载

下载请求先用 `lookup` 查重，实际需要下载时用 `destination` 检查目录。用户指定目录时，`destination` 和 `download` 都传同一个 `--directory` 参数。省略参数时使用默认目录。目录检查只解析本机路径，无需夸克授权。通过时返回 `path` 和 `default_configured`。需要配置时，`required_setting` 指出应设置的环境变量，配置生效后可继续当前请求。

```bash
python3 scripts/music_download.py destination
python3 scripts/music_download.py destination --directory '/music/华语'
```

搜索支持 `--page`，响应的 `next_page` 有值时可继续查询。可用 `--page-size 50` 每页返回最多 50 条候选。下歌吧单次请求最多返回 20 条，指定 `--page-size` 时脚本会按筛选后的候选合并多页，结果不足时返回剩余条目。继续翻页时使用相同关键词和页大小。夸克和百度网盘标签的条目都会列入候选。详情结果中的 `downloads` 包含 `url` 和 `format`，格式未知时 `format` 为 `null`。

下歌吧返回 HTTP 429 时，同一请求最多额外重试 5 次。基础等待时间依次为 5、10、20、35、55 秒，实际请求还要遵守共享请求间隔和服务端 `Retry-After`，因此默认首次重试距离上一请求至少 6 秒。若仍受限，单曲返回 `error_code: source_rate_limited` 和 Unix 时间戳 `retry_at`。批量停止领取新曲目，已在途曲目完成当前处理后暂停，受限的曲目保留在队列中，之后可用原批次 ID 继续。

```bash
python3 scripts/music_download.py search --title '成都' --artist '赵雷'
python3 scripts/music_download.py detail --id '选中的歌曲ID'
python3 scripts/music_download.py share --url 'https://pan.quark.cn/s/example'
```

`share` 输出可用的音频和目录。下一页用 `--page`。夸克子目录用 `--folder-fid`，百度子目录用 `--source-dir` 传目录条目的 `path`。链接有提取码时保留 URL 中的 `pwd`，或传 `--passcode`。`download` 沿用选中文件所在的链接、页码、子目录及提取码，复查 `--file-fid`。可传 `--version '现场版'`、`--album '专辑名'` 将确认过的信息写入索引。歌名已经带有版本后缀时沿用全名。如果歌名没有后缀且传了 `--version`，脚本会在文件名末尾附上版本。

单曲和批量的默认选源顺序是夸克 FLAC → 夸克 MP3 → 百度 FLAC → 百度 MP3。按实际分享链接和文件验证网盘与格式。匹配完整歌名和歌手署名即可自动选择。多个合格音源可直接按优先级处理。同一档优先歌名和歌手完全匹配的条目。合唱字段包含指定歌手时可匹配，明确指定的专辑和版本需符合要求。切换到 MP3 时，批量结果记录 `format_fallback`。`--flac-only` 只查两家的 FLAC。无法核实歌手或指定版本时，用 `search`、`detail` 和 `share` 整理候选。

```bash
python3 scripts/music_download.py resolve-source --title '成都' --artist '赵雷'
```

`resolve-source` 只查库和选源。`selected` 返回 `file`、`file_format` 和 `resolved`，可直接交给 `download`。也可能返回 `already_in_library`、`needs_review` 或 `no_source`。可通过 `--album`、`--version` 和 `--format` 按用户要求筛选，显式传 `--format flac` 时只选 FLAC。

`download` 默认使用 FLAC。可用 `--format mp3` 下载已确认的 MP3。同一夸克分享页有匹配的 MP3 时，可显式传 `--mp3-file-fid`，供指定的夸克 FLAC 超限时回退。

```bash
python3 scripts/music_download.py download \
  --url 'https://pan.quark.cn/s/example' --file-fid '所选文件ID' \
  --artist '赵雷' --title '成都' --directory '/music/华语'
```

已确认转存成功但后续步骤失败时，保留原分享和文件参数，并传 `--staging-fid` 指定结果中 `quark_staging.fid` 对应的目录。脚本直接定位该目录中的文件，避免重复转存。夸克 CLI 1.0.25 可用 `browse` 按目录读取结果，旧版使用搜索结果核对父目录、文件名和字节数。

百度分享会按文件 ID 转存单个音频，再在对应暂存目录核对文件名和字节数。下面示例中的文件 ID 和目录来自 `share` 的结果。

```bash
python3 scripts/music_download.py share \
  --url 'https://pan.baidu.com/s/example?pwd=abcd' --source-dir '/专辑目录' --page 2
python3 scripts/music_download.py download \
  --url 'https://pan.baidu.com/s/example?pwd=abcd' --source-dir '/专辑目录' --page 2 \
  --file-fid '9007199254740993' --artist '赵雷' --title '成都' --directory '/music/华语'
```

`waiting_transfer` 表示暂存目录还未出现可核对的文件。保留响应中的 `baidu_staging`，稍后将其中的 `path` 作为 `--staging-dir` 加到原命令，继续核对原目录。原分享需要可访问，以复查选中文件的信息。若单曲返回 `error_code: busy` 和 `baidu_staging.submitted: false`，说明本次明确未提交，约五分钟后可用原命令重试。批量续跑会自动沿用已保存的音频信息、暂存目录和任务 ID。

夸克返回 `download file size limit[52428800]` 这类单文件上限错误时，脚本给出 `error_code: download_size_limit` 和 `limit_bytes`，具体数值来自夸克响应。自动选源跳过超限的夸克文件，再按默认优先级核对剩余来源。显式传了同页的 `--mp3-file-fid` 时，单曲脚本核对 MP3 格式及大小后自动转存和下载，并用 `fallback` 记录格式变更、上限和原暂存目录。切换到其他分享页或链接时，使用对应文件自己的分享参数。

脚本返回一行 JSON。入库成功时返回 `status`、`path`、`format` 和 `size`。同名且内容相同返回 `exists`，同名冲突返回 `error`。`saved` 和 `exists` 都会写入索引。若 NAS 文件已就绪但索引写入失败，结果保留成功状态并附上 `index_warning`。错误的 `stage` 标识失败环节。若已创建夸克暂存目录，响应附带 `quark_staging`。执行工具的超时应覆盖转存和下载，长任务可用后台模式等待结果。遇到超时或转存后尚未搜到文件时，先根据 `quark_staging` 检查远端状态，再决定是否重试。

音频校验并发布到 NAS 后，百度会按路径、文件 ID 和字节数核对并删除本次转存的单个音频。暂存目录保留。未完成音频校验或发布时，暂存副本保留供续接。`staging_cleanup.status` 为 `removed` 表示已确认暂存音频不在百度暂存目录。为 `manual` 或 `warning` 时，返回暂存位置和原因，音频仍已入库。夸克官方 CLI 暂无删除命令，可用返回的位置在客户端将对应副本移入回收站。批量任务的逐曲 `result` 也会记录清理结果。

### 查找并保存 LRC

`download` 在发布音频和更新索引后尝试匹配歌词，结果写入 `lyrics` 字段。对已有文件可直接调用 `lyrics`，接收音乐库内的绝对路径或相对于 `MUSIC_LIBRARY_ROOT` 的路径。这一步可以脱离夸克 CLI 和 SQLite 单独运行。

```bash
python3 scripts/music_download.py lyrics --path '/music/华语/赵雷 - 少女.flac'
python3 scripts/music_download.py lyrics --path '/music/华语/赵雷 - 少女.flac' \
  --source kugou --id '用户确认的候选ID'
```

脚本优先使用音频标签，标签缺失时使用 `--title`、`--artist`、`--album`、`--version` 和 `歌手 - 歌名` 文件名。匹配时核对歌名、歌手、已知专辑和版本，保留时长差不超过 3 秒的候选。自动保存优先选同专辑、时长差在 1 秒内的带时间轴候选。专辑一致时可接受最多 3 秒的差值。专辑未知时自动保存要求时长差不超过 1 秒。显式指定的版本如果无法根据候选核实，需要人工确认。

默认按 `lrclib`、`netease`、`qq`、`kugou` 的顺序查找。`--source` 可限定一家，选定 ID 时需要同时传 `--source`。同一可信等级有多个不同时间轴时返回 `needs_review`，可用其中的 `source` 和 `id` 续接。只返回纯文本、缺少有效时间戳或时间轴超出音频时长的歌词会跳过。

脚本根据时间轴正文的中文特征字判断字形，通常跳过署名和版权行。遇到繁体或字形不明确的中文歌词时，继续找同等或更高可信度的简体 LRC，找不到时使用当前匹配结果。英文等其他语言沿用默认顺序。保存原文和原时间轴。响应中的 `language` 为 `zh-Hans`、`zh-Hant` 或 `zh`，后者表示简繁混合或缺少足够的特征字。

`saved` 返回 `.lrc` 路径、来源、ID、候选元数据和有效时间戳条数。歌词使用 UTF-8，通过同目录暂存后发布。已有同名 `.lrc` 时返回 `exists`。`not_found` 表示没有匹配的时间轴，`unavailable` 表示有来源暂时未能查询，`error` 表示本地读取或写入失败。以上歌词状态独立于音频的下载、校验和入库结果。

### 批量下载与续跑

`batch-add` 接收 UTF-8 文本、JSON 曲目数组和 `kugou-playlist` 导出的版本 1 JSON 清单。文本每行使用 `歌名` 或 `歌手 - 歌名`。手填 JSON 数组的每项可以是歌名字符串，或使用 `{"title":"成都","artist":"赵雷","album":"无法长大","version":null}`。`title` 必填，`artist`、`album`、`version` 可省略或填 `null`。目录和音质沿用单曲参数。`--format flac` 和 `--flac-only` 都将新批次限制为 FLAC。

将酷狗 `tracks --for-download` 返回的临时清单路径传给 `--consume-manifest`。批量入口检查 `schema_version: 1`、`source: "kugou"`、`read_status` 及曲目计数，按 `tracks` 原顺序读取匹配字段。`read_status: partial` 时先确认是否处理已读取曲目，再加 `--allow-partial`。缺少歌名时会报告位置，补齐后即可重新导入。

`--consume-manifest` 在 SQLite 提交后删除临时清单，导入失败时保留文件。对同一清单用原参数重试会返回已有批次，也可补做清理。若返回 `manifest_cleanup_warning`，批次已可运行。`--input` 适用于用户提供的文件和普通导出清单，导入后保留文件。

```bash
python3 scripts/music_download.py batch-add --input '/tmp/歌曲清单.txt' --directory '/music/华语'
python3 scripts/music_download.py batch-add --consume-manifest '<酷狗 --for-download 返回的 manifest_path>' --directory '/music/华语'
python3 scripts/music_download.py batch-run --job '<批次 ID>' --workers 10
python3 scripts/music_download.py batch-run --job '<批次 ID>' --continue-review
python3 scripts/music_download.py batch-status --job '<批次 ID>'
python3 scripts/music_download.py batch-status --job '<批次 ID>' --state failed
python3 scripts/music_download.py batch-status --job '<批次 ID>' --state no_source
python3 scripts/music_download.py batch-select --job '<批次 ID>' --item 3 --track-id '<歌曲 ID>'
```

状态保存在音乐库索引旁的 `batches.sqlite3`，可通过 `MUSIC_BATCH_DB` 指向运行主机上的持久化可写文件。一次只运行该数据库中的一个批次，可在运行期间查询状态。`--workers` 有效范围是 1 到 10，默认 10。普通单曲错误最多额外重试 5 次，逐次退避。明确限流时暂停派发新任务，按服务端 `Retry-After` 或退避时间等待，并调低当次运行的并发数。明确超限且无法回退时记录失败，无匹配音源时记录 `no_source`。授权、转存额度、存储或索引等共用故障令批次暂停，恢复后用原批次 ID 继续。

`batch-status` 返回统计和逐曲记录，`--state` 可筛选状态，`--offset`、`--limit` 可翻页。只给歌名时，批次先用网易云和 QQ 音乐的正式专辑条目交叉核对演唱者，再匹配下歌吧条目。无法核实时记为 `needs_review`，其他曲目会继续处理，批次结束时集中查看这些记录。更新匹配规则后，旧批次内已提供歌手且尚未选源的待确认曲目会在下次运行时重新匹配。待确认达到 10 首时批次提前暂停。用户确认继续后，恢复命令加 `--continue-review`，本次会处理剩余队列，待确认项仍保留。可按 `position` 用 `batch-select` 选择已经确认的下歌吧条目。选择具体文件时加 `--url`、`--file-fid`、`--page` 和对应子目录参数，必要时传 `--passcode`。选定后仍用原批次 ID 运行。

续跑沿用已选文件和网盘暂存位置。百度转存结果未知时记为 `waiting_transfer`，保留暂存目录及 CLI 已返回的 `task_id`，下次运行原批次时复查目录。服务端报告有其他任务正在转存时，五分钟后才重新领取该曲。等待状态允许其他曲目继续，也保留单曲的下载重试次数。下载中断时从可验证的阶段继续。已经完成校验并记录待发布哈希的文件可恢复发布或补写索引。续跑时会复查已完成的音频路径和大小，将缺失或大小变化的记录排回队列。`failed` 保留错误阶段和历次失败信息，`no_source` 保留未找到可用来源的原因。LRC 查询结果记在逐曲结果中。
