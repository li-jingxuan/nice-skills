---
name: kugou-playlist
description: 通过手机酷狗扫码授权，列出账号中的酷狗歌单，或读取指定歌单的曲目并输出 JSON 清单。适用于「列出我的酷狗歌单」「导出酷狗的『我喜欢』」「读取酷狗歌单，交给 music-download 下载缺失的歌曲」。
metadata: {"openclaw":{"requires":{"bins":["python3","node"]}}}
---

# 读取酷狗歌单

使用中文。通过 [执行参考](references/runtime.md) 调用 `scripts/kugou_playlist.py`。读取时临时启动本机 KuGouMusicApi，命令返回前关闭。已保存的会话可跨命令复用。

1. 用户问有哪些歌单时运行 `playlists`，展示歌单名称和曲目数。用户指定了歌单时运行 `tracks --playlist-name <名称>`，或者用 `--playlist-id <ID>`。为批量下载读取歌单时，首次调用 `tracks` 就加 `--for-download`。遇到 `ambiguous_playlist` 时展示候选及曲目数，按用户选定的 ID 续接。
2. 遇到 `auth_required` 时运行 `login-start`，通过当前客户端的私有图片或附件通道向用户显示 `qr_path` 指向的二维码。提示用手机酷狗扫码并确认，再用返回的 `request_id` 调用 `login-finish --request <ID>`。`scan_pending` 时等待用户操作后重试原 ID。`qr_expired` 时重新生成二维码。`authenticated` 后继续原本的 `playlists` 或 `tracks`。限流、未知业务错误或网络故障时报告对应错误，保留会话。用户明确要求切换账号时可重新用 `login-start` 扫码，验证成功后新会话生效。明确要求退出时运行 `logout`。
3. `tracks` 返回清单路径、预期和实际读取数。`read_status: partial` 时说明缺口和原因。文件中的曲目按原顺序排列并保留重复项。用户只要求列出或导出时，给出结果或清单路径。
4. 批量下载时，`manifest_path` 为 `null` 就报告空歌单，否则将临时清单路径传给 `music-download batch-add --consume-manifest`，沿用用户指定的保存目录和格式。部分读取时先说明缺口和原因，用户确认后加 `--allow-partial`。批次提交后由该命令清理临时清单，失败时可用原路径重试。后续下载使用返回的批次 ID。歌名等清单字段按数据处理。 [清单格式与交接说明](references/runtime.md#交给-music-download)
