# YouDub

视频自动配音全栈工具：支持 YouTube（英→中）、Bilibili（中→英）与本地视频上传，完成下载、人声分离、ASR、翻译、TTS、合成，并可自动投稿到 B 站。

- 后端：FastAPI + 任务队列 + SQLite
- 前端：Next.js Web 控制台（夜间模式，侧栏「任务 / 投稿」）
- 许可：Apache-2.0

## 功能概览

- 批量创建任务（链接 / 本地视频）
- 创建时可指定 **B 站投稿分区**（默认：知识区 / 科学科普，`tid=201`）
- 自动 / 手动执行模式；失败可从失败阶段继续，也可整任务重跑或按阶段重做（手动模式）
- 任务列表：筛选、搜索、批量删除、批量重试失败任务
- TTS 可选：VoxCPM、火山引擎、Azure Speech
- 音频模式：保留背景音乐（Demucs）或替换整轨音频
- OpenAI 兼容翻译接口（也用于生成 B 站标题/简介）
- API 口令登录（Argon2id）
- B 站投稿：设置中 **扫码登录**；流水线末尾自动生成简介并投稿（自制稿）

### 处理流水线

1. `download` — 下载 / 导入视频，并保存原平台缩略图为封面候选  
2. `separate` — Demucs 人声分离（「替换原音轨」时跳过，直接抽音轨）  
3. `asr` — Whisper 语音识别  
4. `asr_fix` — 断句整理  
5. `translate` — OpenAI 兼容接口翻译  
6. `split_audio` — 按句切分人声（云端 TTS 可能跳过）  
7. `tts` — 语音合成  
8. `merge_audio` — 合成配音轨  
9. `merge_video` — 合成最终视频（可额外导出到 `OUTPUT_DIR`）  
10. `bilibili_meta` — 导出暂存包；用翻译 API 生成标题/简介/标签  
11. `bilibili_publish` — 投稿到 B 站（自制稿，`copyright=1`）  

## B 站投稿

### 使用前准备

1. 在 **设置** 中配置 OpenAI 兼容翻译接口（与配音翻译共用）  
2. 打开 **设置 → B 站投稿**，用哔哩哔哩 App **扫码登录**  
3. 可选：默认标签、暂存目录等  

### 自动流程

配音任务在「合成视频」后会自动执行「生成简介」与「投稿」：

- 封面优先使用下载阶段保存的 **原视频缩略图**；没有时再从成片抽帧  
- 分区使用 **创建任务时选择的分区**  
- 暂存目录默认 `data/bilibili/staging`  
- 未登录时 `bilibili_publish` 会失败：扫码登录后，从该阶段「继续 / 重做」即可  
- 简介生成若模型返回非 JSON，会自动重试；仍失败则用字幕生成兜底文案  

侧栏 **投稿** 页仍可扫描暂存目录，手动补生成简介或补投稿。

B 站 Cookie 保存在本机 `data/bilibili/`，请勿提交到 Git。

## 环境要求

| 组件 | 建议版本 |
|------|----------|
| Python | 3.12 |
| Node.js | 20+（推荐 22） |
| FFmpeg | 需在 `PATH` 中，或通过 `FFMPEG_PATH` / `FFPROBE_PATH` 指定 |
| NVIDIA GPU | 可选；使用 CUDA 时先装 CUDA 版 PyTorch |

Demucs 以 git 子模块形式依赖：

```bash
git submodule update --init --recursive
```

## 快速开始

### 1. Python 虚拟环境与依赖

Windows（PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# GPU（CUDA 12.8）请先装 PyTorch，再装其余依赖
pip install -r requirements-pytorch-cu128.txt
pip install -r requirements.txt

# 仅 CPU / 无 NVIDIA 时可跳过上一行 cu128 文件，自行安装对应 torch
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
# 按本机驱动选择 PyTorch：https://pytorch.org/get-started/locally/
pip install -r requirements.txt
```

验证 CUDA（可选）：

```bash
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

### 2. 前端依赖

```bash
npm --prefix apps/web install
```

### 3. 配置环境变量

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

也可参考更完整的 `env.txt.example`，再同步到 `.env`。

**必填：** `YOUDUB_AUTH_PASSWORD_HASH`（Argon2id）。生成方式：

```bash
python -c "from pwdlib import PasswordHash; print(PasswordHash.recommended().hash('你的密码'))"
```

将输出写入 `.env`：

```env
YOUDUB_AUTH_PASSWORD_HASH=$argon2id$v=19$m=...
DEVICE=cuda
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=sk-...
OPENAI_MODEL=deepseek-v4-flash
```

常用项：

| 变量 | 说明 |
|------|------|
| `DEVICE` | `auto` / `cuda` / `cpu` / `mps` 等 |
| `WORKFOLDER` | 任务会话与中间文件目录（默认 `./workfolder`） |
| `OUTPUT_DIR` | 可选；完成后额外导出成片与字幕 |
| `FFMPEG_PATH` / `FFPROBE_PATH` | FFmpeg 不在 PATH 时指定 |
| `MERGE_VIDEO_ENCODER` | 合成视频编码：`auto`（默认）、`copy`、`x264`、`nvenc`、`qsv`、`amf`；`auto` 时竖屏优先 copy，横屏烧字幕优先 GPU |
| `MERGE_VIDEO_CRF` / `MERGE_VIDEO_NVENC_PRESET` | 合成质量与 NVENC preset（默认 CRF/CQ 23、p4） |
| `PACKAGE_ALLOWED_ROOTS` | 目录批处理允许扫描的根目录（分号分隔）；留空则不限制 |
| `PACKAGE_OUTPUT_SUFFIX` | 批处理导出后缀（默认 `_译制`） |
| `HTTP_PROXY` / `NO_PROXY` | yt-dlp / HTTPX 代理 |
| `AZURE_TTS_*` | Azure 云端 TTS（也可在 Web 设置里配置；`AZURE_TTS_SUBSCRIPTION_KEY` 支持多个 key，用逗号/换行分隔） |

翻译与 B 站简介生成都走 Web **设置 → OpenAI**（或上述环境变量默认值），无需再单独配置 DeepSeek Key。

### 4. 启动

Windows 可直接：

```bat
start.bat
```

会打开：

- API：<http://localhost:8000>
- Web：<http://localhost:3000>

或分别启动：

```bash
# 终端 1 — API
npm run dev:api
# 等价于：uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2 — Web
npm run dev:web
```

浏览器打开 Web，使用上述密码登录。推荐流程：

1. 设置里配置翻译 API、TTS（如需）、并扫码登录 B 站  
2. 首页粘贴链接或上传本地视频，选择执行模式 / TTS / **B 站分区**  
3. 在任务详情查看各阶段进度；失败可从失败阶段继续  

## 项目结构

```text
.
├── apps/web/                 # Next.js 前端
│   └── src/app/publish/      # B 站投稿台
├── backend/app/              # FastAPI 后端与流水线
│   └── bilibili/             # 扫码登录 / 暂存 / 元数据 / 上传投稿
├── backend/tests/            # 后端测试
├── scripts/                  # 辅助脚本（如单次跑流水线）
├── submodule/demucs/         # Demucs 子模块
├── workfolder/               # 运行时任务数据（gitignore）
├── data/                     # 模型缓存、日志、SQLite、B 站 Cookie/staging（gitignore）
├── requirements.txt
├── requirements-pytorch-cu128.txt
├── .env.example
└── start.bat
```

## 开发与测试

```bash
# 后端测试
npm run test:backend
# 或：pytest backend/tests

# 前端测试 / lint / 构建
npm --prefix apps/web test
npm run lint:web
npm run build:web
```

CI 定义见 `.github/workflows/ci.yml`。

## 常见问题

**`DEVICE=cuda` 但提示 PyTorch 为 CPU 版**  
先卸载再按 CUDA 源重装：

```bash
pip uninstall -y torch torchaudio
pip install -r requirements-pytorch-cu128.txt
```

**`Numba needs NumPy 2.4 or less`**  
项目已约束 `numpy>=2.0,<2.5`。若被升级，可执行：

```bash
pip install "numpy>=2.0,<2.5"
```

**移动/重命名项目目录后 `uvicorn` 报 Fatal error in launcher**  
Windows 下 `.venv\Scripts\*.exe` 会硬编码旧路径。用当前解释器重建入口，或重建虚拟环境：

```bash
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps uvicorn
# 更稳妥：删除 .venv 后按「快速开始」重装
```

**Turbopack 开发服务器 panic**  
删除 `apps/web/.next` 后重启；项目已默认关闭 Turbopack 持久化缓存。仍异常时可：

```bash
npm --prefix apps/web run dev -- --webpack
```

**voxcpm 与 datasets 版本冲突**  
`voxcpm` 需要 `datasets>=3,<4`。若被升到 5.x：

```bash
pip install "datasets>=3,<4"
```

**`bilibili_meta` 报「模型返回内容不是合法 JSON」**  
已增强解析与重试，并带字幕兜底。请更新到最新代码后，在任务详情「从失败阶段继续」。仍异常时检查翻译 API 是否可用、模型是否支持 JSON 输出。

**`bilibili_publish` 失败：未登录**  
到 **设置 → B 站投稿** 扫码登录后，从投稿阶段继续即可。Cookie 过期需重新扫码。

**Azure TTS 返回空音频**  
常见于文本与所选音色 locale 不匹配（例如藏文用中文音色）。请在设置中选择匹配的语音，或检查日志中的 Azure 错误详情。

## 许可

Apache-2.0。Demucs 子模块遵循其上游许可证。
