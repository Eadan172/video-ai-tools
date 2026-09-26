# Video Pipeline — 单机无人值守批量视频处理系统

面向 50+ 网课视频的批量「修复 → 转格式 → 导出」流水线，为以下硬件基准设计：

| 资源 | 基准 | 角色 |
| --- | --- | --- |
| CPU | 22 核 | FFmpeg 辅助 / 调度 / DeepFilterNet 音频降噪 |
| RAM | 16GB | 严格限制并发，>80% 禁止启动新阶段 |
| GPU 1 | RTX 4060 8GB | 唯一视频 AI 推理卡（REAL-Video-Enhancer / TensorRT） |
| GPU 2 | Intel Arc 8GB | 优先 QSV 硬件编码 |
| 磁盘 | **50GB 可用** | **第一优先级调度约束** |

核心原则：**稳定性 > 吞吐量**。同一时间只有一个视频进入完整工作区
（`work/current/`），断点续跑、失败隔离、磁盘保护、原子输出。

修复能力现状（本轮已实测跑通）：

| 阶段 | 实现 | 状态 |
| --- | --- | --- |
| 画质 AI | REAL-Video-Enhancer 2.4.1 后端 CLI（去压缩伪影 + 超分） | ✅ 可用（CUDA） |
| 音质 AI | DeepFilterNet 0.5.6（语音降噪） | ✅ 可用（CPU，约 10× 实时） |

---

## 快速开始（双击即用）

**双击 `run.bat`** 即可，脚本会自动完成：定位/创建虚拟环境 → 安装依赖 →
定位 FFmpeg（仓库内 `.tools\ffmpeg` → 上级目录 → PATH → 自动下载）→
检查 AI 组件（缺失则自动降级为纯转码）→ 交互选择输出格式与处理范围 →
执行 `scan` + `run`。

命令行用法（参数会被透传给流水线）：

```bat
run.bat                                 双击：交互式，默认 mp4，处理 input\ 全部
run.bat --format mkv                    输出 mkv
run.bat --only 剧集                     只处理 input\剧集
run.bat --no-prompt --format mp4        无人值守，不询问
run.bat --no-ai                         跳过 AI 修复，只转码（AI 组件缺失时的兜底）
run.bat --dry-run                       只做环境检查，不处理
run.bat --selftest                      跑一遍 59 项自测
```

## 输出目录与格式

**输出目录镜像输入子目录**，不同来源互不混淆：

```text
input/剧集/S01E04.mp4   →   output/剧集/S01E04.mp4
input/课程/录像01.wmv   →   output/课程/录像01.mp4
input/xxx.mp4           →   output/xxx.mp4
```

**输出格式可自定义**（默认 mp4）。容器决定编码组合，避免产出不兼容封装：

| 格式 | 视频编码 | 音频编码 | 说明 |
| --- | --- | --- | --- |
| `mp4`（默认） | H.265 | AAC 320k | 兼容性最好 |
| `mkv` | H.265 | AAC 320k | 无损容器，支持多音轨 |
| `mov` | H.265 | AAC 320k | 苹果生态 |
| `webm` | VP9 | Opus 256k | 网页播放（Opus 上限 256k） |
| `avi` | H.264 | MP3 320k | 老设备兼容 |

两种指定方式等价：CLI `--format mkv`，或改 `config.yaml` 的 `output.container`。

---

## 目录结构

```text
video_pipeline/
├── run.bat                 # ★ 双击运行入口（ASCII-only，编码安全）
├── main.py                 # CLI 入口
├── config.yaml             # 全部可调参数
├── requirements.txt
├── pipeline/               # 业务逻辑（不直接拼命令行）
│   ├── scheduler.py        # 资源感知调度器（核心）
│   ├── state_machine.py    # 状态机定义
│   ├── database.py         # SQLite 持久化
│   ├── disk_manager.py     # 磁盘空间调度约束
│   ├── resources.py        # RAM/CPU/GPU 监控
│   ├── scanner.py          # input 扫描（支持 --only 子目录限定）
│   ├── verifier.py         # 输出完整性校验 + 智能跳过转码
│   ├── cleanup.py          # 临时文件清理（绝不碰 input/output）
│   ├── filename.py         # Windows 安全文件名
│   ├── runner.py           # subprocess 封装（禁 shell=True）
│   └── logger.py           # 分层日志
├── adapters/               # 第三方命令适配层（CLI 差异隔离在这里）
│   ├── ffprobe.py
│   ├── ffmpeg.py           # QSV→NVENC→CPU 降级链；容器/编码映射
│   ├── real_video_enhancer.py   # 驱动 RVE 后端 CLI（绕开 GUI）
│   ├── deepfilternet.py    # DeepFilterNet / 预留 ClearerVoice
│   └── _toolpath.py        # 绝对路径 + PATH 均可识别的工具探测
├── profiles/selector.py    # light / course_720 / legacy 自动策略
├── tests/                  # pytest（59 项，含真实 FFmpeg 端到端）
├── tools/                  # AI 组件（体积大故不进版本库，见「安装」）
│   ├── df_enhance_cli.py   # DeepFilterNet 进程内封装（num_workers=0）
│   ├── backend/            # RVE 2.4.1 后端源码
│   └── models/             # 超分 / 压缩修复模型
├── scripts/
│   ├── bootstrap.ps1       # ★ 一键运行引导（被 run.bat 调用）
│   ├── setup_ai_tools.ps1  # 幂等安装两个 AI 组件
│   ├── verify_repair.py    # 画质/音质量化验证
│   ├── verify_cuda.py      # CUDA 环境自检与跑分
│   ├── e2e_batch_test.py   # 端到端批量测试 + 汇总报告
│   └── install.ps1 / run.ps1 / doctor.ps1
├── .venv/                  # DeepFilterNet 环境（numpy<2）
├── .venv-rve/              # RVE 环境（numpy 2.x + torch）
└── .tools/ffmpeg/          # FFmpeg / FFprobe 静态构建
```

## 安装

**推荐：双击 `run.bat`**，它会自动完成下面所有步骤（只需机器上有 Python 3.11+）。

手动安装等价步骤：

```bash
# 1) 基础依赖
pip install -r requirements.txt

# 2) FFmpeg / FFprobe（必需）：放到 .tools\ffmpeg\ 或加进 PATH
#    也可由 run.bat 自动下载

# 3) 两个 AI 组件（可选，缺失时仍可纯转码运行）
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda
```

外部工具依赖：

| 工具 | 用途 | 必需性 |
| --- | --- | --- |
| FFmpeg / FFprobe | 解码、转码、合流、探测 | **必需** |
| REAL-Video-Enhancer | 视频 AI 修复（RTX 4060） | 可选；缺失时 `--no-ai` 降级为纯转码 |
| DeepFilterNet | CPU 音频降噪 | 可选；同上 |

> 两个 AI 组件必须装在**两个独立虚拟环境**里：RVE 需要 `numpy 2.x`，
> DeepFilterNet 需要 `numpy < 2`，主版本冲突无法调和。`config.yaml` 里
> 各自的 `executable` 指向对应解释器，互不干扰。

## 配置

所有参数见 `config.yaml`，关键项：

```yaml
disk:
  safe_start_gb: 30        # >= 30GB 才启动新任务
  pause_new_jobs_gb: 20    # 20~30GB 只继续当前任务
  emergency_stop_gb: 10    # < 10GB 停止流水线
video_repair:
  executable: "..."        # 按本机 REAL-Video-Enhancer 实际 CLI 调整
  args_template: [...]     # 以 <tool> --help 实际输出为准
output:
  container: mp4
  video_codec: hevc        # h264 | hevc | av1
  audio_codec: aac
  audio_bitrate: 320k
```

## 首次检查

```bash
python main.py doctor
```

输出示例：

```text
[OK]   FFmpeg
[OK]   FFprobe
[WARN] REAL-Video-Enhancer  (未安装)
[OK]   NVIDIA NVENC
[OK]   Intel QSV
[OK]   Disk free: 47.3 GB
```

缺依赖不会崩溃，会列出缺失项和配置提示。

## 使用流程

```bash
python main.py scan      # 1. 扫描 input/ 建立任务队列
python main.py run       # 2. 无人值守运行
python main.py status    # 3. 随时查看状态
```

全局参数（写在子命令**之前**）：

| 参数 | 作用 |
| --- | --- |
| `--config <文件>` | 指定配置文件（默认 `config.yaml`） |
| `--format <格式>` | 输出格式：`mp4`(默认) / `mkv` / `mov` / `webm` / `avi` |
| `--only <子目录>` | 只处理 input/ 下的指定子目录（可重复） |
| `--no-ai` | 跳过两个 AI 修复阶段，只做转码导出 |

例：

```bash
./.venv/Scripts/python.exe main.py --format mkv --only 剧集 scan
./.venv/Scripts/python.exe main.py --format mkv --only 剧集 run
```

`status` 输出示例：

```text
Video Pipeline
===============================
Total Jobs       : 57
Done             : 42
Running          : 1
Waiting          : 12
Retry Pending    : 1
Failed Final     : 1

Disk Free        : 27.4 GB
RAM Usage        : 63%
RTX VRAM         : 5.8 / 8 GB
Intel QSV        : READY

Current Job      : lesson_043.wmv
Stage            : REPAIR_VIDEO
```

## 暂停与恢复（断点续跑）

- `Ctrl+C` 安全中断：状态保存在 SQLite（`pipeline.db`），不丢任务。
- 重启电脑后直接 `python main.py run` 即可继续。
- 续跑粒度是**阶段**：例如 `REPAIR_VIDEO`/`REPAIR_AUDIO`/`TRANSCODE`
  已完成而 `EXPORT` 失败，重跑时只会重做 `EXPORT`。
- 续跑判断依据 = SQLite 阶段记录 **+ 产物文件真实存在**，不盲信数据库。

## 失败重试

- `retry.max_attempts: 2` = **最多尝试 2 次**（首次 + 1 次重试）；
  重试判定为 `retry_count < max_attempts - 1`，第 2 次失败即 `FAILED_FINAL`。
- GPU OOM → 自动降低超分倍率再试。
- QSV 失败 → 自动降级 NVENC → 再降级 CPU libx265。
- 源文件损坏 → 直接 `FAILED_FINAL`（不浪费重试）。
- 手动给失败任务一次机会：`python main.py retry`

## 清理

```bash
python main.py cleanup   # 清理 work/ 下所有 .tmp/.partial/.wav 临时文件
python main.py verify    # 用 ffprobe 校验 output/ 所有最终文件
```

**红线**：任何自动化流程都不会删除或修改 `input/` 源文件与 `output/`
已验证文件。

## 磁盘空间策略

```text
50GB 可用磁盘
 ├─ ≥30GB   正常：允许启动新任务
 ├─ 20~30GB 受控：只继续当前任务
 ├─ 15~20GB 保守：暂停新任务
 ├─ 10~15GB EMERGENCY_CLEANUP：自动清理临时文件
 └─ <10GB   HALT：停止整条流水线
```

启动每个任务前还会做**单文件空间预算**：

```text
需求 ≈ 源大小×1.5(视频临时) + 源大小×0.2(音频临时) + 源大小×1.2(输出) + 8GB(余量)
```

不足则任务进入 `WAIT_DISK`，等空间恢复后自动继续。

## 处理流程（单个视频）

```text
ffprobe 校验 + 选择 profile（light/course_720/legacy）
  → RTX 4060 视频 AI 修复（RVE：1x 压缩伪影修复 + 2x 超分，默认不插帧）
  → CPU DeepFilterNet 音频降噪（完成即删原始 WAV）
  → 智能判断：已是目标格式则跳过转码，否则 Intel QSV/NVENC/CPU 转码
  → FFmpeg 合流（-c:v copy，视频绝不二次编码）
  → ffprobe 校验 → 原子重命名（.partial → .mp4）
  → 输出到 output/<与 input 同名的子目录>/
  → 清空 work/current → 下一个视频
```

## 性能实测与已知限制

在 RTX 4060 Laptop 8GB + 16GB RAM 上实测（源 712×400 / 25fps / 68 分钟的剧集）：

| 阶段 | 实测吞吐 | 整集(4088s)外推 |
| --- | --- | --- |
| 画质 AI：1x 压缩修复 + 2x 超分 | ~1.9 fps | **约 14.8 小时** |
| 画质 AI：仅 2x 超分（关掉压缩修复模型） | ~18.9 fps | 约 1.5 小时 |
| 音质 AI：DeepFilterNet | ~10× 实时 | 约 7 分钟 |

结论与建议：

- **瓶颈是压缩修复模型（RealPLKSR），比超分模型慢约 10 倍**。若只追求分辨率与锐度，
  可把 `video_repair.models.decompress` 设为 `null`（或 `deblock: false`），
  整集从 ~14.8h 降到 ~1.5h。
- `video_repair.timeout_seconds` **必须大于实际耗时**（现为 90000s=25h）。
  早期配置为 12h，会在跑到约 80% 时被当成超时杀掉。
- CPU 推理（`device: cpu`）比 CUDA 慢 20~50×，仅适合做功能验证：
  2 秒片段 CPU 需 365~552s。
- 编解码后端把结果编码成 `libx264` 再由流水线用 QSV 转 HEVC，
  比让 RVE 自己编 libx265 更快（libx265 在 CPU 上是主要瓶颈）。

## 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `FAILED_FINAL: ffprobe 无法读取` | 源文件损坏 | 换源文件后 `python main.py retry` |
| `GPU 显存不足` | 超分倍率过高 | 程序已自动降倍率重试；仍失败则在 config 调低 |
| 任务长时间 `WAIT_DISK` | 磁盘低于阈值 | 清理磁盘或将 output 转移到外部存储 |
| `编码后端 hevc_qsv 失败` | QSV 驱动/硬件不可用 | 自动降级 NVENC/CPU，无需干预 |
| `REAL-Video-Enhancer 未安装` | AI 工具缺失 | 配置 executable 路径，或暂时 `enabled: false` |

## GPU 检查

```bash
nvidia-smi              # 应看到 RTX 4060，VRAM 8192MB
python main.py doctor   # 会检查 VRAM 与 NVENC
```

## QSV 检查

```bash
ffmpeg -encoders | findstr qsv   # 应有 hevc_qsv
python main.py doctor
```

注意：`ffmpeg -encoders` 列出 `hevc_qsv` ≠ 硬件可用（可能只是编译了
libmfx）。本系统在**运行失败时也会自动降级**，不受误报影响。

## 错误码 / 异常分类

| 异常 | 含义 | 是否重试 |
| --- | --- | --- |
| `ProbeError` | 源文件损坏/无法解析 | 否 → FAILED_FINAL |
| `DependencyError` | 外部工具缺失 | 否 → FAILED_FINAL |
| `GpuOutOfMemoryError` | 显存不足 | 是（降参数后） |
| `ExternalToolError` | 外部命令非零退出 | 是 |
| `DiskSpaceError` / `EmergencyStopError` | 磁盘不足 | 任务 WAIT_DISK / 流水线暂停 |
| `VerificationError` | 输出未通过校验 | 是 |
| `ConfigError` | 配置非法 | 否 |

## 测试

```bash
python -m pytest tests/ -v
```

59 项测试：状态机迁移、SQLite 断点恢复、磁盘阈值、文件名清洗、
校验器、profile 选择，以及基于真实 FFmpeg 的端到端测试
（完整跑通 scan → run → DONE → work 清空）。
