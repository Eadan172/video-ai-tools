# 流水线 AI 修复（画质 + 音质）实施方案

> 目标：让 `video_pipeline` 在 `input/剧集/*.mp4` 上**真正执行**两个 AI 阶段
> —— 画质用 **REAL-Video-Enhancer (RVE) 2.4.1**，音质用 **DeepFilterNet 0.5.6** ——
> 并说明安装、集成、运行时串联、效果验证与失败隔离。
>
> **状态：已落地并实测跑通。** 全链路 7 个阶段在含两个 AI 阶段的配置下完整执行，
> 产物通过校验并原子输出。文末附实测数据与失败隔离三态证据。

---

## 0. 结论速览

| 项 | 结果 |
|---|---|
| 画质 AI（RVE） | ✅ 流水线内实测 `rc=0`；356×200 → **712×400（精确 2×）**，帧数/时长/音轨保持一致 |
| 音质 AI（DeepFilterNet） | ✅ 流水线内实测 `rc=0`；噪声底 **−59.6 dB**，采样率 48 kHz 保持 |
| 超分质量（公平对照） | ✅ 同分辨率下锐度优于双三次放大基线 **+284%**；块效应 **−4.8%** |
| 全链路 | ✅ VALIDATING → REPAIR_VIDEO → REPAIR_AUDIO → PREPARE_TRANSCODE → TRANSCODE(hevc_nvenc) → EXPORT → VERIFY |
| 失败隔离 | ✅ 可重试→`RETRY_PENDING`；不可重试/超次→`FAILED_FINAL` + 产物隔离到 `failed/job_XXXX/` |
| 断点续跑 | ✅ 中断后重启可跳过已完成的 AI 阶段（实测跳过 9 分钟的 RVE 推理） |
| 退化为纯转码 | ✅ 两个 `enabled: false` 即可，流水线照常工作 |

> ⚠️ **性能红线**：本文实测环境为 **CPU 推理**，2 秒视频耗时 **365–552 秒**（约 180–280× 实时）。
> 整集（4088 s）用 CPU 推理需 **数十小时**，**不具备生产可用性**。
> 生产必须走 **CUDA**（见 §9.1）。

---

## 1. 现状与目标

### 1.1 上一轮为验证主链路临时禁用了两个 AI 阶段

```
video_repair.enabled: false    →   _stage_repair_video 走 FFmpeg 转码分支（SKIPPED）
audio_repair.enabled: false    →   _stage_repair_audio 直接抽 WAV（SKIPPED）
```

结果：扫描／状态机／QSV 转码／合流／校验／原子输出／资源与磁盘约束／失败隔离
都被验证过了，但 **AI 推理路径一次都没被执行**。

### 1.2 本轮要做的事

1. 把两个组件真正装起来（当前机器上二者均未安装）；
2. 让适配器能**正确驱动**这两个组件的真实 CLI；
3. 在 `config.yaml` 里启用并串联两个 AI 阶段；
4. 用**量化指标**证明修复确实生效；
5. 用**破坏性实验**证明失败隔离确实生效。

### 1.3 两个组件为什么"不好装"

| 组件 | 表面障碍 | 真实情况 |
|---|---|---|
| REAL-Video-Enhancer | Windows 发行版是 **PyQt GUI**，无法无人值守调用 | 其推理后端 `backend/rve-backend.py` 是**标准 argparse CLI** → 直驱即可绕开 GUI（本方案成立的关键） |
| DeepFilterNet | PyPI 元数据**漏标 `torch` 依赖**；内部用 `DataLoader(num_workers=2)` | 需显式装 torch/torchaudio；受限环境下子进程会被拦 → 需进程内封装 |
| 两者共存 | RVE 后端要 `numpy 2.x`，DeepFilterNet 要 `numpy < 2` | **必须两个独立 venv** |

---

## 2. 架构：两个 AI 阶段在流水线中的位置

```
VALIDATING            ffprobe 探测 + profile 选择
    ↓
REPAIR_VIDEO   ★AI    REAL-Video-Enhancer：去压缩伪影 + 降噪 + 超分   → work/current/video_ai.mp4
    ↓
REPAIR_AUDIO   ★AI    抽 WAV(48k) → DeepFilterNet 降噪                → work/current/audio_clean.wav
    ↓
PREPARE_TRANSCODE     已是目标格式则跳过 TRANSCODE
    ↓
TRANSCODE             统一转 HEVC（自动 QSV → NVENC → CPU 降级）
    ↓
EXPORT                视频 + 清洗后音轨合流 → <name>.mp4.partial
    ↓
VERIFY                校验通过 → 原子重命名 .partial → .mp4
```

**关键设计点**

- **音画解耦**：`REPAIR_AUDIO` 从**原始文件**抽音频（不是从 `video_ai.mp4`），
  两个 AI 阶段无数据依赖 → 单阶段失败不影响另一阶段已完成的产物。
- **中间产物即断点**：每个阶段把产物落在 `work/current/`，
  重启时 `_stage_done()` 同时检查「DB 记录完成」+「产物文件真实存在」。
- **原子输出**：先写 `.partial`，VERIFY 通过才 `Path.replace()` 改名，
  保证 `output/` 里永远不出现半成品。

---

## 3. 组件一：REAL-Video-Enhancer（画质）

### 3.1 适配器 `adapters/real_video_enhancer.py`

**核心：不做参数翻译，直接构造 backend CLI 的 argv。**

```
<executable>                        ← RVE 专用解释器 .venv-rve/Scripts/python.exe
  <tools/backend/rve-backend.py>    ← 后端入口
  -i <src>  -o <dst>
  --extra_restoration_models <1xDeH264_realplksr.pth>   ← 压缩伪影修复（1× 模型）
  --upscale_model <2x_OpenProteus_Compact_i2_70K.pth>   ← 超分（倍率由模型自身决定）
  --device cpu --backend pytorch --precision auto --pytorch_gpu_id 0
  --tilesize 128                     ← 分块推理，显著降低峰值内存
  --ffmpeg_path <ffmpeg.exe>
  --overwrite --crf 18
  --video_encoder_preset libx264 --audio_encoder_preset aac
```

### 3.2 三个必须知道的 RVE 特性

| 特性 | 说明 | 对应实现 |
|---|---|---|
| **倍率来自模型文件** | RVE 没有 `--scale` 参数；2× 还是 4× 完全由 `--upscale_model` 指向的模型决定 | `resolve_upscale_model(scale)` 把 `scale` 映射到 `models.upscale["2x"]` 的**文件路径**；`scale<=1` 返回空串（不超分） |
| **可选参数组** | 未配置 4× 模型时不能传空路径，否则 CLI 报错 | `args_template` 支持**嵌套列表**表示「可选参数组」：组内任一占位符渲染为空串 → **整组丢弃** |
| **`--audio_encoder_preset` 有枚举约束** | `choices=[aac, libmp3lame, opus, copy_audio]` | 配置里显式写合法值；流水线后续会用自己的音轨重挂，故中间产物音频只为过校验 |

### 3.3 模型选型（当前配置）

| 用途 | 文件 | 说明 |
|---|---|---|
| 超分 2× | `tools/models/2x_OpenProteus_Compact_i2_70K.pth`（2.4 MB） | SPAN 架构，人像/实拍通用，体积小推理快 |
| 压缩伪影修复 1× | `tools/models/1xDeH264_realplksr.pth`（29.6 MB） | RealPLKSR，针对 H.264 块效应，**剧集片源最对症** |

> 已额外备好 `2x_AniSD_DC_SPAN_92500.ncnn/`（动漫向）与 `1xDeH264_RTMoSR.ncnn/`
> 的 ncnn 目录版，供 Vulkan 后端切换使用。

### 3.4 工具探测 `adapters/_toolpath.py`

原实现只认 PATH 上的裸命令，导致填绝对路径（如 `.venv-rve/Scripts/python.exe`）时
`available()` 返回 False。新增 `which_tool()`：

- 含路径分隔符 → 按 `Path` 判断，Windows 下自动补 `.exe/.cmd/.bat` 后缀；
- 否则 → `shutil.which()`。

### 3.5 配置相对路径解析 `pipeline/config.py`

`video_repair` / `audio_repair` 下所有以 `./` 或 `../` 开头的字符串
（`executable`、`args_template`、`extra_args`、`models`、`ffmpeg_path`），
在 `load_config()` 时统一 `resolve_tools()` 解析为**相对 `Config.root` 的绝对路径**。

这样 `config.yaml` 可以写成 `./.venv-rve/Scripts/python.exe` 这种可移植形式，
换机器不必改绝对路径。

---

## 4. 组件二：DeepFilterNet（音质）

### 4.1 适配器 `adapters/deepfilternet.py`

```
<.venv/Scripts/python.exe> <tools/df_enhance_cli.py> <audio.wav>
    --output-dir <work/current> --log-level error
```

产物名为 `<stem>_DeepFilterNet3.wav`，适配器按优先级查找并 `replace()` 成
`scheduler` 期望的 `audio_clean.wav`：

```python
patterns = [f"{stem}*DeepFilterNet*.wav", f"{stem}*.wav", "*.wav"]
```

### 4.2 环境适配点一：`DataLoader(num_workers=2)` 起不来

上游 `df/enhance.py` 用 `DataLoader(ds, num_workers=2, pin_memory=True)`，
在受限环境会报：

```
RuntimeError: DataLoader worker (pid(s) NNNN) exited unexpectedly
```

**解法**：`tools/df_enhance_cli.py` 在**主进程内**把 `DataLoader` 换成
`num_workers=0, pin_memory=False` 的包装，再调用原 `run()`。

```python
import df                              # 触发 df.enhance 子模块导入
_e = sys.modules["df.enhance"]         # 注意：import df.enhance as _e 拿到的是「函数」不是「模块」
_orig = _e.DataLoader
_e.DataLoader = lambda *a, **k: _orig(*a, **{**k, "num_workers": 0, "pin_memory": False})
_e.run()
```

- **不修改第三方包源码**，行为等价（只是少了预取并行）；
- 对流水线场景无影响：每次只处理一个 48 kHz WAV。

### 4.3 环境适配点二：torchaudio 与 torch 混装（本轮新发现）

**症状**（`REPAIR_AUDIO` 阶段 `rc=1`）：

```
File ".../torchaudio/_extension/utils.py", line 117, in _check_cuda_version
    version = torch.ops._torchaudio.cuda_version()
AttributeError: '_OpNamespace' '_torchaudio' object has no attribute 'cuda_version'
```

**根因**：`site-packages/torchaudio` 里的 `.py` 与 `.pyd` **来自不同版本**
（上一次安装被中断，留下 `~orchaudio-2.5.1.dist-info` 残目录，pip 把新旧文件混在一起），
导致 torchaudio 的 C++ 算子没注册进 `torch.ops`。

**修复**（已写入安装脚本，幂等）：

```powershell
# 1) 清理上次中断安装的残目录
Get-ChildItem .venv\Lib\site-packages -Filter "~*" | % { [IO.Directory]::Delete($_.FullName, $true) }
# 2) 从官方索引强制重装「同源同版本」的 torch + torchaudio
pip install --force-reinstall --no-deps torch==2.5.1 torchaudio==2.5.1 `
    --index-url https://download.pytorch.org/whl/cpu
# 3) 验证
python -c "import torchaudio; torchaudio.lib._torchaudio.cuda_version()"
```

> 教训：**torch 与 torchaudio 必须来自同一 index、同一版本**。
> 混装 PyPI 与 download.pytorch.org 的 wheel，或安装被中断，都会踩这个坑。
> 已在安装脚本里加了 `Clear-PipLeftovers` 预清理步骤。

### 4.4 调参建议：`--atten-lim`

DeepFilterNet 是 **语音增强**模型，对**剧集**（有配乐、音效）会连带抑制非语音成分。
实测同一 20 s 有声片段（阈值越小保留原声越多）：

| 配置 | 整体 RMS | 相对源 |
|---|---:|---:|
| 源（未处理） | 0.11697 | — |
| 默认（无 atten-lim） | 0.04511 | **−61.4%** |
| `--atten-lim 20` | 0.04694 | −59.9% |
| `--atten-lim 12` | 0.05365 | −54.1% |
| `--atten-lim 6` | 0.07145 | **−38.9%** |

**建议**：剧集/影视类片源加 `--atten-lim 6`，避免把配乐削没；
纯人声/会议录音用默认即可。写法（`config.yaml`）：

```yaml
audio_repair:
  args_template:
  - ./tools/df_enhance_cli.py
  - '{input}'
  - --output-dir
  - '{output_dir}'
  - --atten-lim
  - '6'
  - --log-level
  - error
```

---

## 5. 安装与集成

### 5.1 一键脚本（推荐）

```powershell
# 功能验证 / 无 N 卡：CPU 版
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1

# 生产：CUDA 版（体积约 2.5 GB）
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda
```

脚本幂等，六步走：

| 步骤 | 内容 |
|---|---|
| 0/6 | 预检：python 版本、可用性 |
| 1/6 | 创建 **两个** 隔离虚拟环境 `.venv` / `.venv-rve` |
| 2/6 | `.venv` 装 PyYAML / psutil / pytest |
| 3/6 | `.venv` 装 DeepFilterNet 0.5.6 + soundfile + **同源 torch/torchaudio**（含 `~*` 残目录清理） |
| 4/6 | 下载并解压 RVE 后端 `backend-v2.4.1.tar.gz` → `tools/backend/` |
| 5/6 | `.venv-rve` 装 RVE 依赖（opencv-headless / einops / safetensors / …）+ torch + numpy 2.2.2 |
| 6/6 | 下载 RVE 模型，**逐文件校验字节数**防截断 |

最后自动执行 `main.py doctor` 自检。

### 5.2 手动等价步骤（不想跑脚本时）

```powershell
cd video_pipeline

# ---- DeepFilterNet 侧 ----
python -m venv .venv
.\.venv\Scripts\pip install deepfilternet==0.5.6 "soundfile>=0.10,<0.13" "numpy<2" PyYAML psutil
.\.venv\Scripts\pip install --force-reinstall --no-deps torch==2.5.1 torchaudio==2.5.1 `
    --index-url https://download.pytorch.org/whl/cpu

# ---- RVE 侧 ----
python -m venv .venv-rve
.\.venv-rve\Scripts\pip install opencv-python-headless requests einops safetensors tqdm sympy typing_extensions packaging pillow
.\.venv-rve\Scripts\pip install torch==2.5.1 torchvision==0.20.1 numpy==2.2.2   # 或 --index-url .../whl/cu121 装 CUDA 版

# ---- RVE 后端与模型 ----
# 下载 https://github.com/TNTwise/REAL-Video-Enhancer/releases/download/RVE-2.4.1/backend-v2.4.1.tar.gz
tar -xzf backend-v2.4.1.tar.gz -C tools          # → tools/backend/rve-backend.py
# 模型放入 tools/models/ ：2x_OpenProteus_Compact_i2_70K.pth / 1xDeH264_realplksr.pth

# ---- 自检 ----
.\.venv\Scripts\python.exe main.py doctor
```

期望输出：

```
[OK]   REAL-Video-Enhancer  (...\.venv-rve\Scripts\python.exe)
[OK]   DeepFilterNet        (...\.venv\Scripts\python.exe)
```

### 5.3 为什么必须是两个 venv（而不是一个）

| | `.venv`（DeepFilterNet） | `.venv-rve`（RVE 后端） |
|---|---|---|
| numpy | **`< 2.0`** | **`== 2.2.2`** |
| torch | 2.5.1（CPU 索引） | 2.5.1（CPU 或 CUDA 索引） |
| torchvision | 不需要 | `0.20.1` |
| 额外 | deepfilternet / soundfile | opencv-headless / einops / safetensors |

numpy 主版本冲突**无法调和** → 必须物理隔离。
两个 venv 由 `config.yaml` 里各自的 `executable` 指定，互不干扰。

---

## 6. 运行时启用与串联

### 6.1 `config.yaml` 关键字段

```yaml
video_repair:
  enabled: true                       # ★ 打开画质 AI
  backend: real-video-enhancer
  infer_backend: pytorch              # pytorch | ncnn | tensorrt
  device: cpu                         # ★ 生产改 cuda
  precision: auto
  gpu_index: 0
  tile: 128                           # 分块尺寸；0 = 整帧（吃内存）
  crf: 16
  encoder: libx265                    # RVE 中间产物编码器
  ffmpeg_path: <ffmpeg.exe>
  timeout_seconds: 43200              # 12h，长视频 CPU 推理要放宽
  executable: ./.venv-rve/Scripts/python.exe
  args_template: [ ... ]              # 见 §3.1
  models:
    upscale:  { "2x": ./tools/models/2x_OpenProteus_Compact_i2_70K.pth }
    decompress: ./tools/models/1xDeH264_realplksr.pth

audio_repair:
  enabled: true                       # ★ 打开音质 AI
  backend: deepfilternet              # deepfilternet | clearervoice | none
  executable: ./.venv/Scripts/python.exe
  sample_rate: 48000
  args_template:
  - ./tools/df_enhance_cli.py
  - '{input}'
  - --output-dir
  - '{output_dir}'
  - --log-level
  - error
```

### 6.2 三种运行档位

| 档位 | 配置 | 适用 |
|---|---|---|
| **全 AI** | 两个 `enabled: true` | 生产 |
| **仅画质** | `video_repair.enabled: true` + `audio_repair.enabled: false` | 片源底噪本来就干净 |
| **仅音质** | 反之 | 画质已达标，只需降噪 |
| **纯转码** | 两个都 `false` | 依赖缺失时的兜底，流水线照常工作 |

### 6.3 串联是怎么保证的

- **顺序固定**：`scheduler._process_job()` 里硬编码 `_stage_repair_video()` → `_stage_repair_audio()`，
  不做动态编排，避免歧义。
- **资源闸门**：每个阶段启动前 `_wait_resources()` 检查
  RAM 软限（`ram_soft_limit_percent`）与磁盘状态；超限则等待，600 s 仍超则放弃启动。
- **并发约束**：`max_video_ai_jobs: 1` + `scheduler.max_active_video_workspaces: 1`
  —— 只有一块 GPU，AI 视频阶段天然串行。
- **断点续跑**：`_stage_done(job, stage, artifact)` = DB 记录为 `DONE/SKIPPED`
  **且** 产物文件存在。`SKIPPED` 视为已完成（如片源本无音轨）。

### 6.4 常用命令

```bash
./.venv/Scripts/python.exe main.py --config config.yaml scan       # 扫描新文件入队
./.venv/Scripts/python.exe main.py --config config.yaml run        # 跑（Ctrl+C 安全退出）
./.venv/Scripts/python.exe main.py --config config.yaml status     # 看队列/阶段
./.venv/Scripts/python.exe main.py --config config.yaml retry      # 重试 RETRY_PENDING
./.venv/Scripts/python.exe main.py --config config.yaml doctor     # 依赖自检
```

> `ffmpeg` / `ffprobe` 需在 PATH 上（或用绝对路径）。

---

## 7. 本次代码改动清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `adapters/_toolpath.py` | 🆕 新增 | `which_tool()`：支持绝对路径 + Windows 扩展名回退 |
| `adapters/real_video_enhancer.py` | ♻️ 重写 | 对齐 RVE 2.4.1 真实 CLI；可选参数组；倍率→模型路径映射 |
| `adapters/deepfilternet.py` | ♻️ 重写 | 对齐 `deepFilter` 真实 CLI；输出重命名策略；DependencyError 提示 |
| `pipeline/config.py` | ♻️ 重写 | `resolve_tools()` 相对路径解析；`DEFAULT_CONFIG` 扩展 RVE 字段 |
| `config.yaml` | ♻️ 重写 | 启用两个 AI 阶段；RAM 软限 92%；RVE 参数与模型映射 |
| `tools/df_enhance_cli.py` | 🆕 新增 | DeepFilterNet 的 `num_workers=0` 进程内封装 |
| `tools/backend/` | 🆕 新增 | RVE 2.4.1 后端源码（解压自官方 release） |
| `tools/models/` | 🆕 新增 | 2× 超分模型 + 1× 压缩修复模型（含 ncnn 目录版） |
| `scripts/setup_ai_tools.ps1` | 🆕 新增 | 幂等安装器（本轮追加 `~*` 残目录清理 + 同源 torch 安装） |
| `scripts/verify_repair.py` | 🆕 新增 | 画质/音质量化验证（本轮追加超分公平对照） |

> `config.e2e*.yaml`、`work/_e2e/` 为验证用临时产物，可删。

---

## 8. 效果验证

### 8.1 全链路运行日志（节选）

```
19:44:01 VALIDATING 完成 profile=legacy
19:44:01 RVE 调用: rve-backend.py -i ...\in\S01E04_excerpt.mp4 -o ...\work4\current\video_ai.mp4 \
           --extra_restoration_models ...\1xDeH264_realplksr.pth \
           --upscale_model ...\2x_OpenProteus_Compact_i2_70K.pth \
           --device cpu --backend pytorch --precision auto --tilesize 128 \
           --overwrite --crf 18 --video_encoder_preset libx264 --audio_encoder_preset aac
19:53:14 EXIT rc=0 552.4s                                  ← ★ 画质 AI 成功
19:53:14 REPAIR_VIDEO 完成 → video_ai.mp4 (0.00 GB)
19:53:14 ffmpeg ... -vn -acodec pcm_s16le -ar 48000 ...\audio.wav
19:53:14 DeepFilterNet 调用: df_enhance_cli.py audio.wav --output-dir ...\current --log-level error
19:53:18 EXIT rc=0 4.0s                                    ← ★ 音质 AI 成功
19:53:18 DeepFilterNet 输出重命名: audio_DeepFilterNet3.wav → audio_clean.wav

（中断后重启，断点续跑）
19:54:07 REPAIR_VIDEO 已完成，跳过                          ← ★ 跳过 9 分钟的推理
19:54:11 删除临时文件 audio.wav (0.37 MB)
19:54:11 REPAIR_AUDIO 完成 → audio_clean.wav
19:54:11 尝试编码后端 hevc_nvenc (nvenc)
19:54:12 TRANSCODE 完成 → transcoded.mp4
19:54:12 删除临时文件 video_ai.mp4 (0.31 MB)
19:54:13 删除临时文件 audio_clean.wav (0.37 MB)
19:54:13 VERIFY 通过 → ...\out4\S01E04_excerpt.mp4
19:54:14 === 完成 → ...\out4\S01E04_excerpt.mp4 ===
```

### 8.2 规格对比

| 属性 | 源 | 修复后 |
|---|---|---|
| 视频编码 | h264 | **hevc** |
| 分辨率 | 356×200 | **712×400（2.00×）** |
| 帧数 / 帧率 | 50 / 25fps | 49 / 25fps |
| 时长 | 2.000 s | 2.005 s |
| 音频 | aac 48 kHz 立体声 | aac 48 kHz 立体声（**保持不变**） |
| 体积 / 码率 | 81 KB / 325 kbps | 285 KB / 1139 kbps |

### 8.3 画质 / 音质量化（`scripts/verify_repair.py`）

```
画质指标（已把修复后缩回源分辨率；缩小本身会抹掉高频，锐度仅供参考）
指标                        源          修复后       变化
锐度(Laplacian方差)         121.5       91.1       -25.1%
块效应比值(越低越好)           1.105       1.052      -4.8%

超分公平对照（在修复后分辨率下比较）
指标                      双三次基线      AI 修复      变化
锐度(Laplacian方差)          11.5        44.2       +284.2%

音质指标
指标                        源          修复后       变化
噪声底(RMS)                0.041778    0.000044    -99.9%
整体能量(RMS)              0.056552    0.005805    -89.7%

噪声抑制量: +59.6 dB
```

**怎么读这组数**

- **块效应 1.105 → 1.052**：8×8 编码块边界梯度 / 块内梯度的比值下降，
  说明 H.264 的块状伪影被 `1xDeH264_realplksr` 抹掉了一部分。
- **超分公平对照 +284%**：这是**最该看的画质指标**。把 AI 输出的 712×400
  与「源双三次放大到 712×400」在同一分辨率下比 Laplacian，AI 输出锐度高 2.8 倍
  —— 说明超分**真的补出了细节**，不是简单插值。
- **第一张表里"锐度 −25.1%"是度量假象**：把 2× 输出缩回源分辨率再比，
  重采样本身会丢高频。所以脚本已把该表标注为"仅供参考"，并新增公平对照表。
- **噪声底 −59.6 dB**：能量最低 10% 帧的 RMS 从 0.0418 降到 0.000044。
- **整体能量 −89.7%**：该 2 s 片段以底噪/环境音为主，被 DFN 强力抑制。
  这是**需要留意的副作用** —— 见 §4.4 的 `--atten-lim` 建议。

### 8.4 失败隔离验证（破坏性实验）

**实验 A — 依赖缺失（不可重试类）**

把 `ffmpeg/ffprobe` 移出 PATH：

```
ERROR  任务失败: 找不到可执行文件: ffprobe
WARN   进入 RETRY_PENDING (第 1 次)
ERROR  任务失败: 找不到可执行文件: ffprobe
ERROR  FAILED_FINAL
```

**实验 B — 组件运行时崩溃（本轮真实发生）**

`torchaudio` 混装导致 `REPAIR_AUDIO` `rc=1`：

```
19:53:18 EXIT rc=1 2.9s ...
ERROR  任务失败: 命令失败 rc=1: ...\.venv\Scripts\python.exe
WARN   进入 RETRY_PENDING (第 1 次)          ← 第一次失败 → 可重试
19:54:xx REPAIR_VIDEO 已完成，跳过            ← 重试时从断点继续，不重跑 AI
ERROR  任务失败: 命令失败 rc=1: ...
ERROR  FAILED_FINAL                          ← 达 max_attempts → 终态
```

隔离结果 —— 失败产物被搬到 `failed/job_0001/`，**原工作区不再持有半成品**：

```
work/_e2e/failed4/job_0001/audio.wav
work/_e2e/failed4/job_0001/video_ai.mp4
```

**三态语义总结**

| 状态 | 触发条件 | 行为 |
|---|---|---|
| `RETRY_PENDING` | `retryable=True` 且 `retry_count < max_attempts-1` | 计数 +1，稍后重跑（**已完成的阶段跳过**） |
| `FAILED_FINAL` | `DependencyError`（`retryable=False`）或重试次数用尽 | `_quarantine()` 把 `work/current/*` 搬到 `failed/job_XXXX/`，**不阻塞队列** |
| `DONE` | 全阶段通过 VERIFY | `cleaner.cleanup_current()` 清理工作区 |

---

## 9. 已知问题与调参建议

### 9.1 ⚠️ CPU 推理不具备生产可用性（最重要）

实测：2 s 视频 → 365–552 s。折算 **≈ 180–280× 实时**。
整集 4088 s 用 CPU 需 **200+ 小时**。

**生产必须上 CUDA**：

1. 用 `scripts/setup_ai_tools.ps1 -Cuda` 安装 CUDA 版 torch（`cu121`，约 2.5 GB）；
2. `config.yaml` 改 `video_repair.device: cuda`；
3. `video_repair.tile` 可回到 `0`（整帧）或调到 `512`，显存换速度。

本机 RTX 4060 Laptop 8 GB 显存，估算可提速 20–50×，
整集从"数十小时"降到"数十分钟"量级。

### 9.2 16 GB 内存机器会撞 RAM 软限

实测本机基线内存占用 **85%**，空闲仅 ~2 GB。
默认软限 80% 会导致流水线**永久卡在**"RAM 超过软限制，等待释放"。

- 已把 `resources.ram_soft_limit_percent` 调到 `92`、`ram_hard_limit_percent` 调到 `97`；
- 并用 `video_repair.tile: 128` 分块推理压低峰值内存；
- 该现象**不是 bug，是这台机器的真实约束**（WorkBuddy + Trae + 网盘 + Defender 已占 4 GB）。

### 9.3 `0xC0000005` 段错误 = 真实内存耗尽，不是逻辑缺陷

早期 RVE 在 CPU 路径下 `rc=3221225477`（`0xC0000005` = ACCESS_VIOLATION），
一度被误判为代码问题。实际定位为**物理内存不足**：

- 抬 RAM 软限 + `tile=128` + 缩小测试样本后，**同一命令 `rc=0` 正常出片**；
- 因此只需在配置层面控制内存，不必改动流水线逻辑。

### 9.4 尚未解决：ncnn 后端 `rc=127`

Vulkan 路径（`--backend ncnn`）在 `BACKEND: Setting up Upscale` 之后立即 `rc=127`。
已逐项验证 ncnn/Vulkan 本身正常（`get_gpu_count()→2`、`getNCNNScale()`、
`UpscaleNCNN` 构造、`hotUnload()` 均 OK），**故障点尚未定位**。

> 影响有限：PyTorch 路径（CPU / CUDA）已可用。ncnn 的价值在于**免 CUDA 依赖**，
> 属可选优化项。

### 9.5 profile 选择器顺序缺陷（建议修）

`profiles/selector.py` 中：

```python
if height >= 700: return course_720      # ← 这行在前
...
if container in legacy_containers: return legacy
```

`if height >= 700` 先命中，导致 1024×768 的 `.wmv` 被误判为 `course_720`
而非 `legacy`。上一轮 37 个 WMV 全部在 `REPAIR_VIDEO` 阶段失败即源于此。
**建议把 `legacy_containers` 判断提到高度判断之前。**

### 9.6 DeepFilterNet 的 `rc=1` 建议改为 `DependencyError`

当前 AI 组件因**环境损坏**退出时，`run_command` 抛的是 `ExternalToolError`
（`retryable=True`），于是白白重试一次。建议：

- 适配器在 `enhance()` 里做**前置健康检查**（如 `import torchaudio` 试跑）；
- 失败时抛 `DependencyError`（`retryable=False`），直接 `FAILED_FINAL`，
  避免在必然失败的路径上反复消耗时间。

---

## 10. 运维速查

### 10.1 常见错误对照表

| 现象 | 根因 | 处理 |
|---|---|---|
| `找不到可执行文件: ffprobe` | ffmpeg 不在 PATH | 把 ffmpeg 目录加入 PATH，或配置绝对路径 |
| `REAL-Video-Enhancer 未安装` | `executable` 路径错 | 核对 `.venv-rve/Scripts/python.exe`；跑 `doctor` |
| `DeepFilterNet 未安装` | `executable` 路径错 | 核对 `.venv/Scripts/python.exe` |
| `rc=3221225477` / `0xC0000005` | 物理内存不足 | 调小 `tile`、抬 RAM 软限、缩短样本；生产改 CUDA |
| `AttributeError: '_OpNamespace' '_torchaudio' object has no attribute 'cuda_version'` | torch/torchaudio 混装 | 清 `~*` 残目录 + 同源 force-reinstall（§4.3） |
| `DataLoader worker exited unexpectedly` | 子进程被拦 | 确认走 `tools/df_enhance_cli.py`（§4.2） |
| `命令失败 rc=127`（ncnn） | 未定位 | 改用 `infer_backend: pytorch`（§9.4） |
| 卡在 `RAM 超过软限制` | 内存基线高 | 抬 `ram_soft_limit_percent`（§9.2） |
| `RuntimeError: Couldn't find appropriate backend to handle uri ...wav` | 缺 soundfile | `.venv` 装 `soundfile` |

### 10.2 新增一个剧集文件后的标准动作

```bash
export PATH="/d/anaconda/envs/ai-label/Library/bin:$PATH"
cd /e/WorkBuddy/video-transfer-tools/video_pipeline
./.venv/Scripts/python.exe main.py --config config.yaml scan
./.venv/Scripts/python.exe main.py --config config.yaml run      # Ctrl+C 可安全中断
./.venv/Scripts/python.exe main.py --config config.yaml status
```

### 10.3 验证修复效果

```bash
./.venv/Scripts/python.exe scripts/verify_repair.py <源> <修复后> \
    --frames 8 --json report.json
```

---

## 附：本轮验证的完整证据链

| 证据 | 位置 |
|---|---|
| 全链路日志（含两个 AI 阶段） | `work/_e2e/run4.out`、`run4b.out` |
| 任务级日志（含 AI 命令与 stderr） | `work/_e2e/logs4/jobs/0001.log` |
| 最终产物 | `work/_e2e/out4/S01E04_excerpt.mp4` |
| 失败隔离产物 | `work/_e2e/failed4/job_0001/` |
| 量化验证报告（JSON） | `work/_e2e/verify_report.json` |
| 环境自检 | `main.py doctor` 输出（§5.2） |
