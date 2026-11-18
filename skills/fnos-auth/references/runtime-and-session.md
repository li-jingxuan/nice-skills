# 运行环境与会话规则

## 三类认证材料

### 官方后端 API 的应用 token

官方调用方式规定：应用服务端经 Unix Socket `/var/run/trim_open_gateway_apiscope.socket` 调用 `POST /api/v1/trimapp`，使用 `Authorization: Bearer <token>`；token 来自系统调用应用脚本时注入的 `TRIM_API_TOKEN`。仅声明实际需要的 `api-scope`。

从当前进程环境读取，不固化在配置、数据库、文件或静态资源。重新安装、注册或运行环境变化后可能更新；长驻进程是否已获得新环境，应结合应用生命周期验证。应用 token 的认证通过不意味着所有用户目录或业务资源已授权。

这条官方路径与下面观察到的内部相册会话协议不同，不要将 `TRIM_API_TOKEN` 填入 `Trim-Token` 或暴露给浏览器。

### Desktop SDK 会话能力

`@trimjs/web-app` 0.5.0 README 将 `getSessionCredentials()` 标为“Desktop 官方应用会话凭据（0.5.0 候选）”，仅支持宿主核验的官方内嵌 Desktop 应用。虽然该版本发布包可从 npm 下载，其 README 仍写着本地候选、发布后再供下游安装；不能据此认定任意部署宿主都支持稳定可用的该能力。其 `code: 0, data: { secret, token? }` 示例不能证明手机 `getUserMessage` 返回相同字段。先读目标版本 README、类型、实现及宿主支持说明，再判断是否适用。

### 当前用户会话与上传凭据

内部相册 JSON API 使用浏览器当前登录 Cookie；上传还使用 secret 签名和可选 token。登录状态、上传材料和相册 ACL 必须分别验证，不能因某一项存在便推断其他项通过。

## 手机原生桥

SDK 0.5.0 可用于核对的机制：

- 桥入口是 `flutter_inappwebview.callHandler`；SDK README 提及手机方法 `getUserMessage`，未声明它的凭据 payload 字段。
- Apple 分支允许使用已有桥；其他平台使用 `_platformReady` 或 `flutterInAppWebViewPlatformReady`。应记住早已发生的一次性事件，并兼容桥晚于页面注入。
- 传输使用 JSON envelope，其中 `result` 还可能是一层 JSON 字符串。传输格式的依据不等于特定方法的字段契约。

以下为来源项目报告经过回归测试的兼容策略（记录摘要见 [来源与验证](evidence-and-validation.md)），本技能未附原项目测试运行器，仍需在目标宿主验证：

- 可检查同源直接父窗口的桥；跨源父窗口访问失败即停止该检查，不发送跨源会话请求。
- 分别记录当前窗口与父窗口就绪状态，优先使用已就绪的桥。不能把 UA 中的 Apple 分类当作身份认证。
- 一次获取最多调用桥一次，初始化和调用共享超时；历史参考实现为 5 秒，可根据目标环境设置，不规定为平台保证。
- `signal` 在开始前、调用桥前和处理返回前检查。结束时清理临时监听、轮询和计时器，忽略超时或取消后的迟到响应。原生调用本身可能无法终止，至少保证结果不再驱动业务请求。
- 每次获取只允许首次终态生效：取消先发生则保留取消结果；超时先发生则保留超时结果，之后的取消、回调或凭据返回不再覆盖结果。结束标记应在确定终态时设置，防止事件竞争继续驱动请求。

## 响应解析与校验

只有确认目标字段映射后才实现解析器。历史兼容实现接受对象或 JSON 字符串，依次读取可选 `result` 和 `data` 包装，最终提取 `secret` 与可选 `token`；包含 `code` 时只接受数值 `0`。不递归搜索未知字段，不猜字段别名。

建议沿用的防御界限：

- JSON 字符串输入限制为 64 KiB，包装层数固定；拒绝数组、null、基本类型和畸形 JSON。
- secret 必须是字符串，长度不超过 4096，能按浏览器 Base64 解码且结果非空；格式通过不验证它是否属于当前用户。
- token 缺省可以归一为空字符串；提供时必须是字符串，长度不超过 8192，限可打印 ASCII，拒绝 CR/LF 和其他控制字符。
- 凭据仅在本次操作内存中流转；不放进 localStorage、URL、剪贴板、异常详情或上报系统。

这些上限和包装规则来自兼容实现，不是官方承诺；若真实宿主结构不同，应以脱敏字段形状取证后修改。

## 原生空返回时的 Cookie 兼容

适用条件同时满足：

1. 当前为手机宿主路径，桥已满足就绪条件并完成本次调用。
2. 原始返回严格为 JavaScript `null` 或 `undefined`。
3. 目标版本已有宿主提供同源可读 Cookie 的证据。

读取 `document.cookie` 时按 cookie 名精确匹配 `fnos-Secret` 和 `fnos-token`：secret 恰好一个，token 至多一个；同名多值拒绝，防止路径 Cookie 歧义。值仅执行一次 `decodeURIComponent`，然后应用与原生相同的凭据校验。解码失败、访问被阻止、缺失或格式无效分别产生固定诊断状态。

不能触发兜底的情况：缺桥、桥未就绪、超时、拒绝、非零业务码、非空畸形返回、`"null"` 或包装内的 null。有效原生结果整组优先，不用 Cookie 补齐其中缺省的 token。手机不读取可能残留的网页 localStorage。

Cookie 来源的依据是某 NAS 自带 Docker 前端读取这些名称的实现观察，不代表所有宿主都会注入。测试时不手工造 Cookie 再将结果称为真实宿主验证。

## 网页宿主兼容

历史网页宿主曾提供 `localStorage["fnos-Secret"]` 与可选 `fnos-token` Cookie，可在已确认的同源宿主中读取并验证。iframe 与顶层页面同源也不自动保证存储材料存在；不同来源拥有不同存储。

这条路径只适用于已确认的网页宿主。独立浏览器、无会话的页面或无法读取存储时，输出相应错误；不能为了“兼容”读取其他来源、改用共享管理员会话或将手机降级为网页旧会话。

## 浏览器请求与身份边界

使用 `new URL(fixedPath, window.location.origin)` 生成经过审核的内部请求地址；固定路径应以单个 `/` 开始，不接受 `//host`、用户传入的完整 URL 或任意重定向。设置 `credentials: "same-origin"` 和 `redirect: "error"`，浏览器自动携带当前来源适用的 Cookie。

HttpOnly Cookie 无法通过 `document.cookie` 读取，这是预期行为。统一网关过滤系统登录 Cookie 的实现已有项目观察，不能依赖应用后端透传用户会话；改写 Nginx 或读取 NAS 私有会话文件不属于本兼容方案。

内部上传观察到 `Trim-Sign = Base64(HMAC-SHA256(Base64Decode(secret), encodedPath))`，可选 token 使用 `Trim-Token`；具体编码、字段和端点应重新核对目标服务版本。JSON API 的 `authx` 是另一套服务协议，不与 `Trim-Sign` 互换。本技能不附内部相册完整调用器或固定签名材料。

原生上传材料和浏览器 Cookie 可能不一致。格式校验不能证明同账号；以目标服务支持的身份/授权检查及同一设备完整操作验证一致性。不输出标识符来比对，不把任意 403 归为身份不一致已证实。
