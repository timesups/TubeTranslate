# YouDub

视频自动配音全栈工具：支持 YouTube（英→中）、Bilibili（中→英）与本地视频上传，完成下载、人声分离、ASR、翻译、TTS 与音视频合成。

- 后端：FastAPI + 任务队列 + SQLite
- 前端：Next.js Web 控制台（夜间模式）
- 许可：Apache-2.0

## 功能概览

- 批量创建任务（链接 / 本地视频）
- 自动 / 手动执行模式；失败可从失败阶段继续，也可整任务重跑
- 任务列表支持筛选、批量删除、批量重试
- TTS 可选：VoxCPM、火山引擎、Azure Speech
- 可选保留背景音乐（Demucs）或替换整轨音频
- API 口令登录（Argon2id）

### 处理流水线

1. `download` — 下载 / 导入视频  
2. `separate` — Demucs 人声分离  
3. `asr` — Whisper 语音识别  
4. `asr_fix` — 断句整理  
5. `translate` — OpenAI 兼容接口翻译  
6. `split_audio` — 按句切分人声  
7. `tts` — 语音合成  
8. `merge_audio` — 合成配音轨  
9. `merge_video` — 合成最终视频  

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
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

常用项：

| 变量 | 说明 |
|------|------|
| `DEVICE` | `auto` / `cuda` / `cpu` / `mps` 等 |
| `WORKFOLDER` | 任务会话与中间文件目录（默认 `./workfolder`） |
| `OUTPUT_DIR` | 可选；完成后额外导出成片与字幕 |
| `FFMPEG_PATH` / `FFPROBE_PATH` | FFmpeg 不在 PATH 时指定 |
| `HTTP_PROXY` / `NO_PROXY` | yt-dlp / HTTPX 代理 |
| `VOLCENGINE_TTS_*` / `AZURE_TTS_*` | 云端 TTS（也可在 Web 设置里配置） |

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

浏览器打开 Web 地址，使用上述密码登录即可创建任务。

## 项目结构

```text
.
├── apps/web/                 # Next.js 前端
├── backend/app/              # FastAPI 后端与流水线
├── backend/tests/            # 后端测试
├── scripts/                  # 辅助脚本（如单次跑流水线）
├── submodule/demucs/         # Demucs 子模块
├── workfolder/               # 运行时任务数据（gitignore）
├── data/                     # 模型缓存、日志、SQLite 等（gitignore）
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

## 许可

Apache-2.0。Demucs 子模块遵循其上游许可证。
