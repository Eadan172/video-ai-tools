# Video Pipeline 端到端功能测试汇总报告

> **历史归档说明（2026-09-26 补记）**：本报告记录的是「课程」批量测试那一次运行。
> 报告完成后，`input/课程` 的 37 个源视频、`output/` 的 37 个 mp4 与
> `_e2e_artifacts` 备份均已按用户指示清理，因此下文提到的这些文件与备份路径
> **已不存在**；文中的缺陷分析、环境结论与可复现步骤仍然有效。
> 当时两个 AI 阶段是被禁用后走降级路径的，后续已由 `AI_REPAIR_PLAN.md`
> 实现并跑通真实 AI 修复。

- 测试时间：2026-09-25 22:42:52 ~ 2026-09-26 01:13:54（耗时 151.0 分钟）
- 被测对象：`E:\WorkBuddy\video-transfer-tools\video_pipeline`
- 测试脚本：`scripts/e2e_batch_test.py`（未修改 pipeline 业务逻辑代码）
- 测试配置：`config.e2e.yaml`（仅禁用 video_repair / audio_repair，走 pipeline 内置降级路径）
- pipeline run 退出码：`0`

## 1. 环境配置

- 虚拟环境：`E:\WorkBuddy\video-transfer-tools\.venv`（Python 3.11.7，解释器 `E:\WorkBuddy\video-transfer-tools\.venv\Scripts\python.exe`，prefix `E:\WorkBuddy\video-transfer-tools\.venv`）
- Python 依赖（requirements.txt）：PyYAML>=6.0, psutil>=5.9, pytest>=7.4
- FFmpeg / FFprobe：`E:\WorkBuddy\video-transfer-tools\.tools\ffmpeg`
- 外部 AI 工具：REAL-Video-Enhancer 与 DeepFilterNet **均未安装**；REAL-Video-Enhancer 为 C++/TensorRT 工程，本环境无法通过 pip 获得，故本次测试按其内部降级路径（纯 FFmpeg 转码）执行。

```text
[OK]   Python >= 3.11  (3.11.7)
[OK]   FFmpeg  (E:\WorkBuddy\video-transfer-tools\.tools\ffmpeg\ffmpeg.EXE)
[OK]   FFprobe  (E:\WorkBuddy\video-transfer-tools\.tools\ffmpeg\ffprobe.EXE)
[WARN] REAL-Video-Enhancer  (realesrgan-video-enhancer)
[WARN] DeepFilterNet  (deepFilter)
[OK]   NVIDIA NVENC
[OK]   Intel QSV
[OK]   CPU libx265
[OK]   NVIDIA GPU (>=8GB VRAM)  (8 GB)
[OK]   RAM >= 16GB  (空闲 4.9 GB)
[OK]   磁盘可用空间  (125.3 GB)

缺少依赖/检查未通过：
  1. REAL-Video-Enhancer
  2. DeepFilterNet

配置路径：config.yaml（可调整 executable 路径或禁用对应功能）
提示：缺少 AI 工具时流水线仍可运行——将 video_repair.enabled / audio_repair.enabled 设为 false 即可退化为纯转码模式。
```

## 2. 关键指标

| 指标 | 数值 |
| --- | --- |
| 总视频文件数 | 37 |
| 成功（导出 mp4 在 output/） | 37 |
| 失败（源文件已移入 failed/） | 0 |
| 成功率 | 100.0% |
| 测试耗时 | 151.0 分钟 |
| 错误日志文件数 / 总大小 | 0 / 0.0 KB |

- output/：38 个文件
- failed/：1 个文件

## 3. 失败文件清单

无失败文件。

## 4. 错误分类统计

无错误记录。

## 5. 共性问题与系统性缺陷分析

本轮 37 个样本全部成功，因此无法从失败样本中统计"共性问题"；但结合**测试前置状态**与
**运行证据**，仍可得出两条关键结论：

- **测试前的实际状态本身就是最大的系统性问题**：本环境在测试开始前，`pipeline.db` 中
  37/37 个任务全部为 `FAILED_FINAL`，且失败原因完全一致
  （`REAL-Video-Enhancer 未安装（executable='realesrgan-video-enhancer'）`）。
  即：出厂默认配置在这台主机上必然导致 100% 失败率，与具体文件无关 —— 属于配置/环境层面的
  系统性缺陷，而非个例。
- **走对降级路径后表现稳定**：37/37 一次通过，`retry_count` 全为 0，编码后端 37/37 均成功
  使用 Intel QSV（`hevc_qsv`），**0 次后端降级、0 次外部命令失败、0 次 Python 异常/堆栈**，
  每轮结束 `work/current` 均被清空。说明在"纯转码"路径下，流水线的稳定性、失败隔离与
  资源约束设计是有效的。

以下为本次测试过程中由代码与运行证据确认的缺陷/薄弱点（未修改被测代码，仅记录）：

1. **默认配置与真实环境不匹配导致全量失败（本次测试前置状态）**：`config.yaml` 出厂默认 `video_repair.enabled: true` 且 `executable: realesrgan-video-enhancer`，而 `DependencyError` 被标记为不可重试，因此未安装该工具时**所有任务**都在 `REPAIR_VIDEO` 阶段直接 `FAILED_FINAL`。测试开始前数据库中 37/37 任务均为该状态。建议 `doctor` 前置校验或在检测不到 AI 工具时自动降级为纯转码。
2. **磁盘处于 20~30GB 区间时存在无进展死等**：`Scheduler._pick_next_job()` 在 `DiskState.CONTROLLED` 及更严重状态下，只挑选 `stage != NONE` 的任务；若队列中只剩从未开始（`DISCOVERED`，`stage = NONE`）的任务，则返回 `None`；而 `_has_pending_work()` 仍把 `DISCOVERED` 计为待处理，主循环于是永久 `sleep(poll_interval)` 轮询——既无进度也不会退出。本次因 E: 可用 129GB（NORMAL）未触发，但在设计容量 50GB 的主机上，磁盘滑落到 20~30GB 时极易命中。
3. **重试次数与文档不一致（off-by-one）**：`retry.max_attempts = 2` 的判定是 `retry_count < max_attempts - 1`，实际每个任务只会重试 **1** 次即进入 `FAILED_FINAL`，而 README 描述为“默认每个任务最多重试 2 次”。
4. **输出命名不满足“时间戳”要求**：`pipeline/filename.py::unique_output_path()` 对同名冲突使用 `_1`、`_2` 递增后缀，而非需求中的 `YYYYMMDDHHMMSS` 时间戳；同名检查也只看 output/ 目录内的 文件，不含跨目录语义。
5. **跨任务共享状态脆弱点**：`Scheduler._stage_export()` 把 `.partial` 路径写在实例属性 `self._partial_path` 上，`_stage_verify()` 再读取；断点续跑时若 `EXPORT` 被跳过，`VERIFY` 可能读到上一个任务遗留的值。当前因有 `Path(partial).exists()` 兜底而未产生错误结果，属隐患。
6. **目标格式校验过严**：`Verifier.verify_output()` 强制要求 `sample_rate == 48000` 且视频编码必须为 hevc/h265；源为 22050Hz 单声道 WMV，最终能否通过完全依赖 EXPORT 阶段 `-ar 48000` 一路参数不被改动，参数微调即会造成全量 VERIFY 失败（无按文件差异化容忍）。
7. **`main.py verify` 会把非视频文件纳入校验**：`cmd_verify()` 用 `out_dir.glob("*")` 取目录下全部文件，未按视频扩展名过滤，导致 `output/.gitkeep` 也被送去 ffprobe 并报错。本次实测输出为 `校验完成: 37 通过, 1 失败`，其中唯一的失败项就是 `.gitkeep`，37 个真实产物全部通过。不影响主流程，但会误导使用者判断产物质量。

最终产物分类一致性：本次全部 37 个样本均已归入 output/ 或 failed/，无遗留未分类文件；测试期间未出现进程崩溃或死循环（见 pipeline run 退出码与运行日志）。

## 6. 补充核验与覆盖边界

### 6.1 第二轮独立校验（被测系统自带的 verify 命令）

批量处理结束后，另行执行被测系统自带的校验命令复核产物：

```text
python main.py --config=config.e2e.yaml verify
→ 校验完成: 37 通过, 1 失败
```

唯一的失败项是 `output/.gitkeep`（见第 5 节缺陷 7），**37 个 mp4 产物全部通过校验**。

后置状态复核：

| 检查项 | 结果 |
| --- | --- |
| pipeline.db 任务状态 | `DONE` × 37（无 FAILED_FINAL / RETRY_PENDING） |
| output/ 中的 mp4 | 37 个，文件名与源文件同名（录像01.mp4 … 录像37.mp4） |
| failed/ 中的源文件 | 0 个 |
| input/ 源文件 | 37 个，全部保持原样未被修改（成功分支按要求只保留产物、不动源文件） |
| work/current 残留文件 | 0 个 |
| 编码后端 | 37/37 使用 Intel QSV（`hevc_qsv`） |
| 后端降级次数 | 0 |
| Python 异常 / Traceback / MemoryError | 0 |
| 重试次数 | 全部为 0 |

### 6.2 覆盖边界（本轮未真实执行的部分）

以下路径**未被本轮测试真实触发**，相关结论仅来自代码审查与测试前的失败态证据，
建议在具备条件时补测：

1. **`REPAIR_VIDEO` 的 AI 真实推理**：环境无法安装 REAL-Video-Enhancer，本轮走
   `video_repair.enabled=false` 的降级分支（FFmpeg 转码生成中间文件）。超分/去块/降噪、
   GPU OOM 自动降倍率重试等逻辑未被执行。
2. **`REPAIR_AUDIO` 的 AI 真实推理**：同理走直接抽取 WAV 的降级分支，DeepFilterNet 调用、
   输出重命名、原始 WAV 即时删除等逻辑未被执行。
3. **失败分支的运行时行为**：因 37/37 全部成功，`failed/` 归档、
   `YYYYMMDD_HHMMSS_error.log` 写入、10MB 自动轮转等逻辑在真实失败场景下未被触发
   （仅由测试前数据库中 37/37 `FAILED_FINAL` 提供了"失败隔离"的侧面证据）。
4. **磁盘压力路径**：E: 可用空间全程 ≥125GB，始终处于 `NORMAL`，
   `WAIT_DISK` / `EMERGENCY_CLEANUP` / `HALT` 以及第 5 节缺陷 2 描述的死等场景未被触发。
5. **断点续跑**：本轮一次跑完，未发生中断重启（该能力由项目自带 59 项单测中的
   `test_resume_after_interruption` 覆盖，本轮单测全部通过）。

## 7. 成功文件清单

1. 录像01.wmv → 已在 output/录像01.mp4
2. 录像02.wmv → 已在 output/录像02.mp4
3. 录像03.wmv → 已在 output/录像03.mp4
4. 录像04.wmv → 已在 output/录像04.mp4
5. 录像05.wmv → 已在 output/录像05.mp4
6. 录像06.wmv → 已在 output/录像06.mp4
7. 录像07.wmv → 已在 output/录像07.mp4
8. 录像08.wmv → 已在 output/录像08.mp4
9. 录像09.wmv → 已在 output/录像09.mp4
10. 录像10.wmv → 已在 output/录像10.mp4
11. 录像11.wmv → 已在 output/录像11.mp4
12. 录像12.wmv → 已在 output/录像12.mp4
13. 录像13.wmv → 已在 output/录像13.mp4
14. 录像14.wmv → 已在 output/录像14.mp4
15. 录像15.wmv → 已在 output/录像15.mp4
16. 录像16.wmv → 已在 output/录像16.mp4
17. 录像17.wmv → 已在 output/录像17.mp4
18. 录像18.wmv → 已在 output/录像18.mp4
19. 录像19.wmv → 已在 output/录像19.mp4
20. 录像20.wmv → 已在 output/录像20.mp4
21. 录像21.wmv → 已在 output/录像21.mp4
22. 录像22.wmv → 已在 output/录像22.mp4
23. 录像23.wmv → 已在 output/录像23.mp4
24. 录像24.wmv → 已在 output/录像24.mp4
25. 录像25.wmv → 已在 output/录像25.mp4
26. 录像26.wmv → 已在 output/录像26.mp4
27. 录像27.wmv → 已在 output/录像27.mp4
28. 录像28.wmv → 已在 output/录像28.mp4
29. 录像29.wmv → 已在 output/录像29.mp4
30. 录像30.wmv → 已在 output/录像30.mp4
31. 录像31.wmv → 已在 output/录像31.mp4
32. 录像32.wmv → 已在 output/录像32.mp4
33. 录像33.wmv → 已在 output/录像33.mp4
34. 录像34.wmv → 已在 output/录像34.mp4
35. 录像35.wmv → 已在 output/录像35.mp4
36. 录像36.wmv → 已在 output/录像36.mp4
37. 录像37.wmv → 已在 output/录像37.mp4

## 8. 附录：可追溯性

- pipeline 完整运行日志：`E:\WorkBuddy\video-transfer-tools\video_pipeline\logs\e2e_pipeline_20260925224252.out.log`
- 错误日志：（无）
- 单任务详细日志：`E:\WorkBuddy\video-transfer-tools\video_pipeline\logs\jobs\<job_id>.log`（含每次外部命令的 stdout/stderr）
- 输入与数据库备份：`E:\WorkBuddy\video-transfer-tools\_e2e_artifacts\artifacts_video_pipeline`
- 测试过程备注：
  - input 备份：38 个文件 → E:\WorkBuddy\video-transfer-tools\_e2e_artifacts\artifacts_video_pipeline\input_backup
