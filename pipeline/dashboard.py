"""本地 Web 看板（只读）。

用 stdlib ``http.server`` 起一个**只监听回环地址**的小服务，浏览器打开即可看到
所有作业的阶段时间线、进度条与剩余时间。

* 零第三方依赖（离线环境装不了包）。
* 只有 ``GET /`` 与 ``GET /api/state`` 两个只读路由，``POST`` 一律 405。
* 前端不使用任何 CDN / 外部资源，全部内联。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import progress as P
from .config import load_config

log = logging.getLogger("pipeline.dashboard")

#: 前端轮询间隔（毫秒）占位符，运行时替换——避免用 str.format 与 JS 花括号冲突
_INTERVAL_TOKEN = "__INTERVAL_MS__"    # noqa: S105 —— 这是占位符，不是密钥

_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频处理实时监控</title>
<style>
:root{
  --bg:#0F172A; --card:#1E293B; --text:#E2E8F0; --dim:#94A3B8;
  --waiting:#64748B; --resource:#D97706; --running:#0EA5E9;
  --retry:#F97316; --done:#16A34A; --failed:#DC2626; --skipped:#475569;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
a{color:var(--running)}
header{position:sticky;top:0;z-index:5;background:var(--bg);
  border-bottom:1px solid #334155;padding:12px 18px}
h1{margin:0 0 8px;font-size:16px;font-weight:600}
.banner{background:var(--failed);color:#fff;padding:8px 18px;font-weight:600}
.hidden{display:none}
.stats{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
.stat{display:flex;gap:6px;align-items:baseline}
.stat b{font-size:18px}
.stat span{color:var(--dim);font-size:12px}
.meta{color:var(--dim);font-size:12px;margin-top:6px;
  display:flex;flex-wrap:wrap;gap:4px 18px}
main{display:grid;gap:12px;padding:16px 18px 40px;
  grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid #334155;border-left:4px solid var(--waiting);
  border-radius:8px;padding:12px 14px;transition:border-color .3s}
.card.running{border-left-color:var(--running);border-color:var(--running);
  animation:pulse 2s ease-in-out infinite}
.card.done{border-left-color:var(--done);opacity:.7}
.card.failed{border-left-color:var(--failed)}
.card.retry{border-left-color:var(--retry);
  background-image:repeating-linear-gradient(45deg,rgba(249,115,22,.06) 0 8px,transparent 8px 16px)}
.card.resource{border-left-color:var(--resource)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(14,165,233,.5)}
  50%{box-shadow:0 0 0 4px rgba(14,165,233,.12)}}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node{font-size:12px;white-space:nowrap;padding:1px 8px;border-radius:10px;
  background:#0f172a;border:1px solid #334155}
.node.running{color:var(--running)}.node.done{color:var(--done)}
.node.failed{color:var(--failed)}.node.retry{color:var(--retry)}
.node.waiting{color:var(--waiting)}.node.resource{color:var(--resource)}
.bar{height:8px;background:#0f172a;border-radius:4px;overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;width:0;border-radius:4px;background:var(--running);
  transition:width .5s ease}
.bar.done>i{background:var(--done)}.bar.failed>i{background:var(--failed)}
.bar.retry>i{background:var(--retry)}
.pctline{display:flex;justify-content:space-between;font-size:12px;color:var(--dim)}
.timeline{display:flex;gap:2px;margin-top:10px}
.tl{flex:1;text-align:center;font-size:10px;color:var(--dim);position:relative}
.tl i{display:block;height:4px;border-radius:2px;background:#334155;margin-bottom:4px}
.tl.done i{background:var(--done)}.tl.current i{background:var(--running)}
.tl.failed i{background:var(--failed)}
.tl.skipped i{background:transparent;border:1px dashed var(--skipped)}
.tl.current{color:var(--running);font-weight:600}
.tl span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.err{margin-top:8px;font-size:12px;color:var(--failed);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
footer{color:var(--dim);font-size:12px;padding:0 18px 24px}
</style>
</head>
<body>
<div id="banner" class="banner hidden"></div>
<header>
  <h1>视频处理实时监控</h1>
  <div class="stats" id="stats"></div>
  <div class="meta" id="meta"></div>
  <div class="meta" id="freshness"></div>
</header>
<main id="grid"></main>
<footer>
  只读视图：仅监听 127.0.0.1，不修改任何数据。进度与剩余时间由历史实测阶段耗时推算。
</footer>
<script>
const INTERVAL_MS = __INTERVAL_MS__;
const GRID = document.getElementById('grid');
const BANNER = document.getElementById('banner');
const STATS = document.getElementById('stats');
const META = document.getElementById('meta');
const FRESHNESS = document.getElementById('freshness');
const cards = new Map();
let failCount = 0;
let lastOk = 0;

function el(tag, cls, text){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text !== undefined) e.textContent = text;
  return e;
}

function buildCard(j){
  const c = el('div','card');
  const r1 = el('div','row');
  const name = el('span','name', j.name);
  const node = el('span','node');
  r1.append(name, node);
  const bar = el('div','bar'); bar.append(el('i'));
  const pl = el('div','pctline');
  const left = el('span'); const right = el('span');
  pl.append(left, right);
  const tl = el('div','timeline');
  const err = el('div','err');
  c.append(r1, bar, pl, tl, err);
  c._parts = {node, bar, left, right, tl, err};
  return c;
}

function updateCard(c, j){
  const p = c._parts;
  c.className = 'card ' + j.style_key;
  p.node.className = 'node ' + j.style_key;
  p.node.textContent = j.node_icon + ' ' + j.node_label;

  if(j.percent === null){
    p.bar.className = 'bar ' + j.style_key;
    p.bar.firstChild.style.width = '0%';
    p.left.textContent = (j.queue_pos ? '队列第 ' + j.queue_pos + ' 位' : '未开始');
    p.right.textContent = '预计 ' + j.eta_text;
  } else {
    p.bar.className = 'bar ' + j.style_key;
    p.bar.firstChild.style.width = Math.max(0, Math.min(100, j.percent)) + '%';
    p.left.textContent = j.percent.toFixed(1) + '%';
    let extra = [];
    if(j.duration_text && j.duration_text !== '-') extra.push('片源 ' + j.duration_text);
    if(j.speed) extra.push(j.speed.toFixed(2) + 'x 实时');
    if(j.retry_count) extra.push('重试 ' + j.retry_count + ' 次');
    p.right.textContent = '剩余 ' + j.eta_text + (extra.length ? ' · ' + extra.join(' · ') : '');
  }

  const sig = j.timeline.map(n => n.key + ':' + n.state).join('|');
  if(p.tl._sig !== sig){
    p.tl._sig = sig;
    p.tl.replaceChildren();
    j.timeline.forEach(n => {
      const d = el('div','tl ' + n.state);
      d.append(el('i'), el('span', null, n.label));
      d.title = n.label + ' — ' + n.note;
      p.tl.append(d);
    });
  }
  p.err.textContent = (j.style_key === 'failed' || j.style_key === 'retry') ? (j.error || '') : '';
}

function render(data){
  lastOk = Date.now();
  const stats = [
    ['总数', data.total, ''], ['完成', data.done, 'done'],
    ['运行', data.running, 'running'], ['等待', data.waiting, 'waiting'],
    ['失败', data.failed, 'failed']
  ];
  STATS.replaceChildren();
  stats.forEach(([label, val]) => {
    const d = el('div','stat');
    const b = el('b', null, String(val));
    b.style.color = val ? ('var(--' + (label === '总数' ? 'running' : (label === '完成' ? 'done' : (label === '失败' ? 'failed' : (label === '运行' ? 'running' : 'waiting')))) + ')') : 'var(--dim)';
    d.append(b, el('span', null, label));
    STATS.append(d);
  });
  const eta = el('div','stat');
  eta.append(el('b', null, data.queue_eta_text), el('span', null, '队列剩余'));
  STATS.append(eta);

  META.replaceChildren();
  const bits = [];
  bits.push('单文件均值 ' + humanFromSeconds(data.eta_throughput_s / Math.max(1, data.counts.DONE + data.waiting + data.running + data.failed || 1)));
  if(data.disk_free_gb !== null) bits.push('磁盘剩余 ' + data.disk_free_gb.toFixed(1) + ' GB');
  if(data.ram_percent !== null) bits.push('内存 ' + data.ram_percent.toFixed(0) + '%');
  bits.push('阶段模型 ' + data.model_age_s.toFixed(0) + ' 秒前更新');
  bits.push('数据时刻 ' + new Date(data.ts * 1000).toLocaleTimeString('zh-CN'));
  bits.forEach(t => META.append(el('span', null, t)));

  const seen = new Set();
  data.jobs.forEach(j => {
    seen.add(j.job_id);
    let c = cards.get(j.job_id);
    if(!c){ c = buildCard(j); cards.set(j.job_id, c); GRID.append(c); }
    updateCard(c, j);
    GRID.append(c);   // 维持按 data.jobs 的次序（已是"最需关注优先"）
  });
  cards.forEach((c, id) => {
    if(!seen.has(id)){ c.remove(); cards.delete(id); }
  });
}

function humanFromSeconds(s){
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if(h >= 24) return Math.floor(h / 24) + ' 天 ' + (h % 24) + ' 小时';
  if(h) return h + ' 小时 ' + m + ' 分';
  if(m) return m + ' 分';
  return '即将完成';
}

async function refresh(){
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2000);
  try{
    const r = await fetch('/api/state', {cache:'no-store', signal:ctrl.signal});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    render(await r.json());
    failCount = 0;
    BANNER.classList.add('hidden');
  }catch(e){
    failCount++;
    if(failCount >= 3){
      BANNER.textContent = '与流水线失联（调度器已退出或数据库被锁）——已连续失败 ' + failCount + ' 次';
      BANNER.classList.remove('hidden');
    }
  }finally{
    clearTimeout(timer);
  }
}

function updateFreshness(){
  if(!lastOk){
    FRESHNESS.textContent = '尚未取得数据…';
    return;
  }
  const s = Math.max(0, Math.round((Date.now() - lastOk) / 1000));
  FRESHNESS.textContent = s <= 1
    ? '数据刚刚更新'
    : ('数据最后更新于 ' + s + ' 秒前' +
       (s > 10 ? '　（浏览器会节流后台标签页的定时器，切回本页会自动补取一次）' : ''));
}

// 后台标签页里 setInterval 会被浏览器节流（可能降到 1 次/分钟），切回来看时
// 还是旧数据。可见性变化时立刻补一次，避免"看上去没有自动刷新"。
document.addEventListener('visibilitychange', () => {
  if(!document.hidden) refresh();
});

refresh();
setInterval(refresh, INTERVAL_MS);
setInterval(updateFreshness, 1000);
updateFreshness();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "video-pipeline-dashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:   # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass          # 浏览器刷新时取消了上一个请求，属正常现象

    def do_GET(self) -> None:   # noqa: N802 —— BaseHTTPRequestHandler 约定
        path = urlsplit(self.path).path
        if path == "/":
            ms = self.server.interval_ms          # type: ignore[attr-defined]
            html = _HTML.replace(_INTERVAL_TOKEN, str(ms))
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        elif path == "/api/state":
            cfg = self.server.cfg                 # type: ignore[attr-defined]
            body = json.dumps(P.snapshot_to_dict(P.build_snapshot(cfg)),
                              ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:   # noqa: N802
        self._send(405, "text/plain; charset=utf-8", b"method not allowed")


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, cfg, interval_ms: int) -> None:
        super().__init__(addr, handler)
        self.cfg = cfg
        self.interval_ms = interval_ms


def _bind(host: str, base: int, explicit: bool, cfg, interval_ms: int):
    """找一个可用端口。显式指定时不做顺延，直接报错更不容易误导用户。"""
    last: OSError | None = None
    for port in range(base, base + (1 if explicit else 10)):
        try:
            return _Server((host, port), _Handler, cfg, interval_ms), port
        except OSError as exc:
            last = exc
    raise OSError(f"端口 {base}~{base + 9} 均被占用: {last}")


def run_dashboard(cfg_path: str = "config.yaml", port: int | None = None,
                  interval: float | None = None, open_browser: bool = False,
                  allow_remote: bool = False) -> int:
    cfg = load_config(cfg_path)
    dash = (cfg.raw or {}).get("dashboard", {}) or {}
    explicit = port is not None
    base = int(port if port is not None else dash.get("port", 8765))
    if interval is None:
        interval = float(dash.get("interval_seconds", 3))
    interval = max(interval, 0.5)
    host = "0.0.0.0" if allow_remote else "127.0.0.1"

    if allow_remote:
        print("[警告] --allow-remote 已开启，看板将暴露给局域网；"
              "本项目的约束是完全本地运行，请谨慎使用。", file=sys.stderr)

    server, actual = _bind(host, base, explicit, cfg, int(interval * 1000))
    url = f"http://127.0.0.1:{actual}/"
    print(f"进度看板已启动：{url}")
    print(f"  只读视图，仅监听 {host}；刷新间隔 {interval:g}s；Ctrl+C 退出")
    if allow_remote:
        print(f"  局域网访问：http://<本机IP>:{actual}/")

    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.4),
                                         webbrowser.open(url)),
                         daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


def add_cli(sub) -> None:
    """在 main.py 的 subparsers 上注册 dashboard 子命令。"""
    p = sub.add_parser("dashboard", help="本地 Web 进度看板（只读，绑 127.0.0.1）")
    p.add_argument("--port", type=int, default=None,
                   help="监听端口（默认取 config 的 dashboard.port，占用则顺延）")
    p.add_argument("--interval", type=float, default=None,
                   help="浏览器刷新间隔秒数（默认取 config，建议 >=1）")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p.add_argument("--allow-remote", action="store_true",
                   help="允许局域网访问（默认只监听本机回环地址）")
    p.set_defaults(_handler=_cmd)


def _cmd(args) -> int:
    return run_dashboard(args.config, port=args.port, interval=args.interval,
                         open_browser=args.open, allow_remote=args.allow_remote)