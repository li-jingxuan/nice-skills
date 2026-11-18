# `kugou-playlist` 技术方案

## 使用方式与范围

在飞牛 NAS 的 OpenClaw 中说「列出我的酷狗歌单」或「读取酷狗『我喜欢』」。`kugou-playlist` 接到读取请求时临时启动 KuGouMusicApi，首次使用时用手机酷狗扫码，后续复用保存在技能目录的登录会话。清单生成后关闭服务。

- `kugou-playlist` 负责酷狗授权和歌单读取。列歌单时返回名称、ID 和曲目数。读取指定歌单时输出曲目清单，保留原顺序与重复项。
- 对「把『我喜欢』里没入库的歌下载到 `/music/华语`」这类请求，OpenClaw 先取得清单，再将清单路径交给 `music-download`。由 `music-download` 判断是否需确认部分结果，并负责清单去重、NAS 全库查重、下载、入库和批次续跑。
- 首版支持由用户发起的单账号、单歌单任务。定时同步、酷狗歌单写回和播放器歌单导出留到后续。

## 飞牛 NAS 部署

在 OpenClaw 的运行环境里预装 Node.js、固定版本的 KuGouMusicApi 源码及其运行依赖，并配置 `KUGOU_API_DIR`。OpenClaw 若部署在飞牛 Docker 里，便在现有容器内启动临时 Node 子进程，无需增加常驻容器或发布端口。首次部署时准备依赖，运行时只需访问酷狗上游。开发参考提交为 `443d4a416dd344ccf7b022b9fb932e324afc77b4`，同一个会话保持相同的 KuGouMusicApi 平台配置。 [KuGouMusicApi 参考提交](https://github.com/MakcRe/KuGouMusicApi/tree/443d4a416dd344ccf7b022b9fb932e324afc77b4)

| 容器内配置示例 | 用途 |
| --- | --- |
| `KUGOU_API_DIR=/opt/kugou-music-api` | 已安装依赖的 KuGouMusicApi 目录，可随 OpenClaw 镜像部署或只读挂载。 |
| `skills/kugou-playlist/data/` | 技能默认运行数据目录。`session.json` 保存登录，`pending/` 保存扫码状态，`manifests/` 保存导出的曲目清单，`kugou-handoff/` 保存待提交的下载清单。将 `data/` 持久化挂载，容器重建或更新技能后仍可复用。技能目录只读时可用 `KUGOU_STATE_DIR` 指向可写的持久化目录。 |

登录状态归 OpenClaw 运行账号所有。数据目录使用 `0700`，会话和扫码文件使用 `0600`。Git 忽略 `/skills/kugou-playlist/data/`，技能打包时也排除该目录。`kugou-playlist` 使用 JSON 保存会话和曲目清单。`music-download` 只接收清单路径。

### 按需启动与关闭

1. `login-start`、`login-finish`、`playlists`、`tracks` 各自在需要请求酷狗时启动一个临时子进程。先检查运行依赖与本地状态。缺少登录会话时，读取命令直接返回 `auth_required`，本地 `logout` 直接清理状态。
2. 用一个薄 Node 启动脚本调用上游 `startService()`，设置 `HOST=127.0.0.1`、`PORT=0`，由系统分配端口。等子进程的 `listening` 事件，从 `app.service.address().port` 向父进程报告实际端口。父进程设置启动超时，只向该子进程监听的本地地址发送请求。参考版本的启动日志会打印配置中的 `0`，应以监听器返回的实际端口为准。 [上游服务入口](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/server.js)
3. 一次读取复用该进程完成分页，请求成功、失败或取消时均关闭并回收子进程。父进程在 `finally` 或中断处理里关闭子进程，超出宽限期再强制终止。子进程监视来自父进程的标准输入，管道关闭时退出，避免任务异常退出后留下后台进程。释放进程后再等待用户确认扫码或选同名歌单。
4. `login-start` 返回二维码后关闭服务。用户扫码确认后，`login-finish` 启动新的本地进程，从短期文件恢复二维码 key 和与申请二维码时一致的设备 Cookie，完成登录并写盘。随后用另一次短期进程读歌单。上次真实账号测试通过 Mac 上的临时 HTTP 服务完成。这里的跨进程扫码、飞牛连通性和重启后会话复用列入验收。

## 持久化登录

用 Python 标准库实现本地 HTTP 客户端和文件操作。`data/session.json` 保存带 `schema_version` 的登录会话，记录平台、保存时间和 Cookie。保留 `token`、`userid`、`t1`、`dfid` 和 API 返回的设备标识，如 `KUGOU_API_GUID`、`KUGOU_API_DEV`、`KUGOU_API_MID`、`KUGOU_API_MAC`、`KUGOU_API_WEBGL` 与 `KUGOU_API_PLATFORM`。按响应合并 `Set-Cookie`，每次启动子进程时恢复保存的 Cookie。临时端口可以变化，会话只发给本次启动的回环地址，HTTP 客户端拒绝带凭据的重定向。扫码成功时用响应中的 `token`、`userid` 补齐凭据，并用 `/user/playlist` 验证。只有验证成功的新会话才以同目录临时文件加原子替换写盘。并发情况下用会话锁防止跨账号覆盖和旧响应回写新会话。 [扫码状态源码](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/module/login_qr_check.js) · [服务端 Cookie 处理](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/server.js)

1. `login-start` 依次调用 `/login/qr/key` 和 `/login/qr/create?qrimg=1`，将二维码和待确认状态留在短期文件中。向用户展示官方二维码，提示用手机酷狗扫码并在手机上确认。响应只返回本地请求 ID 和二维码文件路径，登录 key、Cookie 和响应原文留在脚本内。扫码 URL 的主机和路径须匹配酷狗官方登录页。 [二维码生成源码](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/module/login_qr_create.js)
2. `login-finish --request <id>` 调用 `/login/qr/check`。三个扫码接口都带当前毫秒时间戳。等待扫码（`1`）或待确认（`2`）时以约两秒间隔短时轮询。`0` 表示二维码已过期，重新生成。`4` 表示授权成功，立即验证并持久化会话。提交成功或扫码过期后清理临时文件。以后读取歌单时直接载入会话，不再查询已用过的二维码。
3. 会话存在时直接读取歌单，不为每个任务主动续期。能确认酷狗拒绝登录态时返回 `auth_required` 并引导扫码，扫码后继续原任务。网络超时、限流、服务异常和响应结构变化返回各自的错误并保留会话。真实账号已验证扫码和读取。此前无凭据请求返回过 HTTP `502` 和 `error_code: 20010`，这条记录本身不足以把所有 `502` 或 `20010` 都归类为会话过期。实现时按固定版本验证明确的鉴权失败样本，并把未确认的业务错误按上游失败处理。
4. `logout` 只清除本机保存的会话和待扫码状态，曲目清单与下载进度可以继续使用。命令输出、HTTP 调试日志和批次报告不包含凭据或原始授权响应。

### 刷新接口

KuGouMusicApi 的 `/login/token` 可尝试用现有 `token` 和 `userid` 续期，成功时可能返回替换后的 token 和 Cookie。参考提交会请求 `http://login.user.kugou.com/v5/login_by_token`，公共请求模块还会把 token 放进上游请求参数。此前无凭据测试中，同域名的 HTTPS 证书主机名不匹配，扫码用的 HTTPS 域名对该路径返回 `404`。首版**只实现会话持久化与复用**。自动刷新等到确认有正常校验证书的 HTTPS 上游，并用真实账号验证成功、失效和凭据更新后再启用。届时在明确的鉴权失败后加锁尝试一次，验证新会话后原子保存，重试原请求一次。 [刷新接口文档](https://makcre.github.io/KuGouMusicApi/#/README?id=刷新登录) · [刷新源码](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/module/login_token.js) · [请求模块](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/util/request.js)

## 指定歌单与完整分页

| 操作 | 接口和处理 |
| --- | --- |
| 列出歌单 | `/user/playlist` 使用 `page` 和 `pagesize`，建议从每页 30 张开始。读取 `data.info`，保留名称、`listid`、`global_collection_id`、`count` 等可用字段。按名称精确匹配或按歌单 ID 选择。同名时列出候选及曲目数。 |
| 读取曲目 | 优先 `/playlist/track/all/new?listid=…`，从第 1 页开始，用固定 `pagesize`（建议以 100 为初始值）。以 `data.info` 为曲目响应主形态，确认实际字段后用测试样本固定解析。 |
| 备用接口 | `global_collection_id` 可用时，可以使用 `/playlist/track/all?id=…` 重新完整读取同一歌单。此接口用 `(page - 1) × pagesize` 计算偏移。先核对歌单身份和结果顺序。交付时采用其中一次独立读取结果，避免混合两套接口的分页导致曲目重复、缺失或重排。 |

曲目页按顺序追加并保留源重复项。按已读取的原始行数核对歌单详情中的 `count`，有曲目接口自己的总数时记录并对比。记录响应返回的 `page`、`pagesize`、`list_ver`、`snap` 或 `cursor` 以便诊断。参考提交的新版曲目接口只把 `page` 和 `pagesize` 传给上游，所以首版不把响应中的 `cursor` 当作已支持的续页参数。 [歌单列表源码](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/module/user_playlist.js) · [新版曲目接口](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/module/playlist_track_all_new.js) · [备用曲目接口](https://github.com/MakcRe/KuGouMusicApi/blob/443d4a416dd344ccf7b022b9fb932e324afc77b4/module/playlist_track_all.js)

- 请求短页后，如果尚未达到可信总数，继续验证下一页。对网络故障和限流进行有上限的退避重试，尊重 `Retry-After`。每页校验响应形态和可用的页号。发现重复页时避免追加同一页，但不能仅凭相同曲目 ID 判定页重复，需要结合页号、源条目位置或可用的版本信息。
- 读到可信总数且页序一致、可用的歌单版本未变化时记为 `complete`。明确的空歌单记为 `complete` 且为零首。提前空页、页号异常、页数上限、曲目数不符、总数不可确认或歌单读取中途变化，记为 `partial` 并说明原因。版本变化可从第一页重读一次。新版接口仍不完整时，优先尝试备用接口。如果两者均不完整，交付来源和顺序可验证的较完整结果。坏响应导致一首都读不到时返回错误。遇到某条曲目的元数据缺失时保留其位置，交给下游标记 `needs_review`。
- 已实测「我喜欢」读取 `565/565`，另一张歌单读取 `665/672`。当时后续页返回 HTTP `200` 和应用层 `error_code: 0`，因此还需核对页内容、上游总数和备用接口，才能解释 7 首差额。首版验收要求完整报告和正确拦截部分批次，差额原因保持待验证。

## `kugou-playlist` CLI 与清单

入口为 `skills/kugou-playlist/scripts/kugou_playlist.py`。命令输出一行 JSON，凭据与 HTTP 原始响应不进入标准输出。`login-start` 和 `login-finish` 支持分轮交互。`playlists` 返回 `playlists` 数组，包含名称、ID 和曲目数。`tracks --playlist-name <名称>` 或 `tracks --playlist-id <ID>` 将完整或部分结果写成清单，返回 `manifest_path`、`read_status`、`expected_count` 和 `fetched_count`。`logout` 清除本地登录会话。 [运行配置及命令](../../skills/kugou-playlist/references/runtime.md)

清单使用 UTF-8 JSON，文件原子发布。`position` 从 1 开始，指原播放顺序。`fetched_count` 在业务去重前统计。酷狗曲目 ID 当字符串处理并保留字段类型来源，音频 hash 可选。优先使用明确的歌曲标题、歌手、专辑和版本字段。真实接口样本中 `filename` 可见 `歌手 - 歌名.mp3`，结构化字段缺失时可拆分并移除已知音频后缀。保留原文件名，无法可靠拆分时用空值，避免把字段猜测带入自动下载。

```json
{
  "schema_version": 1,
  "source": "kugou",
  "playlist": {"id": "example", "name": "示例歌单", "list_version": null},
  "read_status": "complete",
  "expected_count": 1,
  "fetched_count": 1,
  "issues": [],
  "tracks": [
    {
      "position": 1,
      "track_id": null,
      "track_id_type": null,
      "audio_hash": null,
      "title": "成都",
      "artist": "赵雷",
      "album": null,
      "version": null,
      "source_filename": "赵雷 - 成都.mp3"
    }
  ]
}
```

## `music-download` 批量接入约定

`music-download` 沿用 `MUSIC_LIBRARY_ROOT=/music` 和 `MUSIC_LIBRARY_DB=/var/lib/openclaw/music/library.sqlite3`。SQLite 索引用于查找 NAS 里已入库的歌曲。建议将批次与逐曲进度持久化到 `/var/lib/openclaw/music/batches`。 [现有音乐库运行配置](../../skills/music-download/references/runtime.md)

用 `tracks --for-download` 生成临时清单，交给 `music-download batch-add --consume-manifest <path>`。批量入口校验清单版本、曲目和计数，部分清单经用户确认后加 `--allow-partial`。批次和曲目写入 SQLite 后清理临时清单。用 `batch-run --job <id>` 启动或续跑，用 `batch-status --job <id>` 查询。以下是原定批量工作流要求，实际命令和运行参数以 `music-download` 的参考为准。 [清单交接说明](../../skills/kugou-playlist/references/runtime.md#交给-music-download) · [批量下载说明](../../skills/music-download/references/runtime.md)

### 批量去重和续跑

1. **歌单内去重**。先用经实测能区分录音版本的曲目 ID，或用同一个音频 hash 识别重复。保留最早位置。缺少可靠标识时，仅在归一化后的歌名、歌手、已知版本和专辑能够确认一致时合并。元数据缺失或冲突的疑似重复保留候选，防止把同名现场版、翻唱版或重制版合并。歌单内重复数与 NAS 已有数单独统计。
2. **全库查重**。对整批复用一个 SQLite 连接，沿用 `lookup` 的归一化和命中文件验证。实际下载前复查该首，覆盖批次计划生成后新增的文件。同歌手、同歌名、同版本且有目标格式时跳过，记录现有路径。明确指定的目录用于新下载文件，库中其他目录的匹配也算已有。已知专辑或版本冲突、原字段缺失且无法确认录音版本时记为待确认。索引不可用时暂停相应处理，避免绕过去重直接下载。 [现有索引查询规则](../../skills/music-download/references/runtime.md)
3. **搜索与入库**。沿用现有 `search`、`detail`、`share` 和 `download`，在 Python 端抽取可复用的单曲操作，优先复用现有索引、校验、发布、LRC 匹配逻辑。只自动处理能确定来源和版本的曲目。版本或分享文件有歧义时标记 `needs_review` 并继续其他歌曲，由用户之后补选。默认 FLAC，用户指定其他格式则照办。只有现有单曲规则允许的 FLAC 超限场景才回退同版本 MP3，优先复用库中匹配 MP3。明确要求只要无损时保持 FLAC 要求。已确认转存成功的曲目沿用 `--staging-fid` 续跑。 [现有下载、MP3 回退与暂存说明](../../skills/music-download/references/runtime.md)
4. **进度持久化**。计划保存清单内容或不可变副本、目录、格式和回退策略。首版默认串行处理，一个音乐库同一时间运行一个批量下载任务，同批次重复调用返回已有进度。每首记录 `queued`、`duplicate_in_playlist`、`already_in_library`、`running`、`saved`、`needs_review` 或 `failed`，以及实际格式、路径、阶段和可用暂存 FID。可用独立 SQLite 记录事务更新。转存目录创建、转存成功和本地发布需有持久化进度点，这样进程在单曲返回之前退出也能找回暂存目录或音频。续跑时重新验证已保存文件，遇到中断的转存先核对网盘状态，确认文件未转存才重试。文件已发布但索引更新失败时优先修复该条索引，避免再次下载。已获准的 MP3 回退记录实际格式，续跑同一批次可复用结果。用户发起新的只要无损批次仍可尝试获取 FLAC。
5. **报告**。显示源歌单曲目数与读取数、清单内重复数、全库已有数、实际入库数、待确认及失败数、仍待处理数和批次 ID。待确认可按原条目位置补选。夸克重新授权、NAS 写入权限丢失或索引不可用这类全局故障暂停批次，恢复后沿用原批次 ID。

## 实施顺序与验收

| 顺序 | 交付和验收 |
| --- | --- |
| 1. 酷狗读取技能 | 新增 `SKILL.md`、脚本和运行参考。在飞牛用手机扫码一次，通过不同子进程完成扫码申请、确认和读取。验证任务结束、失败或中断后没有残留进程或端口。重启 OpenClaw 后复用技能数据目录中的会话。模拟会话明确失效、限流和网络中断，分别验证重新授权或保留会话。确认输出中无登录凭据。 |
| 2. 分页与清单 | 使用多页、短页、重复页、空歌单、同名歌单和读取中途发生版本变化的样本验证。用真实账号重跑 `565/565` 与曾少 7 首的歌单，输出完整性与精确计数。部分清单只在用户确认后交批量入口。 |
| 3. 批量入库 | 用包含重复曲目、已有 FLAC、只有 MP3、现场版、缺失曲目和歧义曲目的小清单测试。验证全库跨目录跳过、FLAC 上限回退及只要无损、人工选曲、夸克暂存复用、发布后索引失败恢复。中断并续跑同一批次，已确认入库的歌曲不再转存和下载。 |

本机用模拟接口验证了分页、重复项保留、部分读取和中断后的子进程清理。模拟清单提取出的曲目可用现有 `batch-add` 创建批次并识别重复项。

OpenCode 1.19.4 已发现项目技能，并通过会话命令完成真实扫码授权。后续命令复用本地会话，读取歌单目录并完整导出「我喜欢」的 `565/565` 首。实测另一张歌单仍为 `665/672`，清单标记为 `partial`。曲目接口报告 665 首，歌单目录报告 672 首，第 8 页响应无法解析。7 首差额的具体原因、飞牛重启后的会话复用和实际批量下载仍待验证。
