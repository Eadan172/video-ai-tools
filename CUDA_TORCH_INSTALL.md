# CUDA 版 PyTorch 安装方案（video_pipeline / REAL-Video-Enhancer）

> 目标：为画质 AI（REAL-Video-Enhancer）换上 CUDA 版 PyTorch，
> 把视频修复从「CPU 数十小时」压缩到可用量级。
>
> **状态：已安装并实测通过。** 本机 RVE 视频修复 **首帧到出片 16.2 秒**（CPU 需 365~552 秒）。

---

## 0. 结果速览

| 项 | 值 |
|---|---|
| 安装位置 | **项目目录内** `video_pipeline/.venv-rve`（虚拟环境，随项目走） |
| 版本组合 | **torch 2.7.0+cu128 / torchvision 0.22.0+cu128 / cp311 / win_amd64** |
| CUDA 运行时 | 12.8（wheel 内置，**无需另装 CUDA Toolkit**） |
| cuDNN | 90701（随 wheel） |
| 验证结论 | `torch.cuda.is_available() == True`，GPU 真实内核执行通过 |
| GPU 算力 | 6.10 TFLOP/s（fp32，4096² matmul） vs CPU 0.51 TFLOP/s → **12.1×** |
| 端到端提速 | RVE 视频 AI：**365~552 s → 16.2 s（约 22~34×）** |
| 全链路耗时 | 2 秒样本 **29 秒**跑完 7 个阶段 |

---

## 1. 适配的 CUDA 版本与 Python 版本

### 1.1 本机约束（决定了能选哪些构建）

```
GPU            : NVIDIA GeForce RTX 4060 Laptop GPU
计算能力        : sm_89（Ada Lovelace）
显存           : 8188 MiB
驱动           : 572.83
驱动支持的最高 CUDA : 12.8
Python         : 3.11.7 → wheel 标签 cp311
```

### 1.2 选型结论与依据

| 维度 | 选择 | 依据 |
|---|---|---|
| torch 版本 | **2.7.0** | 与 RVE 官方 `tools/backend/requirements.txt` 的 pin 完全一致（`torch==2.7.0` / `torchvision==0.22.0`），避免与 RVE 推理代码产生 API 偏差 |
| CUDA 构建 | **cu128** | 驱动 572.83 恰好支持到 CUDA 12.8；cu128 是 2.7.0 可用的最高档，对 Ada(sm_89) 支持最好 |
| Python | **3.11（cp311）** | 项目 venv 由 `D:\anaconda\python.exe 3.11.7` 创建；**torch 的 wheel 与 Python 次版本强绑定**（cp311 的 wheel 不能给 3.12/3.13 用） |

### 1.3 驱动降级时的备选

| 你的驱动版本 | 应改用的 `-CudaIndex` | 说明 |
|---|---|---|
| ≥ 570 | `cu128`（默认） | 本机情形 |
| ≥ 560 | `cu126` | 12.6 runtime |
| ≥ 452 | `cu118` | 老卡（GTX 10/16 系）兼容最好 |

> 判断口诀：**驱动版本对 CUDA 是向下兼容的**。装了比驱动更新的 CUDA 构建会报
> `CUDA driver version is insufficient for CUDA runtime version`；
> 装了更旧的则浪费性能，但能跑。

### 1.4 不需要装 CUDA Toolkit

PyTorch 的 CUDA wheel **自带** cudart / cuBLAS / cuDNN / cuFFT 等全部运行时库
（这也是它单个 3.34 GB 的原因）。系统里只需有 **NVIDIA 显卡驱动**即可。
不要为了跑 torch 去装 `CUDA Toolkit`——那会白白占 5+ GB。
（只有需要 `nvcc` 自己编译 CUDA 扩展时才需要 Toolkit。）

---

## 2. 安装位置策略与迁移注意事项

### 2.1 三类"位置"要分开看

很多人只考虑「虚拟环境装哪」，其实有 **3 个落点**，默认都往 C 盘跑：

| 落点 | 默认位置 | 本方案落点 | 体积 |
|---|---|---|---|
| ① 虚拟环境本体 | 就地创建 | **项目内** `video_pipeline/.venv-rve` | 5.9 GB |
| ② pip 下载缓存 | `C:\Users\<用户>\AppData\Local\pip\Cache` | 项目同级的 `.pip-cache` | 3.4 GB |
| ③ 下载中临时目录 | `%TEMP%`（**在 C 盘**） | 同上 `.pip-cache\tmp` | 峰值 3.4 GB |

> ⚠️ **最容易踩的坑**：只设了 `PIP_CACHE_DIR` 是不够的。
> pip 会先把 3.3 GB 的 wheel **完整下到 `%TEMP%`**，装完才挪进缓存目录。
> 本机 `%TEMP%` 在 C 盘 —— 也就是说单靠 `PIP_CACHE_DIR` 仍会临时吃掉 C 盘 3.4 GB。
> 必须同时改 `TEMP` / `TMP`。

### 2.2 为什么"项目目录优先"是可行的

虚拟环境建在项目内 → 环境与代码同生命周期，不会散落到系统盘；
`config.yaml` 里用 `./.venv-rve/Scripts/python.exe` 相对路径即可引用（见 §4）。

### 2.3 ⚠️ 但「下载项目即可直接使用」要打个折 —— venv 不可搬迁

这是本项目最需要注意的一点：

1. **venv 目录不能直接拷贝到别的机器/路径**。
   - `pyvenv.cfg` 里写死了 base 解释器：本机是 `home = D:\anaconda`；
   - `Scripts\*.exe`（python.exe / pip.exe…）是硬编码路径的启动器；
   - `Lib\site-packages` 里有大量绝对路径记录（`.dist-info/RECORD`）。
   - 换个路径后典型症状：`Fatal error in launcher: Unable to create process using '...'`。
2. **venv 依赖 base 解释器**。本项目 venv 的 base 是 `D:\anaconda\python.exe`；
   目标机器若没有那个 anaconda，venv 直接失效。
3. **CUDA wheel 与"驱动 + Python 次版本"双重绑定**，不能跨驱动大版本迁移。

**正确的"下载即用"姿势**：随项目交付 **代码 + 安装脚本**，让脚本在用户自己的路径上
**重建** 环境：

```powershell
cd video_pipeline
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda
```

脚本会：在**当前项目内**重建 `.venv` / `.venv-rve`，把缓存/临时目录重定向到
项目同级的 `.pip-cache`，然后下载 torch 与 RVE 模型。全程不写 C 盘业务数据。

> 若目标机器上没有 anaconda，需要先改 `scripts/setup_ai_tools.ps1` 里
> `Get-Python` 函数的候选列表，指向目标机的 Python 3.11。

### 2.4 迁移/依赖使用注意事项清单

| 事项 | 说明 |
|---|---|
| 拷贝项目时排除 | `.venv/`、`.venv-rve/`、`.pip-cache/`、`work/`、`*.db` —— 都是可重建的 |
| 模型文件 | `tools/models/*.pth` **可随项目走**（约 32 MB，建议一起交付） |
| RVE 后端源码 | `tools/backend/`（约 520 KB）建议一起交付，省一次 GitHub 下载 |
| DeepFilterNet 模型 | 默认缓存在 **C 盘** `%LOCALAPPDATA%\DeepFilterNet\DeepFilterNet\Cache`（约 8.7 MB），换机器会自动重下；可用 `--model-base-dir` 改到项目内 |
| 多机共用 | 把 `PIP_CACHE_DIR` 指向共享盘，可复用 3.4 GB 的 wheel 缓存 |
| 离线安装 | 见 §3.3，把 wheel 拷到目标机直接 `pip install` 本地文件 |
| 显存 | 8 GB 下 `tile: 0`（整帧）可用；若 OOM 改 `512` 或 `256` |

---

## 3. 安装方式

### 3.1 方式 A：一键脚本（推荐）

```powershell
cd video_pipeline

# 生产：CUDA 12.8（默认）
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda

# 老驱动
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda -CudaIndex cu126

# 自定义缓存位置
powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda -PipCacheDir D:\pip-cache
```

脚本安装 CUDA 版 torch 的逻辑：**先试官方源，失败/过慢自动切国内镜像**。

### 3.2 方式 B：手动（官方源）

```powershell
cd video_pipeline
python -m venv .venv-rve
.\.venv-rve\Scripts\pip install opencv-python-headless requests einops safetensors `
    tqdm sympy typing_extensions packaging pillow "numpy==2.2.2"

# CUDA 版 torch（注意：缓存与临时目录都移出 C 盘）
$env:PIP_CACHE_DIR = "D:\pip-cache"
$env:TEMP = "D:\pip-cache\tmp"; $env:TMP = "D:\pip-cache\tmp"
.\.venv-rve\Scripts\pip install --no-deps `
    --index-url https://download.pytorch.org/whl/cu128 `
    torch==2.7.0 torchvision==0.22.0

# torch 的 4 个运行时依赖（--no-deps 时需补齐）
.\.venv-rve\Scripts\pip install filelock fsspec jinja2 networkx
```

> `--no-deps` 是为了避免 pip 从 PyPI 反复拉依赖、也避免把 torch 换成 CPU 版。
> 代价是必须手动补 `filelock / fsspec / jinja2 / networkx`，
> 用 `.\.venv-rve\Scripts\pip check` 可以查出来。

### 3.3 方式 C：国内镜像 / 离线安装（本机实测走的就是这条）

国内直连 `download.pytorch.org` 实测只有 **0.4~0.95 MB/s**，3.3 GB 要 1~2 小时。
本项目自带多镜像测速 + 断点续传下载器：

```bash
# 1) 下载到指定目录（自动在 上海交大 / 官方 / 阿里云 之间测速选最快）
./.venv-rve/Scripts/python.exe scripts/fetch_cuda_wheels.py --dest D:/pip-wheels

# 其他 CUDA 档位
./.venv-rve/Scripts/python.exe scripts/fetch_cuda_wheels.py --dest D:/pip-wheels --cuda-index cu126

# 2) 从本地 wheel 安装
./.venv-rve/Scripts/python.exe -m pip install --no-deps \
    "D:/pip-wheels/torch-2.7.0+cu128-cp311-cp311-win_amd64.whl" \
    "D:/pip-wheels/torchvision-0.22.0+cu128-cp311-cp311-win_amd64.whl"
```

实测速率对比（本机同一时段）：

| 源 | 速率 |
|---|---|
| 上海交大 `mirror.sjtu.edu.cn/pytorch-wheels` | **5~26 MB/s** ✅ |
| 官方 `download.pytorch.org` | 0.4~0.95 MB/s |
| 阿里云 `mirrors.aliyun.com/pytorch-wheels` | 0.2~0.25 MB/s |
| 清华 `mirrors.tuna.tsinghua.edu.cn/pytorch-wheels` | 该档位不存在（404） |

> 为什么不用 `pip install --index-url <镜像>`：国内这些镜像多为**普通 HTTP 目录列表**，
> 不是 PEP 503 索引，pip 解析不了（报 `No matching distribution found`）。
> 因此只能直接抓 `.whl` 再本地安装 —— 这正是 `fetch_cuda_wheels.py` 的用途。
> 抓完脚本会与官方 `Content-Length` 逐字节比对，保证完整性。

---

## 4. 验证方法

### 4.1 快速自检（已集成进流水线）

```bash
./.venv/Scripts/python.exe main.py --config config.yaml doctor
```

新增的 CUDA 检查项会直接指出**配置与环境的错配**：

```
[OK]   CUDA 版 torch（device=cuda）  (torch 2.7.0+cu128, CUDA 12.8)
```

把 `video_repair.device` 设成 `cuda` 但环境里是 CPU 版 torch 时：

```
[WARN] CUDA 版 torch（device=cuda）  (torch 2.5.1+cpu（CPU 版） ← 需装 CUDA 版: setup_ai_tools.ps1 -Cuda)
```

### 4.2 完整验证（含算力基准）

```bash
./.venv-rve/Scripts/python.exe scripts/verify_cuda.py --bench
```

实测输出：

```
Python                3.11.7
PyTorch               2.7.0+cu128
编译期 CUDA              12.8
cuDNN                 90701
torchvision           0.22.0+cu128

CUDA 可用性
[通过] 可见 GPU 数量: 1
   [0] NVIDIA GeForce RTX 4060 Laptop GPU
       计算能力 sm_89 | 显存 8.00 GiB | 多处理器 24

真实内核执行校验
[通过] GPU matmul 执行成功，结果均值 +0.002024
       显存占用 20.1 MiB / 峰值 20.1 MiB

吞吐对比（4096x4096 矩阵乘法）
GPU (cuda:0)              6.10 TFLOP/s
CPU                       0.51 TFLOP/s
加速比                       12.1x
```

> 脚本特意做了**真实内核执行**校验：`is_available()` 为真只说明驱动/运行时能初始化，
> 不代表算子能真正跑起来。只有真的算完一次 matmul 并回传结果，才能确认可用。

### 4.3 端到端验证（最有说服力）

改动 `config.yaml`：

```yaml
video_repair:
  device: "cuda"
  tile: 0          # 整帧推理；显存不足报 OOM 时改 512 / 256
```

然后跑一个小样本，看日志里是否出现 `Using Device: NVIDIA GeForce RTX 4060 Laptop GPU`：

```
20:22:19  RVE 调用: ... --device cuda --backend pytorch --tilesize 0 ...
20:22:35  EXIT rc=0 16.2s                              ← GPU
20:22:35  REPAIR_VIDEO 完成 → video_ai.mp4
20:22:46  REPAIR_AUDIO 完成 → audio_clean.wav
20:22:47  TRANSCODE 完成 → transcoded.mp4
20:22:48  VERIFY 通过 → out_gpu\S01E04_excerpt.mp4
20:22:48  === 完成 ===
```

产物与 CPU 版**质量一致**（712×400 / 2.00×，锐度对双三次基线 +271.6%，
噪声底 −59.6 dB），只是快了 22~34 倍。

---

## 5. 实测性能对比

| 阶段 | CPU（torch 2.5.1+cpu） | GPU（torch 2.7.0+cu128） | 提速 |
|---|---:|---:|---:|
| REPAIR_VIDEO（RVE，2 s 视频） | 365 ~ 552 s | **16.2 s** | **22 ~ 34×** |
| REPAIR_AUDIO（DeepFilterNet） | 2.9 ~ 4.0 s | 10.3 s | 持平（本就 CPU） |
| 全链路（含转码/合流/校验） | 约 10 min | **29 s** | ≈ 20× |

按此推算，**整集 4088 秒的剧集**：CPU 需数十小时 → GPU 约 **1~2 小时**量级
（实际取决于分辨率与模型；本样本仅 356×200，1080p 会更慢但仍可接受）。

---

## 6. 本次改动清单

| 文件 | 说明 |
|---|---|
| `scripts/fetch_cuda_wheels.py` | 🆕 多镜像测速 + 断点续传的 CUDA wheel 下载器 |
| `scripts/verify_cuda.py` | 🆕 CUDA 安装验证（版本 / 设备 / 真实内核执行 / 算力基准） |
| `scripts/setup_ai_tools.ps1` | ♻️ 新增 `-CudaIndex` / `-PipCacheDir`；缓存与临时目录移出 C 盘；CUDA 段改为 cu128 + torch 2.7.0，官方源失败自动切镜像 |
| `main.py` | ♻️ `doctor` 新增「CUDA 版 torch 与 device 是否匹配」检查项 |
| `config.yaml` | ♻️ `device: cuda`、`tile: 0` |

---

## 7. 常见报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `AssertionError: Torch not compiled with CUDA enabled` | 装的是 CPU 版 torch | 重装 `+cuXXX` 版本 |
| `CUDA driver version is insufficient for CUDA runtime version` | wheel 的 CUDA 比驱动新 | 换更低档 `-CudaIndex`（cu126 / cu118），或升级显卡驱动 |
| `torch.cuda.is_available() == False` 但装了 CUDA 版 | 驱动异常 / 被 WDDM 独占 | 跑 `nvidia-smi` 确认驱动可用；重启 |
| `CUDA out of memory` | 显存不足 | `video_repair.tile` 改 512 / 256；或减小输入分辨率 |
| `WinError 5 拒绝访问 ... INSTALLER...tmp` | 杀软实时扫描占住刚写入的文件（Windows 常见竞态） | 重试即可（脚本已内置 3 次重试）；也可临时把 `.venv-rve` 加入杀软排除项 |
| `Fatal error in launcher: Unable to create process using ...` | 把 venv 拷到了别的路径 | venv 不可搬迁，重跑安装脚本重建 |
| `ModuleNotFoundError: No module named 'filelock'` | `--no-deps` 装 torch 后漏补依赖 | `pip install filelock fsspec jinja2 networkx` |
