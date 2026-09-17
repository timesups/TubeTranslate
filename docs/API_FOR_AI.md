# TubeTranslate / YouDub — 创建任务 API（供 AI Agent 读取）

> 更新目标：让 Agent **正确创建**单任务 / URL 批量 / 本地批任务，并知道轮询与产物位置。  
> 默认 API 根：`http://localhost:8000`  
> 与代码冲突时以 `backend/app/main.py` 为准。

---

## 0. 选哪个创建接口

| 场景 | 接口 | 说明 |
|---|---|---|
| 单个 YouTube / Bilibili 链接 | `POST /api/tasks` | 含 B 站简介/投稿阶段 |
| 多个链接（≤50） | `POST /api/tasks/batch` | 同上，按 URL 批量 |
| 本地目录扫盘批量 | `POST /api/task-packages` + **`source_dir`** | 到合成成片为止，**不含** B 站投稿 |
| 本地逐视频列表（可附字幕） | `POST /api/task-packages` + **`video_paths`** | 推荐给外部系统 / NAS |
| 浏览器上传本地文件 | `POST /api/tasks/upload` | `multipart/form-data` |

硬约束：

1. `source_dir` 与 `video_paths` **必须二选一**，不能同时有、也不能都没有。
2. **不要**传 `export_dir` 绝对路径指望生效；批任务导出固定为源视频同级 `Translate/`。
3. 写操作必须：Cookie `youdub_session` + 请求头 `X-CSRF-Token`。
4. 不要传已废弃字段 `asr_provider`（ASR 固定 Whisper）。

---

## 1. 认证（创建前必做）

```http
POST /api/auth/login
Content-Type: application/json

{"password":"<明文口令>"}
```

成功 `200`：

```json
{
  "authenticated": true,
  "csrf_token": "<string>",
  "expires_at": "<ISO-8601>"
}
```

随后所有创建请求：

- Cookie：登录返回的 `youdub_session`
- Header：`X-CSRF-Token: <csrf_token>`
- `Content-Type: application/json`（upload 除外）

未登录 → `401`；CSRF 失败 → `403`。

公开例外：`GET /api/health`、`POST /api/auth/login`。

---

## 2. 创建时共用概念

### 2.1 枚举

| 字段 | 合法值 | 默认（创建） |
|---|---|---|
| `execution_mode` | `auto` \| `manual` | `auto` |
| `audio_mode` | `keep_bgm` \| `replace` | 单任务/URL 批量/上传默认 `replace`；**批任务包默认 `keep_bgm`** |
| `tts_provider` | `azure` \| `voxcpm` | `azure` |
| `direction` | `en-zh` \| `zh-en` | 批任务/本地上传用；默认 `en-zh` |

含义：

- `keep_bgm`：Demucs 人声分离 + 后期配音与原 BGM 混合（批任务推荐默认）。
- `replace`：替换整轨音频，不保留原 BGM。
- `en-zh`：英语识别 → 中文翻译/配音。
- `zh-en`：中文识别 → 英语翻译/配音。  
  YouTube/Bilibili **链接任务**方向由 URL 自动判定，不传 `direction`。

### 2.2 流水线

**单任务 / 上传 / URL 批量：**

`download → separate → asr → asr_fix → translate → split_audio → tts → merge_audio → merge_video → bilibili_meta → bilibili_publish`

**批任务包（task-packages）：**

仅到 `merge_video`（无 B 站阶段）。

特殊：

| 条件 | 行为 |
|---|---|
| 无音轨视频 | 直拷成片；跳过 ASR/TTS 等 |
| 批任务附带源语言字幕（`.srt`/`.vtt`） | 跳过 Whisper；`asr_fix` 仍做英文短句合并；**仍翻译** |
| 本地上传「已翻译」字幕（`.srt`/`.vtt`） | 跳过 Whisper **和** 翻译；`asr_fix` 仍会跑短句整理 |
| 横屏成片 | 画面烧英文字幕；另导出中英对照 `.srt` |
| 竖屏成片 | 通常不烧字幕 |

### 2.3 媒体格式

- 视频：`.mp4 .mov .mkv .m4v .webm .avi .flv .wmv`
- 字幕输入：`.srt` / `.vtt`（UTF-8 / UTF-8-BOM）
- 批任务成片旁对照字幕输出：始终 `.srt`

### 2.4 环境限制（批任务）

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PACKAGE_MAX_ITEMS` | 每包最多视频数 | `400`（硬上限 500） |
| `PACKAGE_ALLOWED_ROOTS` | 允许的路径根，分号分隔；空=不限制 | 空 |
| `PACKAGE_EXPORT_DIR_NAME` | 导出子目录名 | `Translate` |

---

## 3. 单链接创建

### `POST /api/tasks` → `201`

```json
{
  "url": "https://www.youtube.com/watch?v=XXXXXXXXXXX",
  "execution_mode": "auto",
  "audio_mode": "replace",
  "tts_provider": "azure",
  "bilibili_tid": 229,
  "bilibili_auto_publish": true,
  "bilibili_generate_meta": true
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | YouTube 或 Bilibili |
| `execution_mode` | 否 | 默认 `auto` |
| `audio_mode` | 否 | 默认 `replace` |
| `tts_provider` | 否 | 默认 `azure` |
| `bilibili_tid` | 否 | B 站分区，默认 `229` |
| `bilibili_auto_publish` | 否 | 默认 `true` |
| `bilibili_generate_meta` | 否 | 默认 `true`；若开启自动投稿会强制生成简介 |

行为：

- 同 `video_id` 已存在 → 返回旧任务（不新建、不重新入队）。
- 新建后自动入队。
- 运行时未就绪 → `409`。

轮询：`GET /api/tasks/{id}`  
成片下载：`GET /api/tasks/{id}/artifact/final-video?download=1`（文件名优先用原视频标题）  
B 站标题简介：`GET /api/tasks/{id}/artifact/bilibili-meta`（`bilibili_meta` 成功后）

---

## 4. URL 批量创建

### `POST /api/tasks/batch` → `200`

```json
{
  "urls": [
    "https://www.youtube.com/watch?v=AAAAAAAAAAA",
    "https://www.bilibili.com/video/BVxxxxxxxxxx"
  ],
  "execution_mode": "auto",
  "audio_mode": "replace",
  "tts_provider": "azure",
  "bilibili_tid": 229,
  "bilibili_auto_publish": true,
  "bilibili_generate_meta": true
}
```

限制：去重后至少 1 条；最多 **50** 条。

返回形状：

```json
{
  "created": [{"url": "...", "task": {}}],
  "existing": [{"url": "...", "task": {}}],
  "errors": [{"url": "...", "detail": "..."}]
}
```

---

## 5. 本地批任务包创建（重点）

### `POST /api/task-packages` → `201`

必须且只能提供 **`source_dir`** 或 **`video_paths`**。

公共可选字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | 目录名 | 包显示名 |
| `direction` | `en-zh` | 翻译方向 |
| `execution_mode` | `auto` | |
| `audio_mode` | **`keep_bgm`** | 默认做人声分离 + BGM 混音 |
| `tts_provider` | `azure` | |
| `skip_if_export_exists` | `true` | `Translate/` 已有同名成片则跳过该条 |
| `continue_on_error` | `true` | 单条失败是否继续 |
| `export_subtitle` | `false` | 历史字段；成片旁中英对照 SRT **仍会导出** |
| `auto_start` | `true` | `false` 则只入库不入队 |
| `recursive` | `false` | 仅 `source_dir` 模式 |
| `glob` | 默认视频通配 | 仅 `source_dir` 模式 |

成功 body 含：`id`、`items[]`、`already_existed`、`source_root`、`status` 等。

同 `source_root` 已存在包 → 直接返回旧包，`already_existed: true`（不新建）。强制重建需先 `DELETE /api/task-packages/{id}`。

### 5.1 扫目录模式

```json
{
  "source_dir": "\\\\nas\\Media\\Course",
  "name": "Course",
  "direction": "en-zh",
  "execution_mode": "auto",
  "audio_mode": "keep_bgm",
  "tts_provider": "azure",
  "skip_if_export_exists": true,
  "continue_on_error": true,
  "recursive": false,
  "auto_start": true
}
```

可选预扫描（不创建）：

```http
POST /api/task-packages/scan
{"source_dir":"\\\\nas\\Media\\Course","recursive":false,"skip_if_export_exists":true}
```

### 5.2 逐视频模式（推荐）

```json
{
  "video_paths": [
    {
      "path": "\\\\nas\\Media\\Course\\01.001 Intro.mp4",
      "subtitle": "\\\\nas\\Media\\Course\\01.001 Intro.srt"
    },
    {
      "path": "\\\\nas\\Media\\Course\\01.002 Next.mp4",
      "subtitle_path": "\\\\nas\\Media\\Course\\01.002 Next.vtt"
    },
    {
      "path": "\\\\nas\\Media\\Course\\01.003 NoSub.mp4"
    }
  ],
  "name": "Course",
  "direction": "en-zh",
  "execution_mode": "auto",
  "audio_mode": "keep_bgm",
  "tts_provider": "azure",
  "skip_if_export_exists": true,
  "continue_on_error": true,
  "auto_start": true
}
```

| 字段 | 说明 |
|---|---|
| `video_paths[].path` | 视频绝对路径（UNC/盘符均可，**服务端进程必须可读**） |
| `video_paths[].subtitle` 或 `subtitle_path` | 可选；源语言 `.srt`/`.vtt`；有则跳过 Whisper，仍翻译 |

所有 `path` 必须落在同一共同父路径下（用于计算 `source_root`）。

### 5.3 批任务产物

源文件 `\\nas\Course\01.mp4` →

```
\\nas\Course\Translate\01.mp4   # 成片
\\nas\Course\Translate\01.srt   # 中英对照字幕（有译文时；中文在上、英文在下）
```

轮询：`GET /api/task-packages/{id}`  
直到 `status ∈ {succeeded, partial, failed}`。

### 5.4 常见 422

| detail / 现象 | 原因 |
|---|---|
| `Provide exactly one of source_dir or video_paths.` | 两者都缺或都有 |
| `Field required` @ `source_dir` | 旧客户端未升级，仍只认目录 |
| `Only .srt and .vtt subtitle files are supported.` | 字幕扩展名不对 |
| `Invalid subtitle file: ...` | 字幕内容解析失败 |
| `must be under PACKAGE_ALLOWED_ROOTS` | 路径不在白名单 |
| `At most N videos are allowed per package.` | 超过 `PACKAGE_MAX_ITEMS` |
| `All matching files already have exported outputs.` | 全部已导出且开启 skip |

---

## 6. 本地上传创建

### `POST /api/tasks/upload` → `201`

`multipart/form-data`：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 视频文件 |
| `subtitle_file` | 否 | **已翻译** `.srt`/`.vtt`（会跳过识别+翻译） |
| `direction` | 否 | `en-zh` / `zh-en` |
| `execution_mode` | 否 | 默认 `auto` |
| `audio_mode` | 否 | 默认 `replace` |
| `tts_provider` | 否 | 默认 `azure` |
| `bilibili_*` | 否 | 同单任务 |

---

## 7. Agent 推荐流程（本地课程 + 可选字幕）

```text
1. POST /api/auth/login → 保存 Cookie + csrf_token
2. POST /api/task-packages
   Headers: Cookie, X-CSRF-Token
   Body: video_paths[{path, subtitle?}], direction=en-zh, audio_mode=keep_bgm, auto_start=true
3. 若 already_existed=true → 用返回的 id，或先 DELETE 再建
4. 轮询 GET /api/task-packages/{id}
5. 到源目录 Translate/ 取 .mp4 + .srt
```

Python 示例：

```python
import httpx

base = "http://localhost:8000"
client = httpx.Client(base_url=base, timeout=120.0)

r = client.post("/api/auth/login", json={"password": "..."})
r.raise_for_status()
csrf = r.json()["csrf_token"]
headers = {"X-CSRF-Token": csrf}

payload = {
    "name": "My Course",
    "direction": "en-zh",
    "execution_mode": "auto",
    "audio_mode": "keep_bgm",
    "tts_provider": "azure",
    "skip_if_export_exists": True,
    "continue_on_error": True,
    "auto_start": True,
    "video_paths": [
        {"path": r"\\nas\Course\01.mp4", "subtitle": r"\\nas\Course\01.vtt"},
        {"path": r"\\nas\Course\02.mp4"},
    ],
}
resp = client.post("/api/task-packages", json=payload, headers=headers)
resp.raise_for_status()
package = resp.json()
package_id = package["id"]
# package["already_existed"] 为 True 表示复用了同 source_root 旧包
```

---

## 8. 创建后常用查询 / 控制（摘要）

### 单任务

| 方法 | 路径 |
|---|---|
| GET | `/api/tasks/{id}` |
| GET | `/api/tasks/{id}/log` |
| GET | `/api/tasks/{id}/artifact/final-video?download=1` |
| GET | `/api/tasks/{id}/artifact/bilibili-meta` |
| POST | `/api/tasks/{id}/pause` \| `continue` \| `resume` \| `rerun` |
| DELETE | `/api/tasks/{id}` |

### 批任务包

| 方法 | 路径 |
|---|---|
| GET | `/api/task-packages/{id}` |
| POST | `/api/task-packages/{id}/continue` \| `pause` \| `retry-failed` |
| DELETE | `/api/task-packages/{id}` |

---

## 9. 给代码生成器的硬约束清单

1. 写操作：`youdub_session` Cookie + `X-CSRF-Token`。
2. `POST /api/task-packages`：`source_dir` XOR `video_paths`。
3. 批任务默认 `audio_mode=keep_bgm`；单任务/URL 批量默认 `replace`。
4. 批任务字幕 = **源语言**（跳过 ASR，`asr_fix` 仍短句合并，仍翻译）；上传字幕 = **已译目标语**（跳过 ASR+翻译）。
5. 字幕扩展名只接受 `.srt` / `.vtt`。
6. 不要发送会生效的绝对 `export_dir`；导出目录名由服务端配置。
7. 路径必须对**运行 API 的机器**可见（Windows UNC / 本机盘符）。
8. 创建后异步执行；轮询状态，勿假设同步完成。
9. URL 批量上限 50；批任务视频上限默认 400（`PACKAGE_MAX_ITEMS`）。
10. 同目录/同 `source_root` 重复创建会返回已有包；要重建先删除。

---

*本文档仅覆盖「创建任务」及相关轮询/产物；设置类、B 站登录扫码等见 Web UI 或源码 `backend/app/main.py` / `backend/app/bilibili/`。*
