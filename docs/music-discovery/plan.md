# `music-discovery` 技能方案

## 使用方式

在飞牛 NAS 的 OpenClaw 里说「查网易云热歌榜」或「最近哪几首蹿得快」，默认返回对应榜单前 5 首。也可以指定数量、获取 RSS 订阅链接，或要求将选中的曲目交给 `music-download`。 [技能入口](../../skills/music-discovery/SKILL.md)

| 榜单 | 官方来源 | 订阅 |
| --- | --- | --- |
| 热歌榜 | [网易云热歌榜](https://music.163.com/discover/toplist?id=3778678) | [RSS](https://rsshub.rssforever.com/163/music/playlist/3778678) |
| 飙升榜 | [网易云飙升榜](https://music.163.com/discover/toplist?id=19723756) | [RSS](https://rsshub.rssforever.com/163/music/playlist/19723756) |

## 查询和订阅口径

默认读网易云榜页随页面返回的曲目数据，保持原顺序。榜页公开，无需登录。响应中的「最近更新」用于展示来源更新时间。飙升榜返回当期排名，只有获得可核实的历史名次时才能报告涨幅。

RSS 由第三方 RSSHub 从网易云歌单生成。用户可以在阅读器中订阅，也可以用 `--rss` 读取其中的歌名和歌手。实测 RSS 条目顺序与官方榜页不同，查看精确排名时用官方数据。RSSHub 会缩短过长的条目标题，只在确认歌曲名完整时导出。 [RSSHub 歌单路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/163/music/playlist.ts)

连接超时、响应格式变更或曲目数据异常时，命令返回失败并说明来源。用户只需要发现近期曲目时，可以尝试 RSS 并标明实际来源。

## 下载交接

查询脚本在标准输出返回 UTF-8 JSON 数组，字段为 `title`、`artist` 和可用的 `album`。保留歌名里的版本后缀，由下游按现有规则识别。多位歌手用 `/` 连接。来源和更新时间通过标准错误输出，重定向标准输出即可生成清单。

用户明确要求下载时，将这份清单直接交给 `music-download batch-add --input`，再用返回的批次 ID 运行下载。用户选择了部分曲目时，对本次已展示的清单筛选后再交接。下载选源、版本核对和 NAS 查重由 `music-download` 处理。 [音乐发现运行参考](../../skills/music-discovery/references/runtime.md) · [批量下载入参](../../skills/music-download/references/runtime.md#批量下载与续跑)

## 验收

- 对照官方页面验证两榜导出的顺序，并检查数量上限可用户指定。
- 验证两条 RSS 可读取，并覆盖 RSSHub 截短联名歌手字段以及无法恢复完整歌名的情况。
- 将真实榜单 JSON 交给原有 `batch-add --input`，用 `batch-status` 查看入参、曲目总数和队列状态。验收使用临时音乐库和数据库。

本机验证了官网与 RSS 的热歌榜各 200 首、飙升榜各 100 首，四份清单均成功建立下载批次。单元测试从仓库根目录运行 `python3 -m unittest discover -s skills/music-discovery/tests`。
