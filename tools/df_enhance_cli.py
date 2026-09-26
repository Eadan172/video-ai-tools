"""DeepFilterNet CLI 兼容封装（无人值守环境适配）。

背景
----
上游 `deepFilter` 命令在 `df/enhance.py` 里使用:

    loader = DataLoader(ds, num_workers=2, pin_memory=True)

`num_workers>0` 会让 PyTorch 启动子进程来预取音频。在
受限/无人值守环境（沙箱、受限服务账户、被安全策略拦截进程创建的机器）中，
子进程可能启动失败并报:

    RuntimeError: DataLoader worker (pid(s) NNNN) exited unexpectedly

本封装在**主进程内**以 `num_workers=0` 运行同一套推理逻辑，行为等价
（只是少了预取并行），从而避免依赖子进程。**不修改第三方包源码**。

用法（与 deepFilter 完全一致）:

    python df_enhance_cli.py <input.wav> [--output-dir DIR] [更多 deepFilter 参数...]
"""

from __future__ import annotations

import sys


def main() -> int:
    import sys

    import df  # noqa: F401  触发 df.enhance 子模块导入
    # 注意：`import df.enhance as _e` 拿到的是被 df/__init__.py 导出的**函数**
    # `enhance`，而非子模块；这里从 sys.modules 取真正的模块对象。
    _e = sys.modules["df.enhance"]

    _orig_loader = _e.DataLoader

    def _single_process_loader(*args, **kwargs):
        kwargs["num_workers"] = 0
        kwargs["pin_memory"] = False
        return _orig_loader(*args, **kwargs)

    _e.DataLoader = _single_process_loader  # type: ignore[assignment]
    _e.run()  # 解析 argv 并执行增强
    return 0


if __name__ == "__main__":
    sys.exit(main())
