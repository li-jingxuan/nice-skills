# 运行参考

在 OpenClaw 所在主机或容器运行。需要 Python 3.9+ 和访问 `music.163.com` 的网络。读取 RSS 时另需访问 `rsshub.rssforever.com`。使用 Python 标准库，无需登录。

```bash
python3 scripts/music_discovery.py hot --limit 10
python3 scripts/music_discovery.py rising
python3 scripts/music_discovery.py rising --rss
```

`--limit` 接受正整数，默认 5。来源返回少于指定数量的曲目时，输出实际可用数量。官方榜单保持页面顺序。RSS 模式保持订阅条目顺序，提供歌名和歌手。网络或解析错误返回非零状态，并在标准错误中说明原因。标准输出只包含 JSON，来源和可核实的更新日期写到标准错误。

## 交给 `music-download`

清单采用 `batch-add --input` 支持的普通 JSON 数组。`title` 必填，`artist`、`album`、`version` 可选。输出保留源歌名里的版本后缀，交给现有下载技能识别。有多位歌手时使用其支持的 `/` 分隔。

```json
[
  {"title": "成都", "artist": "赵雷", "album": "无法长大"}
]
```

下面示例在同一台主机上执行。将两个脚本路径替换成已安装 Skill 的实际路径。成功建立批次后，普通输入文件仍保留，临时清单可在确认导入后清理。

```bash
chart_file=$(mktemp) &&
  python3 /path/to/music-discovery/scripts/music_discovery.py hot --limit 10 > "$chart_file" &&
  python3 /path/to/music-download/scripts/music_download.py batch-add --input "$chart_file"
```

`batch-add` 返回 `job` 后，按 `music-download` 的 `batch-run` 和 `batch-status` 流程执行。若用户要求保存清单，使用其指定文件路径并保留文件。`--consume-manifest` 是酷狗临时清单的入口。
