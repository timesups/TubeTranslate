# TubeTranslate / YouDub API（给 AI Agent 用）

机器可读接口说明。默认 API 根地址：`http://localhost:8000`。JSON 请求体；除非注明，所有受保护接口需要登录 Cookie + CSRF。

---

## 0. 快速决策

| 你要做什么 | 用哪个接口 |
|---|---|
| YouTube / Bilibili 单个链接 | `POST /api/tasks` |
| 多个链接批量 | `POST /api/tasks/batch` |
| 本地目录批量（扫盘） | `POST /api/task-packages` + `source_dir` |
| 本地逐视频列表（可附字幕） | `POST /api/task-packages` + `video_paths` |
| 浏览器上传本地文件 | `POST /api/tasks/upload`（multipart） |
| 查进度 | `GET /api/tasks/{id}` 或 `GET /api/task-packages/{id}` |

**不要**把 `video_paths` 当成 `source_dir` 的别名；二者互斥。  
**不要**传绝对路径作为 `export_dir`；批任务导出固定写到源视频同级 `Translate/` 目录。

---

## 1. 认证

### 登录

```http
POST /api/auth/login
Content-Type: application/json

{"password": "<明文口令>"}
```

成功：`200`，Set-Cookie `youdub_session=...`，body：

```json
{
  "authenticated": true,
  "csrf_token": "<string>",
  "expires_at": "<ISO-8601>"
}
```

### 后续请求规则

1. 带上 Cookie：`youdub_session`
2. **非安全方法**（POST/PUT/PATCH/DELETE）必须带请求头：
   - `X-CSRF-Token: <csrf_token>`
3. Origin 需被服务端允许（本地开发一般 OK；跨域需配置 CORS）
4. 公开例外：`GET /api/health`、`POST /api/auth/login`
5. 未登录受保护路由 → `401`；CSRF 失败 → `403`

### 会话

- `GET /api/auth/session` → 当前会话与 csrf
- `POST /api/auth/logout` → 登出（需 CSRF）

---

## 2. 核心概念

### 2.1 Task（单任务）

- 来源：YouTube / Bilibili URL，或本地上传 `local://upload/...`
- 含完整流水线，**含** B 站 `bilibili_meta` / `bilibili_publish`（可配置关闭自动投稿）
- 按 `video_id` 去重：同视频已存在则返回旧任务，不再新建

### 2.2 Task Package（批任务 / 本地文件夹包）

- 来源：本机/NAS 上的视频文件列表
- 流水线到 `merge_video` 为止（**不含** B 站投稿阶段）
- 成片导出到：`<源视频所在目录>/Translate/<同名.mp4>`
- 同时写出中英对照 SRT：`<同名.srt>`（有译文时）
- 按 `source_root` 去重：同目录已有包则返回 `already_existed: true`

### 2.3 流水线阶段

单任务：

1. `download` → 2. `separate` → 3. `asr` → 4. `asr_fix` → 5. `translate`  
→ 6. `split_audio` → 7. `tts` → 8. `merge_audio` → 9. `merge_video`  
→ 10. `bilibili_meta` → 11. `bilibili_publish`

批任务：仅 1–9。

特殊行为：

| 条件 | 行为 |
|---|---|
| 源视频无音轨 | `separate` 后直拷成片；跳过 asr…merge_video 媒体处理 |
| 批任务提供了源语言字幕（SRT/VTT） | 跳过 Whisper ASR / asr_fix 重切；**仍翻译** |
| 本地上传「已翻译 SRT」 | 跳过 Whisper + 翻译（字幕直接用于 TTS） |
| `audio_mode=replace` | 不混原 BGM |
| `audio_mode=keep_bgm` | Demucs 分离后人声配音 + BGM |

### 2.4 常用枚举

| 字段 | 取值 |
|---|---|
| `execution_mode` | `auto` \| `manual` |
| `audio_mode` | `replace` \| `keep_bgm` |
| `tts_provider` | `azure` \| `voxcpm`（`volcengine` 会归一为兼容值，勿新用） |
| `direction`（本地/批任务） | `en-zh` \| `zh-en` |
| 任务状态 | `queued` \| `running` \| `paused` \| `succeeded` \| `failed` |
| 包状态 | `queued` \| `running` \| `paused` \| `partial` \| `succeeded` \| `failed` |
| 包条目状态 | `pending` \| `queued` \| `running` \| `succeeded` \| `failed` \| `skipped` |

视频扩展名：`.mp4 .mov .mkv .m4v .webm .avi .flv .wmv`  
字幕：`.srt` / `.vtt`（UTF-8 / UTF-8-BOM）

---

## 3. 单任务 API

### 3.1 创建

```http
POST /api/tasks
```

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

- YouTube → 英译中；Bilibili → 中译英（由 URL 自动判定）
- 已存在同 `video_id` → 直接返回旧任务（HTTP 仍可能是 201 路径上的成功体；以 body `id` 为准）
- 运行时未就绪（缺 GPU/设备校验失败等）→ `409`

### 3.2 URL 批量

```http
POST /api/tasks/batch
```

```json
{
  "urls": ["https://...", "https://..."],
  "execution_mode": "auto",
  "audio_mode": "replace",
  "tts_provider": "azure",
  "bilibili_tid": 229,
  "bilibili_auto_publish": true,
  "bilibili_generate_meta": true
}
```

限制：最多 50 条 URL。返回大致形状：

```json
{
  "created": [{"url": "...", "task": {...}}],
  "existing": [{"url": "...", "task": {...}}],
  "errors": [{"url": "...", "detail": "..."}]
}
```

### 3.3 查询 / 控制

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks` | 列表（支持筛选查询参数） |
| GET | `/api/tasks/current` | 当前运行相关 |
| GET | `/api/tasks/{task_id}` | 详情 + stages |
| GET | `/api/tasks/{task_id}/log` | 纯文本日志 |
| GET | `/api/tasks/{task_id}/artifact/final-video?download=false` | 成片 |
| POST | `/api/tasks/{task_id}/pause` | 暂停 |
| POST | `/api/tasks/{task_id}/continue` | 继续（可带 `execution_mode`） |
| POST | `/api/tasks/{task_id}/resume` | 失败后从失败阶段恢复 |
| POST | `/api/tasks/{task_id}/rerun` | 整任务重跑 |
| POST | `/api/tasks/{task_id}/stages/{stage_name}/redo` | 从指定阶段重做 |
| DELETE | `/api/tasks/{task_id}` | 删除 |
| POST | `/api/tasks/{task_id}/cleanup-files` | 清理工作文件 |
| POST | `/api/tasks/batch-delete` | `{"task_ids":[...]}` |
| POST | `/api/tasks/batch-cleanup-files` | 批量清文件 |
| POST | `/api/tasks/batch-resume` | 批量恢复失败任务 |

### 3.4 本地上传（multipart）

```http
POST /api/tasks/upload
Content-Type: multipart/form-data
```

字段：`file`（视频）、可选 `subtitle_file`（**已翻译** SRT）、`direction`、`execution_mode`、`audio_mode`、`tts_provider`、B 站相关 form 字段。

有 `subtitle_file` 时：跳过 Whisper **和** OpenAI 翻译（字幕当作目标语）。

---

## 4. 批任务 API（重点）

### 4.1 预扫描（可选）

```http
POST /api/task-packages/scan
```

```json
{
  "source_dir": "D:/Videos/Course",
  "recursive": false,
  "skip_if_export_exists": true
}
```

返回匹配到的文件列表与 `will_skip` 标记。不创建任务。

### 4.2 创建批任务

```http
POST /api/task-packages
```

**必须且只能二选一：**

- `source_dir`：扫描目录
- `video_paths`：显式文件列表（可逐条附字幕）

#### A) 扫目录

```json
{
  "source_dir": "\\\\nas\\Media\\Course",
  "name": "Course",
  "direction": "en-zh",
  "execution_mode": "auto",
  "audio_mode": "replace",
  "tts_provider": "azure",
  "skip_if_export_exists": true,
  "continue_on_error": true,
  "export_subtitle": false,
  "recursive": false,
  "auto_start": true
}
```

#### B) 逐视频（推荐给外部系统）

```json
{
  "video_paths": [
    {
      "path": "\\\\nas\\Media\\Course\\01.001 Intro.mp4",
      "subtitle": "\\\\nas\\Media\\Course\\01.001 Intro.srt"
    },
    {
      "path": "\\\\nas\\Media\\Course\\01.002 Next.mp4"
    }
  ],
  "name": "Course",
  "direction": "en-zh",
  "execution_mode": "auto",
  "audio_mode": "replace",
  "tts_provider": "azure",
  "skip_if_export_exists": true,
  "continue_on_error": true,
  "auto_start": true
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `video_paths[].path` | 视频绝对路径（UNC/本地均可，**服务端进程必须能读**） |
| `video_paths[].subtitle` 或 `subtitle_path` | 可选源语言字幕（`.srt` / `.vtt`）；提供则跳过 Whisper |
| `direction` | `en-zh`：英→中；`zh-en`：中→英 |
| `skip_if_export_exists` | `Translate/` 里已有同名导出则跳过该条目 |
| `continue_on_error` | 单条失败是否继续下一条 |
| `auto_start` | 默认 `true`；`false` 则只入库不入队 |
| `export_subtitle` | 历史字段；成片旁中英对照 SRT 现已默认导出 |
| `name` | 包名；空则用目录名 |

成功：`201`，body 含 `id`、`items[]`、`already_existed`。

常见错误：

| 情况 | HTTP | detail |
|---|---|---|
| 既没 `source_dir` 也没 `video_paths`，或两者都有 | 422 | `Provide exactly one of source_dir or video_paths.` |
| 缺 `source_dir`（旧客户端只发 video_paths 前） | 422 | `Field required` @ `source_dir`（升级后应消失） |
| 路径不存在 / 不在 `PACKAGE_ALLOWED_ROOTS` | 422 | 路径校验文案 |
| SRT 非法 | 422 | `Invalid SRT subtitle file: ...` |
| 全部已导出且 `skip_if_export_exists` | 422 | `All matching files already have exported outputs.` |

环境限制：

- `PACKAGE_ALLOWED_ROOTS`：分号分隔的允许根路径；空=不限制
- `PACKAGE_MAX_ITEMS`：默认 200，上限 500
- `PACKAGE_EXPORT_DIR_NAME`：默认 `Translate`

### 4.3 源语言字幕语义（重要）

批任务 `video_paths[].subtitle` = **源语言**时间轴字幕：

- ✅ 跳过 Whisper `asr`
- ✅ 跳过 `asr_fix` 模型重切（按 SRT cue 写 asr/asr_fixed）
- ✅ **仍执行** `translate` → TTS → 成片
- ❌ 不是「已译好的目标语字幕」

本地上传 UI 的可选 SRT = **已翻译**字幕（会跳过翻译）。两套语义不同，勿混淆。

### 4.4 包的查询与控制

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/task-packages?limit=50` | 列表 |
| GET | `/api/task-packages/{id}` | 详情 + items + stages |
| POST | `/api/task-packages/{id}/continue` | 继续（paused/partial/failed） |
| POST | `/api/task-packages/{id}/pause` | 暂停 |
| POST | `/api/task-packages/{id}/retry-failed` | 重置失败条目并重入队 |
| DELETE | `/api/task-packages/{id}` | 删除包 |
| POST | `/api/task-packages/batch-delete` | `{"package_ids":[...]}` |
| POST | `/api/task-packages/batch-cleanup-files` | 清工作文件 |
| POST | `/api/task-packages/batch-retry-failed` | 批量重试失败 |

### 4.5 输出产物位置

对源文件 `\\nas\Course\01.mp4`：

```
\\nas\Course\Translate\01.mp4          # 成片
\\nas\Course\Translate\01.srt          # 中英对照 SRT（有译文时）
```

工作会话在服务端 `WORKFOLDER/packages/<package_id>/items/...`（默认在项目 `data/` 一类目录）。

---

## 5. 设置类（简述）

通常由 Web UI 配置；Agent 一般只需确认已配好：

| 路径 | 用途 |
|---|---|
| `GET/POST /api/settings/openai` | 翻译 API |
| `POST /api/settings/openai/models` | 拉模型列表 |
| `GET/POST /api/settings/azure-tts` | Azure TTS |
| `POST /api/settings/azure-tts/voices` | 音色列表 |
| `GET/POST /api/settings/ytdlp` | yt-dlp 代理端口 |
| `GET/POST /api/cookies/youtube` | YouTube Cookie |

B 站相关挂在 `/api/bilibili/...`（登录、分区、投稿等）。批任务**不**自动投稿。

---

## 6. Agent 推荐工作流

### 本地课程批量 + 可选英文字幕

```text
1. POST /api/auth/login → 保存 Cookie + csrf_token
2. POST /api/task-packages
   Cookie + X-CSRF-Token
   body: video_paths[{path, subtitle?}], direction=en-zh, auto_start=true
3. 轮询 GET /api/task-packages/{id}
   直到 status ∈ {succeeded, partial, failed}
4. 到源目录 Translate/ 取 mp4 + srt
```

伪代码：

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
    "audio_mode": "replace",
    "tts_provider": "azure",
    "skip_if_export_exists": True,
    "continue_on_error": True,
    "auto_start": True,
    "video_paths": [
        {"path": r"\\nas\Course\01.mp4", "subtitle": r"\\nas\Course\01.srt"},
        {"path": r"\\nas\Course\02.mp4"},
    ],
}
resp = client.post("/api/task-packages", json=payload, headers=headers)
resp.raise_for_status()
package = resp.json()
# package["already_existed"] 为 True 时说明同 source_root 已存在，未新建
```

### 同目录重复提交

同一 `source_root` 再次创建 → 返回已有包，`already_existed: true`，**不会**新建。要强制重建：先 `DELETE /api/task-packages/{id}`。

---

## 7. 错误与排错清单

| 现象 | 排查 |
|---|---|
| 422 `source_dir` Field required | 客户端还在用旧契约；改用 `video_paths` 或补 `source_dir` |
| 422 path must be under PACKAGE_ALLOWED_ROOTS | 把路径挂进允许根，或清空该环境变量 |
| 401 Authentication required | 未登录或 Cookie 丢失 |
| 403 CSRF validation failed | 忘了 `X-CSRF-Token` 或 token 过期，重新 login |
| 409 运行时未就绪 | GPU/设备校验失败，看服务端日志 |
| 任务卡在 download | 路径对服务端不可见（盘符映射、权限、未挂载 NAS） |
| 有字幕仍跑 Whisper | 确认创建时写入了 `subtitle`/`subtitle_path`，且包条目 `subtitle_path` 非空 |
| 成片旁没有 srt | 静音直拷或无译文；正常译制应有 `Translate/*.srt` |

健康检查：`GET /api/health` → `{"status":"ok"}`。

---

## 8. 给代码生成器的硬约束

1. 写操作必须 Cookie + `X-CSRF-Token`。
2. `POST /api/task-packages`：`source_dir` XOR `video_paths`。
3. 批任务字幕 = 源语言；上传字幕 = 已译目标语。
4. 不要发送 `export_dir` 绝对路径期望生效；导出目录名由服务端配置决定。
5. 路径字符串用服务端能访问的形式（Windows 服务用 `\\server\share\...` 或本机盘符）。
6. 轮询包状态，不要假设同步完成。
7. ASR 提供商只有 Whisper；不要再传 `asr_provider`。
8. 单任务 URL 批量上限 50；批任务视频上限见 `PACKAGE_MAX_ITEMS`。

---

*文档对应当前仓库实现；若与代码冲突，以 `backend/app/main.py` 为准。*
