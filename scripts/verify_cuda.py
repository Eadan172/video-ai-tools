"""CUDA 版 PyTorch 安装验证。

用法：
    ./.venv-rve/Scripts/python.exe scripts/verify_cuda.py [--bench]

检查项：
  1. 解释器 / torch / torchvision 版本，torch 的编译期 CUDA 版本与 cuDNN 版本
  2. torch.cuda 是否可用；设备名、计算能力(sm)、显存
  3. **真实内核执行**：在 GPU 上跑一次 matmul，确认不只是 is_available() 为真
     （is_available() 只代表驱动/运行时能初始化，不代表能真正跑算子）
  4. --bench：GPU vs CPU 矩阵乘法吞吐对比，给出加速比
"""

from __future__ import annotations

import argparse
import sys
import time


def hr(title: str = "") -> None:
    if title:
        print(f"\n{title}")
        print("-" * 62)
    else:
        print("=" * 62)


def main() -> int:
    ap = argparse.ArgumentParser(description="CUDA 版 PyTorch 验证")
    ap.add_argument("--bench", action="store_true", help="追加 GPU/CPU 吞吐对比")
    ap.add_argument("--bench-size", type=int, default=4096,
                    help="基准矩阵边长（默认 4096）")
    args = ap.parse_args()

    import torch

    hr()
    print("CUDA 版 PyTorch 安装验证")
    hr()

    # ---------- 1. 版本信息 ---------- #
    print(f"{'Python':<22}{sys.version.split()[0]}  ({sys.executable})")
    print(f"{'PyTorch':<22}{torch.__version__}")
    print(f"{'编译期 CUDA':<22}{torch.version.cuda}")
    print(f"{'cuDNN':<22}{torch.backends.cudnn.version()}")
    try:
        import torchvision
        print(f"{'torchvision':<22}{torchvision.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"{'torchvision':<22}[未安装] {exc}")

    # ---------- 2. CUDA 可用性 ---------- #
    hr("CUDA 可用性")
    if not torch.cuda.is_available():
        print("[失败] torch.cuda.is_available() == False")
        print("  常见原因：")
        print("   * 装成了 CPU 版（torch.__version__ 不带 +cuXXX）")
        print("   * NVIDIA 驱动过旧，低于该 wheel 要求的 CUDA 大版本")
        print("   * 显卡不被该 CUDA 架构支持")
        return 1

    n = torch.cuda.device_count()
    print(f"[通过] 可见 GPU 数量: {n}")
    for i in range(n):
        props = torch.cuda.get_device_properties(i)
        print(f"   [{i}] {props.name}")
        print(f"       计算能力 sm_{props.major}{props.minor}"
              f" | 显存 {props.total_memory / 1024 ** 3:.2f} GiB"
              f" | 多处理器 {props.multi_processor_count}")

    # ---------- 3. 真实内核执行 ---------- #
    hr("真实内核执行校验")
    try:
        dev = torch.device("cuda:0")
        a = torch.randn(1024, 1024, device=dev)
        b = torch.randn(1024, 1024, device=dev)
        c = a @ b
        torch.cuda.synchronize()
        # 结果回传 CPU 校验，确保不是惰性图
        assert c.shape == (1024, 1024), "结果形状异常"
        assert torch.isfinite(c.sum()).item(), "结果含 nan/inf"
        print(f"[通过] GPU matmul 执行成功，结果均值 {c.mean().item():+.6f}")
        print(f"       显存占用 {torch.cuda.memory_allocated() / 1024 ** 2:.1f} MiB"
              f" / 峰值 {torch.cuda.max_memory_allocated() / 1024 ** 2:.1f} MiB")
        del a, b, c
        torch.cuda.empty_cache()
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] GPU 算子执行异常: {type(exc).__name__}: {exc}")
        return 1

    # ---------- 4. 吞吐对比（可选） ---------- #
    if args.bench:
        hr(f"吞吐对比（{args.bench_size}x{args.bench_size} 矩阵乘法）")

        def bench(device: str, iters: int = 10) -> float:
            d = torch.device(device)
            x = torch.randn(args.bench_size, args.bench_size, device=d)
            y = torch.randn(args.bench_size, args.bench_size, device=d)
            for _ in range(3):          # 预热
                x @ y
            if d.type == "cuda":
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            for _ in range(iters):
                x @ y
            if d.type == "cuda":
                torch.cuda.synchronize()
            dt = (time.perf_counter() - t0) / iters
            flops = 2 * args.bench_size ** 3
            return flops / dt

        g = bench("cuda")
        c = bench("cpu")
        print(f"{'GPU (cuda:0)':<20}{g / 1e12:>10.2f} TFLOP/s")
        print(f"{'CPU':<20}{c / 1e12:>10.2f} TFLOP/s")
        print(f"{'加速比':<20}{g / c:>10.1f}x")

    hr()
    print("[结论] CUDA 版 PyTorch 安装正确，可用于 video_repair.device=cuda")
    print()
    print("下一步：把 config.yaml 的 video_repair.device 改为 cuda，并视显存调整 tile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
