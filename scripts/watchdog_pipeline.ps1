# =============================================================================
# 批处理守护脚本（无人值守）—— 保证队列不因内存问题停顿
#
# 做两件事：
#   1) 调度器进程消失（内存压力下被系统杀掉）→ 清理它留下的孤儿 RVE/ffmpeg
#      进程，重新拉起 `main.py run`；
#   2) RVE 还活着但输出文件长时间不增长（卡死/在换页里打转）→ 杀掉该 RVE，
#      并把这个任务在库里降到队尾，让调度器立刻转到下一个视频。
#
# 队列清空（没有待处理任务）后脚本自行退出，不会空转。
# 每 5 分钟巡检一次，每次写一行到 logs/watchdog.log（异常事件另有专门记录）。
#
# 用法：powershell -ExecutionPolicy Bypass -File logs\watchdog_pipeline.ps1
# 注意：必须用 WMI 创建（Invoke-CimMethod Win32_Process Create）启动，
#      用 Start-Process 起的进程在本工具会话结束时会被一并杀掉。
# =============================================================================

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 必须是**无 BOM** 的 UTF-8：带 BOM 时管道给 python 的 stdin 会在首行前多出
# U+FEFF，python 直接 SyntaxError: invalid non-printable character U+FEFF。
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$root     = Split-Path -Parent $PSScriptRoot          # 脚本放在 logs/ 下 → 上一级是项目根
$python   = Join-Path $root '.venv\Scripts\python.exe'
$log      = Join-Path $root 'logs\watchdog.log'
$vout     = Join-Path $root 'work\current\video_ai.mp4'
$interval = 300      # 巡检间隔（秒）。2 分钟一次太吵（每天 720 行日志），改 5 分钟
$stall_n  = 3        # 连续 N 次输出无增长 → 判定卡死（N × interval = 15 分钟）

Set-Location $root

function Write-Log($msg) {
    Add-Content -Path $log -Encoding UTF8 -Value (
        "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
}

# 只读查询：返回 "活动任务数|当前任务与阶段|当前输出MB"
# 刻意只用 ASCII 输出，避免管道编码把中文文件名字节搞乱。
$pyState = @'
import sqlite3
try:
    c = sqlite3.connect("file:pipeline.db?mode=ro", uri=True)
    rows = dict(c.execute("select status, count(*) from jobs group by status").fetchall())
    cur = c.execute("select job_id,stage from jobs where status='RUNNING'").fetchone()
except Exception:
    rows, cur = {}, None
active = sum(rows.get(k, 0) for k in ("DISCOVERED", "WAITING", "WAIT_RESOURCE",
                                      "WAIT_DISK", "RUNNING", "RETRY_PENDING"))
print("%d|%s|%s" % (active, cur[0] if cur else "-", cur[1] if cur else "-"))
'@

# 把当前 RUNNING 的任务降到队尾（内存卡死的任务不要原地反复重跑）
$pyDemote = @'
import sqlite3
c = sqlite3.connect("pipeline.db", timeout=10)
c.execute("PRAGMA busy_timeout=5000")
c.execute("UPDATE jobs SET priority=-1 WHERE status='RUNNING'")
c.commit()
'@

# 队列跑完后生成"未成功导出"报告（落在 logs/ 下，带时间戳）
$pyReport = @'
import sqlite3, os, time

root = os.getcwd()
out = os.path.join(root, "logs",
                   time.strftime("%Y%m%d_%H%M%S") + "_未导出视频报告.md")
con = sqlite3.connect("file:pipeline.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
counts = dict(con.execute("select status, count(*) from jobs group by status").fetchall())
total = sum(counts.values())
done = counts.get("DONE", 0)
bad = list(con.execute(
    "select job_id, source_path, status, stage, retry_count, finished_at, last_error "
    "from jobs where status != 'DONE' order by job_id"))

def base(p):
    return p.replace("/", "\\").split("\\")[-1]

L = []
L.append("# 课程视频批量处理 —— 未成功导出清单")
L.append("")
L.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
L.append("- 队列总数：%d，成功导出：%d，未导出：%d" % (total, done, len(bad)))
L.append("")
if not bad:
    L.append("全部任务均已成功导出。")
else:
    L.append("## 汇总")
    L.append("")
    L.append("| 源文件 | job | 状态 | 失败阶段 | 重试 | 最后失败时间 | 原因 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in bad:
        err = (r["last_error"] or "").replace("\n", " ").replace("|", "/")
        if len(err) > 70:
            err = err[:70] + "…"
        L.append("| %s | %d | %s | %s | %d | %s | %s |" % (
            base(r["source_path"]), r["job_id"], r["status"], r["stage"],
            r["retry_count"], r["finished_at"] or "-", err))
    L.append("")
    L.append("## 明细")
    for r in bad:
        L.append("")
        L.append("### %s（job %d）" % (base(r["source_path"]), r["job_id"]))
        L.append("")
        L.append("- 源文件：%s" % r["source_path"])
        L.append("- 状态：%s；失败阶段：%s" % (r["status"], r["stage"]))
        L.append("- 重试次数：%d" % r["retry_count"])
        L.append("- 最后失败时间：%s" % (r["finished_at"] or "-"))
        L.append("- 原因：%s" % (r["last_error"] or "-"))
        st = list(con.execute(
            "select stage, result, attempts, error from job_stages "
            "where job_id=? order by stage", (r["job_id"],)))
        if st:
            L.append("- 阶段记录：")
            for s in st:
                L.append("  - %s：%s（尝试 %d 次）%s" % (
                    s["stage"], s["result"], s["attempts"],
                    (s["error"] or "").replace("\n", " ")[:100]))
        ev = list(con.execute(
            "select ts, level, message from events where job_id=? "
            "order by id desc limit 5", (r["job_id"],)))
        if ev:
            L.append("- 最近事件：")
            for e in ev:
                L.append("  - %s [%s] %s" % (
                    e["ts"], e["level"],
                    (e["message"] or "").replace("\n", " ")[:160]))
L.append("")
L.append("说明：原因为「系统内存不足」的失败是 RVE 后端在长视频上的内存耗尽")
L.append("（非确定性，实测约 1/4 概率），可在内存充裕时执行 ")
L.append("`python main.py retry` 重置后再跑一次。")
open(out, "w", encoding="utf-8").write("\n".join(L))
print("REPORT=" + out)
'@

function Get-State {
    # 读队列状态。读失败必须与"队列真的空了"区分开——否则一次读失败就会
    # 让守护误判队列清空而退出。
    $raw = ($pyState | & $python - 2>&1) | Out-String
    $last = ($raw -split "`r?`n" | Where-Object { $_ -match '^\d+\|' } |
             Select-Object -Last 1)
    if (-not $last) { return $null }
    return $last.Trim()
}

function Get-Scheduler {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*main.py*' -and
                       $_.CommandLine -like '*run*' }
}

function Get-Rve {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*rve-backend.py*' }
}

function Stop-PipelineChildren {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='ffmpeg.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$root*" -and
                       ($_.CommandLine -like '*rve-backend.py*' -or $_.Name -eq 'ffmpeg.exe') } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                Write-Log ("  killed pid={0} {1}" -f $_.ProcessId, $_.Name)
            } catch { }
        }
    Start-Sleep -Seconds 5
}

function Start-Scheduler {
    Start-Process -FilePath $python `
        -ArgumentList 'main.py', '--config=config.yaml', 'run' `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root 'logs\batch_kecheng_supervised.out.log') `
        -RedirectStandardError  (Join-Path $root 'logs\batch_kecheng_supervised.err.log')
}

Write-Log ("守护启动：interval={0}s stall={1} 次" -f $interval, $stall_n)

# 复用项目自己的定位结果（scripts/bootstrap.ps1 第 3 步）：FFmpegAdapter 用的是
# 裸命令名 "ffmpeg"，靠 PATH 找。不把 .tools\ffmpeg 放上 PATH，跑到 REPAIR_AUDIO
# 的第一步就报「找不到可执行文件: ffmpeg」（实测一次报废 4 个视频）。
$ffdir = Join-Path $root '.tools\ffmpeg'
if (Test-Path (Join-Path $ffdir 'ffmpeg.exe')) {
    $env:PATH = "$ffdir;$env:PATH"
    Write-Log ("PATH 已加入 " + $ffdir)
} else {
    Write-Log ("警告：未找到 " + $ffdir + "\ffmpeg.exe，调度器可能因找不到 ffmpeg 失败")
}

$last_size = -1
$frozen = 0

while ($true) {
    $state = Get-State
    if (-not $state) {
        Write-Log '读取队列状态失败，本轮跳过（不重启、不退出）'
        Start-Sleep -Seconds $interval
        continue
    }
    $parts = $state.Split('|')
    $active = [int]($parts[0])
    $jobid  = $parts[1]
    $stage  = $parts[2]

    # ---- 队列跑完 → 生成未导出报告 → 守护退出 ----
    if ($active -le 0) {
        $rep = ($pyReport | & $python - 2>&1) | Out-String
        Write-Log ("active=0 队列已清空，报告：" + $rep.Trim())
        break
    }

    $size_mb = -1
    if (Test-Path $vout) { $size_mb = [math]::Round((Get-Item $vout).Length / 1MB, 1) }

    # ---- 1) 调度器是否还活着 ----
    $sched = @(Get-Scheduler)
    if ($sched.Count -eq 0) {
        Write-Log ("active={0} job={1} {2} | out={3}MB —— 调度器已不在，清理孤儿进程后重启" -f `
                   $active, $jobid, $stage, $size_mb)
        Stop-PipelineChildren
        Start-Scheduler
        $last_size = -1
        $frozen = 0
        Start-Sleep -Seconds $interval
        continue
    }

    # ---- 2) RVE 是否卡死（输出长时间不增长） ----
    $rve = @(Get-Rve)
    if ($rve.Count -gt 0) {
        # $size_mb = -1 表示输出文件根本还没出现 —— 那同样是"没有进展"，必须计入
        # frozen。否则 RVE 卡在启动/读帧阶段（实测降超分倍率重试时挂了 1 小时 40
        # 分钟、一个字节都没写）永远不会被判为卡死，调度器要陪它等到 4 小时超时。
        if ($size_mb -eq $last_size) {
            $frozen++
        } else {
            $frozen = 0
        }
        if ($frozen -ge $stall_n) {
            Write-Log ("active={0} job={1} {2} | out={3}MB 连续 {4} 次无增长 —— 判定卡死，终止 RVE 并让出队列" -f `
                       $active, $jobid, $stage, $size_mb, $frozen)
            $pyDemote | & $python - | Out-Null
            Stop-PipelineChildren
            $frozen = 0
            $last_size = -1
            Start-Sleep -Seconds $interval
            continue
        }
        Write-Log ("active={0} job={1} {2} | out={3}MB frozen={4} 调度器在跑" -f `
                   $active, $jobid, $stage, $size_mb, $frozen)
    } else {
        $frozen = 0
        Write-Log ("active={0} job={1} {2} | 调度器在跑（当前无 RVE 进程）" -f `
                   $active, $jobid, $stage)
    }

    $last_size = $size_mb
    Start-Sleep -Seconds $interval
}

Write-Log '守护结束'
