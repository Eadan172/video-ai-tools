"""修复效果量化验证（画质 + 音质）。

用法：
    python scripts/verify_repair.py <源文件> <修复后文件> [--frames N] [--json out.json]

指标说明
--------
画质（在**同一分辨率**下比较，修复后先缩放回源分辨率，保证可比）：
  * 锐度 sharpness   ：灰度图 Laplacian 的方差，越大越锐利
  * 块效应 blocking   ：8x8 编码块边界处梯度 / 块内梯度 的比值，
                        越小说明压缩块状伪影越少（h264/wmv 常见伪影）
  * 分辨率 / 码率     ：直接反映超分是否生效

音质：
  * 噪声底 noise_floor：能量最低 10% 帧的 RMS，越小说明噪声抑制越好
  * 整体 RMS          ：语音/音乐能量，过大变化提示过度处理
  * 时长              ：应与源一致

输出：控制台对比表 + 结论；--json 可落地为机器可读报告。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------- #
def run(cmd: list[str], binary: bool = False):
    r = subprocess.run(cmd, capture_output=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"命令失败: {' '.join(cmd[:3])}...\n{r.stderr.decode('utf-8', 'replace')[-500:]}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    d = json.loads(out)
    fmt = d.get("format", {})
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "container": fmt.get("format_name", ""),
        "duration": float(fmt.get("duration", 0) or 0),
        "size": int(fmt.get("size", 0) or 0),
        "bitrate": int(fmt.get("bit_rate", 0) or 0),
        "v_codec": v.get("codec_name", ""),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "a_codec": a.get("codec_name", ""),
        "sample_rate": int(a.get("sample_rate", 0) or 0),
    }


def extract_frames(path: Path, n: int, duration: float, w: int, h: int,
                   flags: str = "bilinear") -> np.ndarray:
    """在时间轴上均匀抽取 n 帧灰度图，缩放到 w×h，返回 (n, h, w)。

    flags 为 ffmpeg scale 的重采样算法（bilinear / bicubic / neighbor）。
    """
    if duration <= 0:
        raise RuntimeError("duration <= 0")
    step = max(duration / (n + 1), 0.1)
    frames = []
    for i in range(1, n + 1):
        t = step * i
        raw = run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                   "-frames:v", "1", "-vf", f"scale={w}:{h}:flags={flags}",
                   "-pix_fmt", "gray", "-f", "rawvideo", "-"], binary=True)
        need = w * h
        if len(raw) >= need:
            frames.append(np.frombuffer(raw[:need], dtype=np.uint8)
                          .reshape(h, w).astype(np.float32))
    if not frames:
        raise RuntimeError("未能抽取任何帧")
    return np.stack(frames)


def laplacian_variance(img: np.ndarray) -> float:
    """锐度：Laplacian 响应方差（越大越锐）。"""
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    lap = (img[:-2, 1:-1] * k[0, 1] + img[1:-1, :-2] * k[1, 0]
           + img[1:-1, 1:-1] * k[1, 1] + img[1:-1, 2:] * k[1, 2]
           + img[2:, 1:-1] * k[2, 1])
    return float(lap.var())


def blocking_score(img: np.ndarray, block: int = 8) -> float:
    """块效应比值：8 像素块边界处的平均绝对梯度 / 块内平均绝对梯度。

    值越接近 1 越好；明显 > 1 表示存在网格状压缩伪影。
    """
    dh = np.abs(np.diff(img, axis=1))          # 水平方向相邻像素差 (h, w-1)
    dv = np.abs(np.diff(img, axis=0))          # 垂直方向相邻像素差 (h-1, w)
    h, w = img.shape

    cols = np.arange(block - 1, w - 1, block)  # 块边界的列索引（在 dh 坐标系）
    rows = np.arange(block - 1, h - 1, block)  # 块边界的行索引（在 dv 坐标系）
    if len(cols) == 0 or len(rows) == 0:
        return float("nan")
    mask_c = np.ones(dh.shape[1], dtype=bool)
    mask_c[cols] = False
    mask_r = np.ones(dv.shape[0], dtype=bool)
    mask_r[rows] = False

    edge = np.concatenate([dh[:, cols].ravel(), dv[rows, :].ravel()])
    inner = np.concatenate([dh[:, mask_c].ravel(), dv[mask_r, :].ravel()])
    if inner.size == 0 or inner.mean() == 0:
        return float("nan")
    return float(edge.mean() / inner.mean())


# --------------------------------------------------------------------------- #
def audio_metrics(path: Path) -> dict:
    """抽取 48k 单声道 PCM，计算噪声底与整体 RMS。"""
    raw = run(["ffmpeg", "-v", "error", "-i", str(path),
               "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
              binary=True)
    if not raw:
        return {"error": "无音频流"}
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if x.size == 0:
        return {"error": "音频为空"}
    win = int(48000 * 0.02)
    frames = x[: x.size // win * win].reshape(-1, win)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    k = max(1, int(len(rms) * 0.10))
    noise = float(np.sort(rms)[:k].mean())
    overall = float(np.sqrt((x ** 2).mean()))
    return {
        "noise_floor": noise,
        "rms": overall,
        "noise_floor_db": 20 * np.log10(max(noise, 1e-12)),
        "rms_db": 20 * np.log10(max(overall, 1e-12)),
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="修复效果量化验证")
    ap.add_argument("source", help="源文件")
    ap.add_argument("repaired", help="修复后文件")
    ap.add_argument("--frames", type=int, default=8, help="抽样帧数（默认 8）")
    ap.add_argument("--json", dest="json_out", default=None, help="写出 JSON 报告")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"[错误] 未找到 {tool}，请先加入 PATH", file=sys.stderr)
            return 2

    src, rep = Path(args.source), Path(args.repaired)
    for p in (src, rep):
        if not p.is_file():
            print(f"[错误] 文件不存在: {p}", file=sys.stderr)
            return 2

    si, ri = probe(src), probe(rep)
    report: dict = {"source": str(src), "repaired": str(rep),
                    "source_info": si, "repaired_info": ri}

    print("=" * 68)
    print("修复效果验证")
    print("=" * 68)
    print(f"源     : {src.name}")
    print(f"修复后 : {rep.name}")
    print()
    print(f"{'属性':<22}{'源':>18}{'修复后':>18}")
    print("-" * 68)
    def row(label, a, b):
        print(f"{label:<22}{str(a):>18}{str(b):>18}")
    row("容器", si["container"], ri["container"])
    row("视频编码", si["v_codec"], ri["v_codec"])
    row("分辨率", f'{si["width"]}x{si["height"]}', f'{ri["width"]}x{ri["height"]}')
    row("时长(s)", f'{si["duration"]:.2f}', f'{ri["duration"]:.2f}')
    row("码率(kbps)", f'{si["bitrate"]//1000}', f'{ri["bitrate"]//1000}')
    row("音频编码", si["a_codec"], ri["a_codec"])
    row("采样率", si["sample_rate"], ri["sample_rate"])

    scale = (ri["width"] / si["width"]) if si["width"] else 0
    report["scale"] = round(scale, 3)

    # ---- 画质（同分辨率比较） ---- #
    print()
    print("画质指标（已把修复后缩回源分辨率；缩小本身会抹掉高频，锐度仅供参考，见下方超分对照）")
    print("-" * 68)
    try:
        fs = extract_frames(src, args.frames, min(si["duration"], ri["duration"]),
                            si["width"], si["height"])
        fr = extract_frames(rep, args.frames, min(si["duration"], ri["duration"]),
                            si["width"], si["height"])
        s_sharp = float(np.mean([laplacian_variance(f) for f in fs]))
        r_sharp = float(np.mean([laplacian_variance(f) for f in fr]))
        s_block = float(np.nanmean([blocking_score(f) for f in fs]))
        r_block = float(np.nanmean([blocking_score(f) for f in fr]))
        print(f"{'指标':<22}{'源':>18}{'修复后':>18}{'变化':>10}")
        print("-" * 68)
        print(f"{'锐度(Laplacian方差)':<22}{s_sharp:>18.1f}{r_sharp:>18.1f}"
              f"{(r_sharp/max(s_sharp,1e-9)-1)*100:>9.1f}%")
        print(f"{'块效应比值(越低越好)':<22}{s_block:>18.3f}{r_block:>18.3f}"
              f"{(r_block/max(s_block,1e-9)-1)*100:>9.1f}%")
        report["video"] = {
            "sharpness_source": s_sharp, "sharpness_repaired": r_sharp,
            "blocking_source": s_block, "blocking_repaired": r_block,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  画质指标计算失败: {exc}")
        report["video"] = {"error": str(exc)}

    # ---- 超分公平对照（仅在发生放大时） ---- #
    # 说明：把 2x 输出「缩小回源分辨率」再比 Laplacian，会因重采样天然丢高频，
    # 得出「锐度下降」的假象。真正公平的做法是**在修复后分辨率下**，
    # 把「双三次放大的源」当作基线，与 AI 输出直接比较。
    if scale > 1.05:
        print()
        print("超分公平对照（在修复后分辨率下比较）")
        print("-" * 68)
        try:
            rw, rh = ri["width"], ri["height"]
            dur = min(si["duration"], ri["duration"])
            base_frames = extract_frames(src, args.frames, dur, rw, rh,
                                         flags="bicubic")
            rep_frames = extract_frames(rep, args.frames, dur, rw, rh,
                                        flags="neighbor")
            b_sharp = float(np.mean([laplacian_variance(f) for f in base_frames]))
            a_sharp = float(np.mean([laplacian_variance(f) for f in rep_frames]))
            gain = (a_sharp / max(b_sharp, 1e-9) - 1) * 100
            print(f"{'指标':<22}{'双三次基线':>18}{'AI 修复':>18}{'变化':>10}")
            print("-" * 68)
            print(f"{'锐度(Laplacian方差)':<22}{b_sharp:>18.1f}{a_sharp:>18.1f}"
                  f"{gain:>9.1f}%")
            report["upscale_fair"] = {
                "baseline_bicubic_sharpness": b_sharp,
                "repaired_sharpness": a_sharp,
                "gain_percent": float(gain),
            }
        except Exception as exc:  # noqa: BLE001
            print(f"  超分对照计算失败: {exc}")
            report["upscale_fair"] = {"error": str(exc)}

    # ---- 音质 ---- #
    print()
    print("音质指标")
    print("-" * 68)
    am_s, am_r = audio_metrics(src), audio_metrics(rep)
    if "error" in am_s or "error" in am_r:
        print(f"  源: {am_s.get('error', 'ok')} | 修复后: {am_r.get('error', 'ok')}")
        report["audio"] = {"source": am_s, "repaired": am_r}
    else:
        print(f"{'指标':<26}{'源':>16}{'修复后':>16}{'变化':>10}")
        print("-" * 68)
        print(f"{'噪声底(RMS)':<26}{am_s['noise_floor']:>16.6f}"
              f"{am_r['noise_floor']:>16.6f}"
              f"{(am_r['noise_floor']/max(am_s['noise_floor'],1e-12)-1)*100:>9.1f}%")
        print(f"{'整体能量(RMS)':<26}{am_s['rms']:>16.6f}{am_r['rms']:>16.6f}"
              f"{(am_r['rms']/max(am_s['rms'],1e-12)-1)*100:>9.1f}%")
        drop = 20 * np.log10(max(am_s['noise_floor'], 1e-12) /
                             max(am_r['noise_floor'], 1e-12))
        print(f"\n  噪声抑制量: {drop:+.1f} dB（正值表示显著降噪）")
        report["audio"] = {"source": am_s, "repaired": am_r,
                           "noise_reduction_db": float(drop)}

    # ---- 结论 ---- #
    print()
    print("=" * 68)
    print("结论")
    print("=" * 68)
    verdict = []
    if scale > 1.05:
        verdict.append(f"✔ 超分生效：{si['width']}x{si['height']} → {ri['width']}x{ri['height']}（{scale:.2f}x）")
    elif scale:
        verdict.append(f"· 分辨率未放大（{scale:.2f}x）——若期望超分请检查 models.upscale 配置")
    v = report.get("video", {})
    if "blocking_source" in v and v["blocking_repaired"] < v["blocking_source"]:
        verdict.append(f"✔ 块效应下降 {v['blocking_source']:.3f} → {v['blocking_repaired']:.3f}（压缩伪影减少）")
    uf = report.get("upscale_fair", {})
    if uf.get("gain_percent", 0) > 5:
        verdict.append(f"✔ 同分辨率下锐度优于双三次放大基线 {uf['gain_percent']:.1f}%（AI 超分确实补出了细节）")
    if report.get("audio", {}).get("noise_reduction_db", 0) > 1.0:
        verdict.append(f"✔ 噪声底下降 {report['audio']['noise_reduction_db']:.1f} dB（音质修复生效）")
    for line in verdict or ["· 未检测到明显改善，请检查 AI 阶段是否真正执行（见 logs/jobs/*.log）"]:
        print("  " + line)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
