# 客户端直连 STRM 的云盘画质切换

适用于客户端连接原 Jellyfin 或 MediaFlux 媒体反代，取得 MediaFlux 签名 STRM 地址，再直接播放云盘内容的场景。不需要修改 Jellyfin 插件，也不需要为不同画质生成多个 STRM。

## 请求流程

1. 从 Jellyfin `PlaybackInfo` 的选定媒体源取得 HTTP(S) `Path`；经 MediaFlux 反代时优先读取下述 `MediaFluxPlayback.source_url`。保留完整路径及 `v`、`sig`、可选 `enc` 查询参数。
2. 对这个 URL 增加 `view=variants`，用 **GET** 查询该文件当前已有的云端画质。
3. 用户选档后，对同一个原 URL 增加 `view=resolve&quality=<variant.id>`，取得短时 CDN 地址及格式。
4. 播放器直接请求返回的 `url`；切换时保存当前时间和播放/暂停状态，用新资源恢复到这个时间。

只改变查询参数，不拼接新的目录、不去掉原签名、不重新生成 STRM。`quality` 以返回的 `id` 为准，不能用“1080”或按数组下标猜。列表可能只有原画；本功能只读取云盘已有资源，不创建转码任务。

### 通过 MediaFlux 媒体反代发现入口

反代会改写 `Path`、`DirectStreamUrl` 用于普通 302 播放，同时在对应的 `MediaSource` 上保留独立的画质入口：

```json
"MediaFluxPlayback": {
  "protocol": "mediaflux.playback.v1",
  "source_url": "https://mediaflux.example/playgy/<file>/<etag>/<size>/<filename>?v=1&sig=<signature>"
}
```

客户端确认 `protocol` 后，使用 `source_url` 调用同一套 `variants` / `resolve` 接口，不受代理将 `Protocol` 改为 `File` 的影响。入口使用本次连接的反代协议、主机和端口，保留原 STRM 路径和签名，由反代直接复用签名验证及云盘画质服务。入口必须与反代实际绑定的文件相符；本地文件、缺少签名或绑定到另一文件的来源不会获得该入口。多个版本各自携带自己的入口，不能在版本间复用。

Pathfin 仍需开启“STRM 原始地址”。关闭时沿用 Jellyfin 画质协商；开启后原服务器和反代服务器共用云盘切档流程。旧客户端忽略此扩展，继续原有 302 行为。通过公网反代连接时，手机无需访问 STRM 中的内网面板端口，也无需重新生成 STRM；仅画质查询经过反代，视频仍直接访问云盘 CDN。若前面还有 HTTPS 反向代理，应保留公网 Host，并在可信代理配置下正确传递 `X-Forwarded-Proto`。直接连接原 Jellyfin 时，原 STRM 地址仍需能由手机访问。

### 画质列表

```http
GET /playgy/<file>/<etag>/<size>/<filename>?v=1&sig=<signature>&view=variants
```

```json
{
  "protocol": "mediaflux.playback.v1",
  "variants": [
    {
      "id": "original", "original": true, "label": "原画",
      "width": 1920, "height": 1080,
      "video_codec": "h264", "audio_codec": "aac",
      "bitrate": 5000000, "duration": 1440,
      "container": "mov,mp4,m4a,3gp,3g2,mj2",
      "transport": "file", "mime_type": "video/mp4"
    },
    {
      "id": "<resource-gcid>", "original": false, "label": "1080P",
      "width": 1920, "height": 1080,
      "video_codec": "hevc", "audio_codec": "aac",
      "bitrate": 1200000, "duration": 1440,
      "container": "mpegts", "transport": "hls",
      "mime_type": "application/x-mpegURL"
    }
  ]
}
```

- `bitrate` 单位 bit/s，`duration` 单位秒；未知数值或编码返回 `null`，不能解释成零。
- `container` 保留提供商探测格式，可能是 FFmpeg 格式族；播放器应使用 `mime_type` 决定 HLS，而不是解析 `container` 字符串。
- 同为 1080，原画与云端压缩版仍是两个不同资源。宽银幕可能实际是 1920×816，应显示真实尺寸，不把标签当作实际高度。
- HEVC 是否可解码取决于客户端设备；无法解码的档位应提示或禁用，不能假装已切换。

### 解析所选画质

```http
GET /playgy/<file>/<etag>/<size>/<filename>?v=1&sig=<signature>&view=resolve&quality=<variant.id>
```

```json
{
  "protocol": "mediaflux.playback.v1",
  "variant": { "id": "<resource-gcid>", "original": false, "label": "720P", "width": 1280, "height": 720, "video_codec": "hevc", "audio_codec": "aac", "bitrate": 800000, "duration": 1440, "container": "mpegts", "transport": "hls", "mime_type": "application/x-mpegURL" },
  "url": "https://<cloud-cdn>/<temporary-signed-resource>",
  "expires_in": 21600
}
```

服务端重新检查资源属于这个文件，再取短时地址。`expires_in` 是提供商声明的有效秒数，未知为 `null`，不是播放成功保证。不要持久化 CDN 地址替换 STRM。链接失效时重新 resolve 当前档位，并从原播放位置恢复；同一次恢复失败后应显示错误，不无限重试。

两个 JSON 视图均只支持 GET，成功响应 `Cache-Control: no-store`。不带 `view` 的原有 GET 仍返回原画 302，HEAD 的原文件大小、Range、ETag 行为不变。不能用原文件 HEAD 的大小推断 HLS 清单大小。

## Pathfin / Media3 接入点

现有 `PlaybackResolver` 已可选用 `MediaSource.Path`，`PlaybackViewModel` 已会把 `ResolvedPlayback.mimeType` 传给 `MediaItem.Builder.setMimeType()`，并支持带位置更换媒体。可复用这些路径：

- **画质来源分开**：普通媒体的 `PlaybackQuality.HIGH/STANDARD/DATA_SAVER` 表示 Jellyfin 转码码率上限；支持协议的云盘 STRM 在同一菜单中使用真实 `variants` 列表。云盘切档调用 `resolve`，不通过 `api.playbackInfo(quality=...)` 触发 Jellyfin 转码。
- **短时资源与原始来源分开**：保留原始签名 STRM URL 用于下次枚举/解析，播放 URI 才换成 CDN URL；切档仍属于同一 Jellyfin item/source，正常上报播放进度。
- **显式 MIME**：光鸭 HLS 地址未必以 `.m3u8` 结尾。构建 `ResolvedPlayback` 时设置 `mimeType = response.variant.mime_type`、`trustedServer = false`；HLS 必须显式 `application/x-mpegURL`。
- **保留时间和状态**：复用现有切源/失败回退流程；不要清零播放进度。转码资源内的音轨可能与原文件不同，应读取新流的真实轨道，不能直接照搬原文件轨道索引。
- **网络隔离**：调用 MediaFlux 和 CDN 时不发送 Jellyfin Token/Cookie。沿用已有逐跳凭据隔离；HLS 非 BYTERANGE 分片不要强加原文件 Range。

发现接口时应关闭自动重定向，并验证 HTTP 200、JSON Content-Type 和 `protocol`。旧 MediaFlux 不识别 `view` 时可能返回原画 302，不能跟随该响应把整部电影当 JSON 下载。发现不可用只隐藏云端画质入口，保留原播放行为；不改变普通本地媒体的 Jellyfin 画质功能。

本接口不依赖客户端额外配置云盘账号；签名 STRM URL 本身是播放凭据。不要在日志、截图、遥测中记录完整签名地址或 CDN URL。

## 错误处理

| HTTP | 含义 | 客户端处理 |
|---|---|---|
| 400 | 查询参数不合法 | 修正 view/quality，不重试相同参数 |
| 403 | STRM 签名缺失或无效 | 检查原始 Path 是否完整 |
| 404 | 所选资源已不可用或不属于该文件 | 刷新列表；不要静默播放另一档 |
| 405 | JSON 视图使用了 HEAD | 改用 GET |
| 503 | 光鸭未登录 | 提示服务端重新登录 |
| 504 | 云端只读请求超时 | 保留当前播放，可重试选档 |
| 502 | 云端拒绝、数据不完整或取流失败 | 保留当前播放并显示失败 |

列表存在不等于当前账号一定能取流：实际会员权限、资源状态、CDN 和设备解码能力仍以解析/播放结果为准。失败不得破坏正在播放的原资源。
