#!/usr/bin/env python3
"""replay_driver.py — 并发回放驱动（A 的交付物，v2）

与旧版的区别（旧版是串行的，峰值并发恒为 1）：
  * 每个 session_id 一个线程，互不阻塞
  * 按**绝对 timestamp** 调度：send_at = run_start + (row.timestamp - min_timestamp)
    完全不用 delay_after_ms 累加（累加会漂移，20 会话就能把峰值并发从 16 拉到 10）
  * 行内动作顺序严格：
        resume_before ? POST agent-status resume
        -> POST /v1/chat/completions (带 X-Agent-Hint: base64url)
        -> 记录 TTFT
        -> state in {pause, finalize} ? POST agent-status <state>
  * kind=="event"（finalize）是事件不是推理：只发状态 POST，不发推理
  * --dry-run 不发任何请求，只算峰值并发并与 manifest 的 _peak 比对
  * 证据落到 <AH_EVIDENCE_ROOT>/<run_id>/{meta.json,requests.jsonl,metrics_before.json,metrics_after.json}
    （默认 AH_EVIDENCE_ROOT=<场景包>/results；可用 --out-root 覆盖）

stdlib only，不写 results/（那是 B 的地盘）。
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# 环境契约（见 env/env.sh）。本包内不出现机器相关硬编码：
#   AH_SCENARIO_ROOT / AH_EVIDENCE_ROOT / AH_DATA_DIR / AH_MODEL_NAME
#   AH_{A,B}_{INFER,MGMT,OBS,ENGINE}_PORT / AH_{A,B}_MOTOR_CONTAINER / AH_{A,B}_SHM_KEY
# 未设置时回退到「场景包相对路径」，保证单机自测也能直接跑。
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCENARIO_ROOT = os.environ.get("AH_SCENARIO_ROOT") or os.path.abspath(os.path.join(_HERE, ".."))


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


SHARED = _env("AH_EVIDENCE_ROOT", os.path.join(_SCENARIO_ROOT, "results"))
DATA_DIR = _env("AH_DATA_DIR", os.path.join(_SCENARIO_ROOT, "data", "generated"))
MANIFEST = os.path.join(DATA_DIR, "manifest_contend.json")

# lane 端口表：infer / mgmt(agent-status) / obs(metrics) / engine
_LANE_DEFAULTS = {
    "A": {"infer": 19475, "mgmt": 19476, "obs": 19477, "engine": 18220, "container": "ttft-laneA-motor"},
    "B": {"infer": 19485, "mgmt": 19486, "obs": 19487, "engine": 18230, "container": "ttft-laneB-motor"},
}
LANES = {
    _ln: {
        "infer": int(_env("AH_%s_INFER_PORT" % _ln, _d["infer"])),
        "mgmt": int(_env("AH_%s_MGMT_PORT" % _ln, _d["mgmt"])),
        "obs": int(_env("AH_%s_OBS_PORT" % _ln, _d["obs"])),
        "engine": int(_env("AH_%s_ENGINE_PORT" % _ln, _d["engine"])),
        "container": _env("AH_%s_MOTOR_CONTAINER" % _ln, _d["container"]),
    }
    for _ln, _d in _LANE_DEFAULTS.items()
}

MODEL = _env("AH_MODEL_NAME", "kvh")
PEAK_STEP_MS = 100          # manifest 的 _peak 是 100 ms 采样算的，必须用同一口径
VLLM_KEEP = {
    "vllm:kv_cache_usage_perc",
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:request_prefill_kv_computed_tokens_sum",
    "vllm:request_prefill_kv_computed_tokens_count",
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total",
    "vllm:prompt_tokens_total",
    "vllm:prompt_tokens_cached_total",
    "vllm:generation_tokens_total",
    "vllm:num_preemptions_total",
}
_VERBOSE = False


# ---------------------------------------------------------------- 工具
def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _get(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _post(url, payload, headers=None, timeout=60):
    """返回 (http_status, body_text)。HTTP 错误也返回状态码，不抛。"""
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")[:500]
    except Exception as exc:
        return 0, "{}: {}".format(type(exc).__name__, exc)


# --------------------------------------------------- agent_hint wire 编码
def _prune_none(value):
    """与 utils/agent_hint.py:_prune_none 完全一致：递归去 None，再去掉空 dict。"""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if item is None:
                continue
            result[key] = _prune_none(item)
        return {
            key: item
            for key, item in result.items()
            if not (isinstance(item, dict) and not item)
        }
    if isinstance(value, list):
        return [_prune_none(item) for item in value]
    return value


def encode_hint(hint):
    """与 encode_agent_hint_header 一致：utf-8 json 紧凑 -> urlsafe b64 去掉 '='。"""
    payload = _prune_none(hint)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------- 状态上报
def report_state(ports, session_id, state, epoch, timeout=30):
    """POST /internal/agent-status。返回 (http_status, dict, port_used)。"""
    payload = {"session": {"session_id": session_id}, "agent_status": {"state": state}}
    if epoch is not None:
        payload["session"]["epoch"] = epoch
    tried = []
    for name in ("mgmt", "obs"):
        port = ports[name]
        status, text = _post(
            "http://127.0.0.1:{}/internal/agent-status".format(port),
            payload,
            timeout=timeout,
        )
        tried.append({"port": port, "name": name, "status": status})
        if status and status != 404:
            body = {}
            try:
                body = json.loads(text or "{}")
            except Exception:
                body = {"raw": text[:300]}
            return status, body, tried
    return (tried[-1]["status"] if tried else 0), {"tried": tried}, tried


# ----------------------------------------------------------------- 推理
def infer_stream(port, body, hint_b64, timeout=900):
    """流式 POST /v1/chat/completions，返回 TTFT 等。绝对时间戳用 time.time()。"""
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"}
    if hint_b64:                      # off 臂：完全不发 header（B 2026-11-12 21:05 定的三臂语义）
        hdrs["X-Agent-Hint"] = hint_b64
    req = urllib.request.Request(
        "http://127.0.0.1:{}/v1/chat/completions".format(port),
        data=data,
        headers=hdrs,
        method="POST",
    )
    t0 = time.time()
    first = None
    chunks = 0
    usage = {}
    nchar = 0
    status = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if first is None:
                    first = time.time()
                chunks += 1
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        nchar += len(piece)
        end = time.time()
        return {
            "http_status": status,
            "first_token_abs": first,
            "end_abs": end,
            "chunks": chunks,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "text_len": nchar,
            "ok": 200 <= status < 300,
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8", "ignore")[:500]
        except Exception:
            pass
        return {
            "http_status": exc.code,
            "first_token_abs": None,
            "end_abs": time.time(),
            "chunks": 0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "text_len": 0,
            "ok": False,
            "error": "HTTPError {}: {}".format(exc.code, text),
        }
    except Exception as exc:
        return {
            "http_status": 0,
            "first_token_abs": None,
            "end_abs": time.time(),
            "chunks": 0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "text_len": 0,
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
        }


# ------------------------------------------------------------- metrics 快照
def _snapshot_once(obs_port):
    """整份 /metrics -> flat dict。

    kvc_*：键名剥掉 'motor:' 前缀和 label（现在只有 scope="central"），不做 scope 过滤。
    off 臂下这些指标压根不注册 -> kvc_metric_count=0、键不存在、raw=[]，不报错不补 0。
    """
    text = _get("http://127.0.0.1:{}/metrics".format(obs_port), timeout=30)
    lines = text.splitlines()
    out = {
        "ts": now_iso(),
        "obs_port": obs_port,
        "metrics_lines_total": len(lines),
        "metrics_fetched": bool(text),
        "kvc_metric_count": 0,
        "kvc_raw": [],
        "vllm": {},
        "vllm_raw": {},
    }
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        name_labels, _, val = s.rpartition(" ")
        if not name_labels or not val:
            continue
        try:
            value = float(val)
        except ValueError:
            continue
        bare = name_labels.split("{")[0]
        if bare.startswith("motor:kvc_"):
            key = bare[len("motor:"):]
            out[key] = value
            out["kvc_metric_count"] += 1
            out["kvc_raw"].append(s)
        elif bare in VLLM_KEEP:
            key = bare[len("vllm:"):]
            out["vllm"][key] = value
            out["vllm_raw"].setdefault(key, []).append(s)
    out["kvc_names"] = sorted(k for k in out if k.startswith("kvc_") and k not in
                              ("kvc_metric_count", "kvc_raw", "kvc_names"))
    return out


def snapshot(obs_port, tries=1, gap=1.5):
    """_snapshot_once 的壳子：before/after 快照必须真有 vllm 块，空的就重抓。

    为什么要有这个壳子（2026-11-13 01:51 那次踩到的）：
      pause_G_150s_ttl600_off 的 metrics_before.json 里 vllm 块是空的（0 个键），
      delta() 退化成「没有 before 就拿累计值当增量」，于是 prefill_tok 从 8,598
      变成 21,365,348，命中率跟着失真。一次瞬时的抓取失败就能把一跑的数据变成
      不可用，而 TTFT 本身是好的 —— 这种跑不该整跑作废，但 before 必须抓到手。

    采样器每秒一次走默认 tries=1，不加延迟也不改变原有行为。
    """
    tries = max(1, int(tries))
    out = None
    last_exc = None
    for i in range(tries):
        try:
            out = _snapshot_once(obs_port)
        except Exception as exc:                 # 端口抖动、连接被拒
            last_exc = exc
        else:
            last_exc = None
            if out.get("vllm"):
                return out
        if i + 1 < tries:
            time.sleep(gap)
    if last_exc is not None:
        raise last_exc
    return out


LANE_MARK = {
    "A": _env("AH_A_SHM_KEY", "ttft52113laneA"),
    "B": _env("AH_B_SHM_KEY", "ttft52113laneB"),
}
KVC_EXPECTED = 9            # life 臂应有的 kvc_* 指标个数（B 的口径：九个）

# 三臂语义（B 2026-11-12 21:05 定，A 21:14 修正后实现）：
#   life = MOTOR_CACHE_CONTROL_LIFECYCLE=1, enabled=true,  发 header  -> kvc 存在
#   decl = MOTOR_CACHE_CONTROL_LIFECYCLE=0, enabled=false, 发 header  -> kvc 不存在
#   off  = MOTOR_CACHE_CONTROL_LIFECYCLE=0, enabled=false, 不发 header -> kvc 不存在
# 之前把 decl 期望成"kvc 存在"、且 off 仍然发 header，两条都是错的。
ARM_SPEC = {
    # 2x2 拆开关版（A 2026-11-16）：
    #   hint    = 发不发 X-Agent-Hint
    #   proc_lc = 引擎侧 env MOTOR_CACHE_CONTROL_LIFECYCLE (0/1)
    #   cfg_lc  = 协调器侧 kvc_lifecycle_config.enabled（决定 kvc_* 九个指标在不在）
    #   restart = 切到该臂要跑哪个脚本（不带参数的老脚本 / 带 cfg+envlc 的 n2 脚本）
    # 老五臂语义一字未改，只是把 lifecycle 拆成 proc_lc + cfg_lc 两维。
    "life":      {"hint": True,  "proc_lc": 1, "cfg_lc": True,  "restart": "restart_night.sh on"},
    "on":        {"hint": True,  "proc_lc": 1, "cfg_lc": True,  "restart": "restart_night.sh on"},
    "life_notl": {"hint": False, "proc_lc": 1, "cfg_lc": True,  "restart": "restart_night.sh on"},
    "decl":      {"hint": True,  "proc_lc": 0, "cfg_lc": False, "restart": "restart_night.sh off"},
    "off":       {"hint": False, "proc_lc": 0, "cfg_lc": False, "restart": "restart_night.sh off"},
    # 单侧臂：kvc 管理照常开，引擎侧 lifecycle 不开（idle_offload_s=0 一动不动）
    "cfgonly":   {"hint": True,  "proc_lc": 0, "cfg_lc": True,  "restart": "restart_n2A.sh .../cfgonly.json 0"},
    # 反向单侧：引擎侧 lifecycle 开，kvc 管理关
    "envonly":   {"hint": True,  "proc_lc": 1, "cfg_lc": False, "restart": "restart_n2A.sh .../envonly.json 1"},
    # offload/prefetch 显性化臂（2026-11-16）：底座 = cfgonly（env=0, kvc 开），
    # 唯一差别是 idle_offload_s 0 -> 20（pause 60s 里 20s 处把块搬出 HBM）。
    # idle_evict_s 保持 0：校验只在两者都 >0 时才要求 evict >= offload，故合法；
    # 效果是"只搬出去、不丢"，resume 时才有东西可以 prefetch 回来。
    "offl":      {"hint": True,  "proc_lc": 0, "cfg_lc": True,  "restart": "restart_n2A.sh .../offl.json 0"},
    "offl_nopf": {"hint": True,  "proc_lc": 0, "cfg_lc": True,  "restart": "restart_n2A.sh .../offl_nopf.json 0"},
}


def arm_spec(arm, no_hint=False):
    sp = dict(ARM_SPEC.get(arm) or ARM_SPEC["life"])
    if no_hint:
        sp["hint"] = False
    return sp


def _parse_environ(raw):
    env = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        k, _, v = item.decode("utf-8", "ignore").partition("=")
        env[k] = v
    return env


def _coordinator_proc_in_container(container):
    """容器内兜底：宿主机 /proc 看不到 coordinator（PID 命名空间隔离）时，
    用 docker exec 读容器内 /proc/<pid>/environ。换机复现时这是唯一可靠路径。"""
    if not container:
        return (None, {})
    try:
        pid_out = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             "pgrep -f 'motor.coordinator.main' | head -1"],
            capture_output=True, text=True, timeout=15)
        pid = (pid_out.stdout or "").strip()
        if not pid:
            return (None, {})
        env_out = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             "tr '\\0' '\\n' < /proc/{}/environ".format(pid)],
            capture_output=True, text=True, timeout=15)
        if not (env_out.stdout or "").strip():
            return (None, {})
        raw = b"\0".join(line.encode() for line in env_out.stdout.splitlines())
        return (int(pid), _parse_environ(raw))
    except Exception:
        return (None, {})


def coordinator_proc(mark, container=None):
    """找本 lane 的 coordinator 进程，返回 (pid, env)。

    restart 脚本用 `docker exec -d -e MOTOR_CACHE_CONTROL_LIFECYCLE=1 ...` 起进程，
    那些变量只存在于**进程环境**里，容器 .Config.Env 和 `docker exec env` 都看不到。
    可靠来源是 /proc/<pid>/environ：先扫宿主机，扫不到再退回 docker exec（容器内）。
    """
    best = (None, {})
    try:
        cands = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:
        cands = []
    for d in cands:
        try:
            with open("/proc/{}/cmdline".format(d), "rb") as fp:
                cmd = fp.read().decode("utf-8", "ignore")
        except Exception:
            continue
        if "motor.coordinator.main" not in cmd:
            continue
        try:
            with open("/proc/{}/environ".format(d), "rb") as fp:
                env = _parse_environ(fp.read())
        except Exception:
            env = {}
        if mark and env.get("MOTOR_SESSION_AFFINITY_SHM_KEY") == mark:
            return int(d), env
        if best[0] is None:
            best = (int(d), env)
    if best[0] is not None and (not mark or best[1].get("MOTOR_SESSION_AFFINITY_SHM_KEY") == mark):
        return best
    cpid, cenv = _coordinator_proc_in_container(container)
    if cpid is not None:
        return cpid, cenv
    return best


def read_lifecycle_config(config_path):
    """从 coordinator json 读 kvc_lifecycle_config / agent_status_config / context_budget_mode。"""
    out = {}
    if not config_path:
        return out
    try:
        with open(config_path, encoding="utf-8") as fp:
            d = json.load(fp)
    except Exception as exc:
        return {"_error": "{}: {}".format(type(exc).__name__, exc), "_path": config_path}
    c = d.get("motor_coordinator_config") or d
    out["config_path"] = config_path
    out["kvc_lifecycle_config"] = c.get("kvc_lifecycle_config")
    out["agent_status_config"] = c.get("agent_status_config")
    out["context_budget_mode"] = c.get("context_budget_mode")
    kv = (c.get("kvc_lifecycle_config") or {})
    out["kvc_lifecycle_enabled"] = kv.get("enabled")
    out["idle_offload_s"] = kv.get("idle_offload_s")
    out["prefetch_enabled"] = kv.get("prefetch_enabled")
    return out


def motor_env_snapshot(lane):
    """返回 dict：进程 env（MOTOR_* 全量）+ 配置侧开关 + pid。"""
    pid, env = coordinator_proc(LANE_MARK.get(lane, ""), (LANES.get(lane) or {}).get("container"))
    snap = {k: v for k, v in env.items() if k.startswith("MOTOR_")}
    cfg = read_lifecycle_config(env.get("USER_CONFIG_PATH"))
    return {
        "coordinator_pid": pid,
        "coordinator_uptime_s": _uptime_of(pid),
        "env": snap,
        "config": cfg,
        "MOTOR_CACHE_CONTROL_LIFECYCLE": snap.get("MOTOR_CACHE_CONTROL_LIFECYCLE"),
        "kvc_lifecycle_enabled": cfg.get("kvc_lifecycle_enabled"),
        "context_budget_mode": cfg.get("context_budget_mode"),
        "usable": bool(snap) and not cfg.get("_error"),
    }


def _uptime_of(pid):
    if not pid:
        return None
    try:
        with open("/proc/{}/stat".format(pid), encoding="utf-8") as fp:
            fields = fp.read().split()
        start_ticks = float(fields[21])
        with open("/proc/uptime", encoding="utf-8") as fp:
            up = float(fp.read().split()[0])
        return round(up - start_ticks / 100.0, 1)
    except Exception:
        return None


def kv_usage_once(obs_port):
    text = _get("http://127.0.0.1:{}/metrics".format(obs_port), timeout=10)
    for line in text.splitlines():
        if line.startswith("vllm:kv_cache_usage_perc"):
            try:
                return float(line.rsplit(" ", 1)[1])
            except Exception:
                return None
    return None


def usage_sampler(obs_port, t0, samples, stop_evt, interval=1.0, kvc_ticks=None):
    """每秒采一次。kvc_ticks 非空时顺带把 9 个 kvc 计数一起记下来 ——
    只为回答一个 B 关心的问题：evict / offload 的突变发生在第几秒。"""
    while not stop_evt.is_set():
        t = round(time.time() - t0, 3)
        if kvc_ticks is not None:
            try:
                snap = snapshot(obs_port)
            except Exception:
                snap = None
            if snap:
                vllm = snap.get("vllm") or {}
                kv = vllm.get("kv_cache_usage_perc")
                if kv is not None:
                    samples.append([t, kv])
                kvc_ticks.append({
                    "t_s": t,
                    "kvc": {k: snap.get(k) for k in (snap.get("kvc_names") or [])},
                    "kv_usage": kv,
                    "running": vllm.get("num_requests_running"),
                    "waiting": vllm.get("num_requests_waiting"),
                    "preemptions": vllm.get("num_preemptions_total"),
                    "kvc_metric_count": snap.get("kvc_metric_count"),
                })
        else:
            v = kv_usage_once(obs_port)
            if v is not None:
                samples.append([t, v])
        stop_evt.wait(interval)


def kvc_change_points(ticks):
    """把每秒 kvc 采样压成突变点：{name: [[t_s, value], ...]}，只在值变化时记一条。
    这样 B 一眼能看到 evict 是在第几秒跳的，而不用自己扫 190 行。"""
    out = {}
    for name in (ticks[0]["kvc"].keys() if ticks else []):
        prev = None
        pts = []
        for tk in ticks:
            v = tk["kvc"].get(name)
            if v is None:
                continue
            if prev is None or v != prev:
                pts.append([tk["t_s"], v])
                prev = v
        out[name] = pts
    return out


def pause_windows(records):
    """实测暂停窗：会话内 **第一个** pause 的状态 POST 落地时刻 → resume 的状态 POST 落地时刻。

    必须取第一个 pause，不能取最后一个。依据 motor 源码
    agent_hint_lifecycle.py:286-292：
        if state == _PAUSE_STATE:
            # Repeated pause events are idempotent: only a real transition
            # into pause (or a new epoch) arms the idle timers.
            if previous != _PAUSE_STATE or epoch_changed:
                self._cancel_timers_locked(session)
                ... self._schedule_idle_locked(...)
    重复 pause 不重排 idle 定时器，定时器仍从**第一次**进入 pause 起算。
    所以 motor 眼里的空闲窗 = 第一个 pause POST → resume POST。
    （数据集里 call4 也是 pause，取最后一个会得出 ~4 ms 的假窗。）
    """
    per = {}
    for r in records:
        per.setdefault(r["session_id"], []).append(r)
    wins = {}
    for sid, rs in per.items():
        rs.sort(key=lambda r: (r["plan_ts_ms"], r["call_index"] or 0))
        pauses = [r for r in rs if r["state"] == "pause" and r.get("state_ts_ms") is not None]
        resumes = [r for r in rs if r["state"] == "resume" and r.get("resume_ts_ms") is not None]
        if not pauses or not resumes:
            continue
        wins[sid] = round(resumes[0]["resume_ts_ms"] - pauses[0]["state_ts_ms"], 1)
    return wins


def last_pause_gaps(records):
    """参考量：最后一个 pause POST → resume POST。恒接近 0（call4 的流本身吃满设计窗）。
    只用于证明"最后一个 pause 不是 idle 起点"，不参与判定。"""
    per = {}
    for r in records:
        per.setdefault(r["session_id"], []).append(r)
    out = {}
    for sid, rs in per.items():
        rs.sort(key=lambda r: (r["plan_ts_ms"], r["call_index"] or 0))
        pauses = [r for r in rs if r["state"] == "pause" and r.get("state_ts_ms") is not None]
        resumes = [r for r in rs if r["state"] == "resume" and r.get("resume_ts_ms") is not None]
        if not pauses or not resumes:
            continue
        out[sid] = round(resumes[0]["resume_ts_ms"] - pauses[-1]["state_ts_ms"], 1)
    return out


def pause_phases(records):
    """把暂停窗拆成三段，这是判断"offload 会不会打在活请求上"的唯一办法。

    数据集里每个会话有两行 pause（第 3、第 4 次调用），第二行 pause 自身带一个真请求。
    于是窗内结构固定是：
        front  = 首个 pause 的状态 POST 落地  ->  第二个 pause 行的请求 actual
        stream = 第二个 pause 行的请求 actual ->  它的状态 POST 落地（也就是最后一个 pause POST）
        tail   = 最后一个 pause POST          ->  resume 的状态 POST 落地

    motor 的 idle 定时器从首个 pause POST 起算、且不被重复 pause 重排
    （agent_hint_lifecycle.py:289），所以 idle_offload_s 到期时刻 = front 起点 + idle_offload_s：
      - 落在 front 里  → offload 发生在第二个请求之前，那个请求随后打在已 offload 的上下文上
      - 落在 tail 里   → 真正的空闲里 offload，resume 时 prefetch，这才是 P2 想要的形态
      - tail ≈ 0       → 不存在任何能落进 tail 的取值，P2 在这套数据上不可测
    """
    per = {}
    for r in records:
        per.setdefault(r["session_id"], []).append(r)
    out = {}
    for sid, rs in per.items():
        rs.sort(key=lambda r: (r["plan_ts_ms"], r["call_index"] or 0))
        pauses = [r for r in rs if r["state"] == "pause" and r.get("state_ts_ms") is not None]
        resumes = [r for r in rs if r["state"] == "resume" and r.get("resume_ts_ms") is not None]
        if not pauses or not resumes:
            continue
        p_first, p_last = pauses[0], pauses[-1]
        # 第二个 pause 行才是"空闲窗里插进来的那个请求"；第一个 pause 行自己也带请求，
        # 但它的状态 POST 落在它自己请求之后，拿它算 front 会得出负数。
        cand = [r for r in pauses[1:] if r.get("actual_ts_ms") is not None]
        if not cand:
            cand = [r for r in pauses if r.get("actual_ts_ms") is not None and r is not p_first]
        if not cand:
            continue
        req = min(cand, key=lambda r: r["actual_ts_ms"])
        out[sid] = {
            "front_ms": round(req["actual_ts_ms"] - p_first["state_ts_ms"], 1),
            "stream_ms": round(p_last["state_ts_ms"] - req["actual_ts_ms"], 1),
            "tail_ms": round(resumes[0]["resume_ts_ms"] - p_last["state_ts_ms"], 1),
            "window_ms": round(resumes[0]["resume_ts_ms"] - p_first["state_ts_ms"], 1),
        }
    return out


def design_pause_window(order):
    """设计暂停窗：会话内 首个 pause 行 → resume 行的 plan 间距。"""
    vals = []
    for rows in order.values():
        rs = sorted(rows, key=lambda r: r["_plan_ms"])
        pauses = [r for r in rs if (spec_of(r).get("agent_status") or {}).get("state") == "pause"]
        resumes = [r for r in rs if (spec_of(r).get("agent_status") or {}).get("state") == "resume"]
        if pauses and resumes:
            vals.append(resumes[0]["_plan_ms"] - pauses[0]["_plan_ms"])
    return vals[0] if vals else None


# ------------------------------------------------------------- 峰值并发
def peak_concurrency(intervals, step_ms=PEAK_STEP_MS, grid_offset_ms=0, closed_end=True):
    """100 ms 采样算活动区间重叠峰值。intervals = [(start_ms, end_ms), ...]

    与 manifest 的 _peak 同口径：t 从 grid_offset 起每 step 采样一次，
    闭区间 [start, end]（closed_end=True）时端点算在内。
    """
    if not intervals:
        return 0
    t_end = max(e for _, e in intervals)
    peak = 0
    t = grid_offset_ms
    while t <= t_end:
        n = 0
        for s, e in intervals:
            if closed_end:
                if s <= t <= e:
                    n += 1
            else:
                if s <= t < e:
                    n += 1
        if n > peak:
            peak = n
        t += step_ms
    return peak


def peak_variants(intervals):
    """四种口径都算一遍，用来确认我们和 manifest 是同一把尺子。"""
    return {
        "grid0_closed": peak_concurrency(intervals, grid_offset_ms=0, closed_end=True),
        "grid0_open": peak_concurrency(intervals, grid_offset_ms=0, closed_end=False),
        "grid50_closed": peak_concurrency(intervals, grid_offset_ms=50, closed_end=True),
        "grid50_open": peak_concurrency(intervals, grid_offset_ms=50, closed_end=False),
    }


# ---------------------------------------------------------------- 数据装载
def spec_of(row):
    """取一行的 spec 形态 hint（session / agent_status / prediction / spec cc）。

    header 通道数据集：spec 部分整体放在 row["agent_hint"]（现状，向后兼容）。

    churn 行（形态 C）：session_id 以 churn- 开头、agent_hint 里**没有 cache_control**。
    --arm decl 时它们同样会带 X-Agent-Hint，但 header 里没有保护声明（requests.jsonl
    的 cc_raw 为空），因此不会占用保护额度；它们只制造块淘汰压力。

    body   通道数据集：spec 部分放在 row["agent_hint_header"]，
                       row["agent_hint"] 只留 legacy 平铺块（session_id / ttl / msg_offset）。
    读状态机与声明一律走这里，两个通道都不会错。
    """
    return row.get("agent_hint_header") or row.get("agent_hint") or {}


def load_rows(dataset):
    path = dataset
    if not os.path.isfile(path):
        cand = os.path.join(DATA_DIR, dataset if dataset.endswith(".jsonl") else dataset + ".jsonl")
        if os.path.isfile(cand):
            path = cand
        else:
            raise SystemExit("dataset not found: {}".format(dataset))
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if not rows:
        raise SystemExit("empty dataset: {}".format(path))
    min_ts = min(r["timestamp"] for r in rows)
    for r in rows:
        r["_plan_ms"] = r["timestamp"] - min_ts
    order = {}
    for r in rows:
        sid = (spec_of(r).get("session") or {}).get("session_id")
        if not sid:
            raise SystemExit("row without session_id (need agent_hint.session or "
                             "agent_hint_header.session): {}".format(
                                 json.dumps(r, ensure_ascii=False)[:300]))
        order.setdefault(sid, []).append(r)
    for sid in order:
        order[sid].sort(key=lambda r: (r["timestamp"], r.get("kind") or ""))
    return path, rows, order, min_ts


def load_manifest():
    try:
        return json.load(open(MANIFEST, encoding="utf-8"))
    except Exception:
        return {}


def design_record(manifest, dataset_name):
    """从 manifest 找这份数据集的 _peak / _pct，容忍几种键名写法。"""
    name = os.path.basename(dataset_name).replace(".jsonl", "")
    ds = manifest.get("datasets") or []
    rec = {}
    entries = []
    if isinstance(ds, dict):          # 兼容 dict 形态
        for k, v in ds.items():
            if isinstance(v, dict):
                entries.append(dict(v, name=v.get("name") or k))
    elif isinstance(ds, list):
        entries = [e for e in ds if isinstance(e, dict)]
    for e in entries:
        if e.get("name") == name or e.get("name") == name.replace("contend_", ""):
            rec = e
            break
    if not rec:
        for e in entries:
            en = e.get("name") or ""
            if name in en or en in name:
                rec = e
                break
    peak = rec.get("_peak", rec.get("peak"))
    pct = rec.get("_pct", rec.get("pct", rec.get("kv_pct")))
    return name, peak, pct, rec


def dataset_intervals(order):
    """设计口径：会话活动区间 = [首行 plan_ms, 末行 plan_ms]。"""
    return [
        (rows[0]["_plan_ms"], rows[-1]["_plan_ms"])
        for rows in order.values()
        if rows
    ]


# ---------------------------------------------------------------- dry-run
def do_dry_run(args, path, rows, order, manifest):
    name, d_peak, d_pct, d_rec = design_record(manifest, path)
    intervals = dataset_intervals(order)
    variants = peak_variants(intervals)
    peak = variants["grid0_closed"]
    span_ms = max(r["_plan_ms"] for r in rows)
    kinds = {}
    states = {}
    for r in rows:
        kinds[r.get("kind")] = kinds.get(r.get("kind"), 0) + 1
        st = (spec_of(r).get("agent_status") or {}).get("state")
        states[st] = states.get(st, 0) + 1
    out = {
        "mode": "dry-run",
        "dataset": name,
        "path": path,
        "sessions": len(order),
        "total_requests": kinds.get("infer", 0),
        "rows_total": len(rows),
        "total_infer": kinds.get("infer", 0),
        "total_event": kinds.get("event", 0),
        "span_s": round(span_ms / 1000.0, 3),
        "peak_concurrency": peak,
        "design_peak_conc": d_peak,
        "peak_match": (d_peak == peak),
        "peak_variants": variants,
        "design_kv_pct": d_pct,
        "kind_counts": kinds,
        "state_counts": states,
        "stagger_ms": rows[1]["_plan_ms"] - rows[0]["_plan_ms"] if len(rows) > 1 else None,
        "design_pause_window_ms": design_pause_window(order),
        "manifest_keys": sorted(d_rec.keys()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


# ---------------------------------------------------------------- 真跑
# ------------------------------------------------- 预热（跨臂同态，不记入证据）
WARMUP_MAX_TOKENS = 8          # 前缀驻留由 prompt 决定，与输出长度无关，所以只出 8 token
WARMUP_SETTLE_PP = 0.5         # 池占用连续两次读数差 < 0.5 个百分点即认为稳定
WARMUP_SETTLE_MAX_S = 12.0
WARMUP_RETRY = 2               # hits 没涨时最多补发几条


def _metric_value(obs_port, name, timeout=5):
    """从引擎 obs 口抓一条 gauge 的当前值，抓不到返回 None。"""
    txt = _get("http://127.0.0.1:{}/metrics".format(obs_port), timeout=timeout)
    for line in (txt or "").splitlines():
        if line.startswith(name):
            try:
                return float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                return None
    return None


def kv_pct_now(obs_port, timeout=5):
    return _metric_value(obs_port, "vllm:kv_cache_usage_perc", timeout=timeout)


def prefix_hits_now(obs_port, timeout=5):
    return _metric_value(obs_port, "vllm:prefix_cache_hits_total", timeout=timeout)


def warmup_phase(ports, rows, order):
    """跑前把共享前缀推进池子，让各臂从同一个池状态起跑。

    两条请求，内容取自第一个会话的第一条推理行，**不带 X-Agent-Hint**
    （预热是夹具不是处理，四臂必须同态，否则暖的那一臂自带优势）。
    不写进 requests.jsonl；返回的 kv_pct_after 就是 meta.start_kv_pct。

    B 的原始要求里还有"确认 prefix_cache_hits 有增长"，这里第二条请求
    正是为此而发：第一条升温（hits 可能不涨），第二条必须命中。
    """
    out = {
        "enabled": True,
        "requests": 0,
        "max_tokens": WARMUP_MAX_TOKENS,
        "hint_sent": False,
        "prompt_tokens": None,
        "ttft_ms": [],
        "prefix_hits_before": prefix_hits_now(ports["engine"]),
        "prefix_hits_after": None,
        "hits_delta_per_req": [],
        "kv_pct_before": kv_pct_now(ports["engine"]),
        "kv_pct_after": None,
        "kv_pct_coord_after": None,
        "settle_s": None,
        "error": None,
    }
    row = None
    for sid in order:
        cand = [r for r in order[sid] if r.get("kind") != "event"]
        if cand:
            row = cand[0]
            break
    if row is None:
        out["enabled"] = False
        out["error"] = "no infer row found"
        return out
    msgs = row.get("messages")
    if isinstance(msgs, str):
        msgs = json.loads(msgs)
    tools = row.get("tools")
    tools = json.loads(tools) if isinstance(tools, str) and tools.strip() else None
    body = {
        "model": MODEL,
        "messages": msgs,
        "max_tokens": WARMUP_MAX_TOKENS,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
    for i in range(2):
        t_send = time.time()
        h0 = prefix_hits_now(ports["engine"])
        res = infer_stream(ports["infer"], body, None, timeout=120)
        out["requests"] += 1
        if res.get("prompt_tokens"):
            out["prompt_tokens"] = res["prompt_tokens"]
        if res.get("first_token_abs"):
            out["ttft_ms"].append(round((res["first_token_abs"] - t_send) * 1000, 1))
        if not res.get("ok"):
            out["error"] = "warmup req#{} http={} {}".format(i + 1, res.get("http_status"), res.get("error"))
            break
        time.sleep(0.4)
        h1 = prefix_hits_now(ports["engine"])
        if h0 is not None and h1 is not None:
            out["hits_delta_per_req"].append(h1 - h0)
    # 指标口径（2026-11-12 21:5x 实测）：
    #   · coordinator obs 口 :19477 的 vllm:* 是聚合快照，比引擎慢约 2 s
    #     （engine hits=47870592 时 coord 仍读 47869696，+2 s 才追平）⇒ 一律读引擎口。
    #   · 请求一结束 vllm:kv_cache_usage_perc 就回 0，块仍被前缀缓存持有
    #     （hits 涨、TTFT 165 ms 证明）⇒ "start_kv_pct=0" 是健康值，不是冷池。
    #   · 真正可验的是 hits 增量：6956 tok 的共享前缀 ⇒ 54 个满块 = 6912 tok。
    # 收尾读数：这个点位 /metrics 会偶发取不到（21:53 那轮四臂全是 None），
    # 重试几次；仍拿不到就用 before + Σdelta 兜底并标记 derived。
    out["prefix_hits_after"] = None
    for _ in range(5):
        out["prefix_hits_after"] = prefix_hits_now(ports["engine"])
        if out["prefix_hits_after"] is not None:
            break
        time.sleep(0.5)
    if out["prefix_hits_after"] is None and out["prefix_hits_before"] is not None:
        _dd = [x for x in out["hits_delta_per_req"] if x is not None]
        out["prefix_hits_after"] = out["prefix_hits_before"] + sum(_dd)
        out["prefix_hits_after_derived"] = True
    out["retries"] = 0
    while (out["prefix_hits_after"] is not None
           and out["prefix_hits_before"] is not None
           and out["prefix_hits_after"] <= out["prefix_hits_before"]
           and out["retries"] < WARMUP_RETRY):
        out["retries"] += 1
        res = infer_stream(ports["infer"], body, None, timeout=120)
        out["requests"] += 1
        if not res.get("ok"):
            out["error"] = "warmup retry#{} http={} {}".format(out["retries"], res.get("http_status"), res.get("error"))
            break
        time.sleep(0.8)
        out["prefix_hits_after"] = prefix_hits_now(ports["engine"])
    t_settle = time.time()
    prev, cur = None, None
    while time.time() - t_settle < WARMUP_SETTLE_MAX_S:
        cur = kv_pct_now(ports["engine"])
        if cur is not None and prev is not None and abs(cur - prev) < WARMUP_SETTLE_PP / 100.0:
            break
        prev = cur
        time.sleep(1.0)
    out["settle_s"] = round(time.time() - t_settle, 1)
    out["kv_pct_after"] = cur if cur is not None else prev
    out["kv_pct_coord_after"] = kv_pct_now(ports["obs"])
    return out


def do_run(args, path, rows, order, manifest):
    name, d_peak, d_pct, _ = design_record(manifest, path)
    ports = LANES[args.lane]
    if args.infer_port:
        ports = dict(ports, infer=args.infer_port)
    if args.mgmt_port:
        ports = dict(ports, mgmt=args.mgmt_port)
    if args.obs_port:
        ports = dict(ports, obs=args.obs_port)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = getattr(args, "run_tag", None)
    run_id = "{}_{}_{}{}".format(name, args.arm, (tag + "_") if tag else "", stamp)
    outdir = os.path.join(getattr(args, "out_root", None) or SHARED, run_id)
    os.makedirs(outdir, exist_ok=True)

    m_before = snapshot(ports["obs"], tries=5)

    # ---- 可选：运行前清引擎前缀缓存（只在引擎口上，如 :18220，不是 motor 口）----
    reset_info = None
    if args.reset_prefix_cache:
        eport = args.engine_port or ports["engine"]
        try:
            url = "http://127.0.0.1:{}/reset_prefix_cache".format(eport)
            st, body = _post(url, {}, timeout=30)
            reset_info = {"engine_port": eport, "http_status": st, "body": body[:200]}
            print("[reset-prefix-cache] :{} -> {} {}".format(eport, st, body[:120]), flush=True)
        except Exception as exc:
            reset_info = {"engine_port": eport, "error": str(exc)}
            print("[reset-prefix-cache] 失败: {}".format(exc), file=sys.stderr, flush=True)

    # ---- 臂自检：不许把臂标错（这是最容易毁掉整份证据的一步）----
    spec = arm_spec(args.arm, args.no_hint)
    send_hint = spec["hint"]
    want_proc = int(spec["proc_lc"])
    want_cfg = bool(spec["cfg_lc"])
    want_life = "proc_lc=%d cfg_lc=%s" % (want_proc, want_cfg)
    motor_env_before = motor_env_snapshot(args.lane)
    n_kvc = m_before["kvc_metric_count"]
    has_kvc = n_kvc >= KVC_EXPECTED
    proc_lc = motor_env_before.get("MOTOR_CACHE_CONTROL_LIFECYCLE")
    cfg_lc = motor_env_before.get("kvc_lifecycle_enabled")
    # 三层判据：指标在不在 / 进程 env / 配置开关。任一和 --arm 冲突就算不一致。
    checks = {
        "metrics": (n_kvc >= KVC_EXPECTED) if want_cfg else (n_kvc == 0),
        "proc_env": (str(proc_lc) == "1") if want_proc else (str(proc_lc) == "0"),
        "config": (cfg_lc is True) if want_cfg else (cfg_lc is False),
    }
    arm_mismatch = not all(checks.values())
    if arm_mismatch:
        msg = (
            "臂自检失败：--arm {} 期望 proc_lc={} cfg_lc={}（hint={}），实际 metrics={}（kvc_metric_count={}，应 {}）、"
            "proc MOTOR_CACHE_CONTROL_LIFECYCLE={}、config kvc_lifecycle_config.enabled={}。"
            "\n先 bash /home/t00887700/kv-hint/restart_laneA.sh {} 再跑。"
        ).format(
            args.arm, want_proc, want_cfg, send_hint, checks["metrics"], n_kvc,
            KVC_EXPECTED if want_cfg else 0, proc_lc, cfg_lc, spec["restart"],
        )
        print("!! " + msg, file=sys.stderr)
        if not args.allow_arm_mismatch:
            sys.exit(4)
    print(
        "[arm-check] arm={} hint={} hint_mode={} kvc_metric_count={} proc_lc={} cfg_enabled={} pid={} -> {}".format(
            args.arm, send_hint, args.hint_mode, n_kvc, proc_lc, cfg_lc,
            motor_env_before.get("coordinator_pid"),
            "OK" if not arm_mismatch else "MISMATCH",
        ),
        flush=True,
    )
    # ---- 预热：把共享前缀推进池子，让各臂从同一个池状态起跑（B 2026-11-12 21:08 的前置条件）----
    warmup = {"enabled": False}
    if getattr(args, "warmup", True):
        w_ports = dict(ports, engine=(args.engine_port or ports["engine"]))
        warmup = warmup_phase(w_ports, rows, order)
        print(
            "[warmup] req={} retry={} ttft={} prompt_tok={} hits_delta={} prefix_hits {} -> {} kv_pct(engine) {} -> {} (coord {}) settle={}s err={}".format(
                warmup.get("requests"), warmup.get("retries"), warmup.get("ttft_ms"),
                warmup.get("prompt_tokens"), warmup.get("hits_delta_per_req"),
                warmup.get("prefix_hits_before"), warmup.get("prefix_hits_after"),
                warmup.get("kv_pct_before"), warmup.get("kv_pct_after"),
                warmup.get("kv_pct_coord_after"),
                warmup.get("settle_s"), warmup.get("error"),
            ),
            flush=True,
        )
        if warmup.get("error"):
            print("!! 预热失败，起跑状态不可比: {}".format(warmup["error"]), file=sys.stderr)
            if not args.allow_arm_mismatch:
                sys.exit(5)
    start_kv_pct = warmup.get("kv_pct_after")
    samples = []
    kvc_ticks = [] if args.kvc_ticks else None
    stop_evt = threading.Event()
    t0 = time.time()
    sampler = threading.Thread(
        target=usage_sampler,
        args=(ports["obs"], t0, samples, stop_evt),
        kwargs={"kvc_ticks": kvc_ticks},
        daemon=True,
    )
    sampler.start()

    records = []
    lock = threading.Lock()

    def worker(sid, srows):
        # --anchor pause：把会话内 pause 之后的行锚在"pause 状态真正落地的时刻 + 设计间距"上。
        # 不这么做的话，16 路超载下 call3 的 pause POST 会被推理排队拖到 28 s 才发，
        # 而 call5 又不能早于 plan 23 s，两头夹 → 设计 20 s 的暂停窗实测只剩 12.8 s。
        anchor = None
        for r in srows:
            if args.anchor == "pause" and anchor is not None:
                deadline = anchor[0] + (r["_plan_ms"] - anchor[1]) / 1000.0
            else:
                deadline = t0 + r["_plan_ms"] / 1000.0
            gap = deadline - time.time()
            if gap > 0:
                time.sleep(gap)
            sent = time.time()
            hint = r["agent_hint"]          # legacy 块：body 模式下原样进 request_json.agent_hint
            spec = spec_of(r)               # spec 块：状态机 / 声明 / 记录一律读它
            sess = spec.get("session") or {}
            state = (spec.get("agent_status") or {}).get("state")
            epoch = sess.get("epoch")
            spec_cc = spec.get("cache_control") or {}
            body_cc = hint.get("cache_control") or {}
            # 记录用的"声明"：两条通道各自只有一边有，header 优先
            cc = spec_cc or body_cc
            cc_channel = "header" if spec_cc else ("body" if body_cc else None)
            # spec 通道的 cache_control 是数组（协议允许一个请求带多个 range），body 是单个 dict；
            # 记录口径统一取第一项，两种形态都能读。
            if isinstance(cc, list):
                cc = cc[0] if cc else {}
            if not isinstance(cc, dict):
                cc = {}
            rec = {
                "session_id": sid,
                "call_index": sess.get("call_index"),
                # 数据集行不写 kind 时它会是 None，而分析口径只认 kind=="infer"，
                # 于是整跑被算成 n_ok=0。这里按驱动其余处的同一套语义归一化：
                # kind=="event" 是事件，其余一律是推理。
                "kind": r.get("kind") or "infer",
                "state": state,
                "plan_ts_ms": r["_plan_ms"],
                "actual_ts_ms": round((sent - t0) * 1000, 1),
                "first_token_ts_ms": None,
                "ttft_ms": None,
                "http_status": 0,
                "completion_tokens": None,
                "ok": False,
                "epoch": epoch,
                "ttl_s": cc.get("ttl_s", cc.get("ttl")),
                "range_end": (cc.get("range") or {}).get("end"),
                "cc_raw": dict(cc),
                "cc_channel": cc_channel,
                "state_since_ms": (spec.get("agent_status") or {}).get("state_since_ms"),
                "resume_before": r.get("resume_before"),
                "total_ms": None,
                "prompt_tokens": None,
                "text_len": None,
                "chunks": None,
                "hint_b64_len": 0,
                "hint_sent": False,
                "resume_post": None,
                "state_post": None,
                "state_ts_ms": None,
                "resume_ts_ms": None,
                "error": None,
            }
            if r.get("kind") == "event":
                st, body, tried = report_state(ports, sid, state, epoch)
                rec["state_ts_ms"] = round((time.time() - t0) * 1000, 1)
                rec["http_status"] = st
                rec["state_post"] = {"status": st, "body": body, "tried": tried}
                rec["ok"] = 200 <= st < 300
                rec["latency_kind"] = "event_only"
                if not rec["ok"]:
                    rec["error"] = "agent-status {} failed: {}".format(state, body)
            else:
                if r.get("resume_before"):
                    st, body, tried = report_state(ports, sid, "resume", epoch)
                    rec["resume_ts_ms"] = round((time.time() - t0) * 1000, 1)
                    rec["resume_post"] = {"status": st, "body": body, "tried": tried}
                msgs = r.get("messages")
                if isinstance(msgs, str):
                    msgs = json.loads(msgs)
                tools = r.get("tools")
                if isinstance(tools, str) and tools.strip():
                    tools = json.loads(tools)
                else:
                    tools = None
                body_req = {
                    "model": MODEL,
                    "messages": msgs,
                    "max_tokens": r.get("max_out_len") or 256,
                    "temperature": 0,
                    "ignore_eos": True,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tools:
                    body_req["tools"] = tools
                if r.get("enable_thinking") is not None:
                    body_req["enable_thinking"] = bool(r["enable_thinking"])
                # --hint-mode body：把 cache_control 从 header 通道挪到 body 通道（legacy）。
                # 只改通道，不改声明：ttl_s=600 -> ttl=600；不写 range（body 路会 warning+忽略），
                # msg_offset 由 _finalize_cache_control 兜底成 len(messages)，与 range.end 等价。
                hdr_hint = hint
                if args.hint_mode == "body" and send_hint:
                    hdr_hint = r.get("agent_hint_header") or spec
                    body_req["agent_hint"] = _prune_none(hint)
                hint_b64 = encode_hint(hdr_hint) if send_hint else None
                rec["hint_b64_len"] = len(hint_b64) if hint_b64 else 0
                rec["hint_sent"] = bool(hint_b64)
                rec["hint_mode"] = args.hint_mode
                rec["body_hint"] = bool(args.hint_mode == "body" and send_hint)
                res = infer_stream(ports["infer"], body_req, hint_b64, timeout=args.timeout)
                rec["http_status"] = res["http_status"]
                rec["ok"] = res["ok"]
                rec["completion_tokens"] = res["completion_tokens"]
                rec["prompt_tokens"] = res["prompt_tokens"]
                rec["text_len"] = res["text_len"]
                rec["chunks"] = res["chunks"]
                rec["error"] = res["error"]
                if res["first_token_abs"]:
                    rec["first_token_ts_ms"] = round((res["first_token_abs"] - t0) * 1000, 1)
                    rec["ttft_ms"] = round(
                        (res["first_token_abs"] - sent) * 1000, 1
                    )
                rec["total_ms"] = round((res["end_abs"] - sent) * 1000, 1)
                if state in ("pause", "finalize"):
                    st, body, tried = report_state(ports, sid, state, epoch)
                    rec["state_ts_ms"] = round((time.time() - t0) * 1000, 1)
                    rec["state_post"] = {"status": st, "body": body, "tried": tried}
            if anchor is None and state == "pause" and rec.get("state_ts_ms") is not None:
                anchor = (t0 + rec["state_ts_ms"] / 1000.0, r["_plan_ms"])
            with lock:
                records.append(rec)
                n = len(records)
            if _VERBOSE or n % 16 == 0:
                print(
                    "[{:3d}/{}] {} call={} state={} ttft={} tok={} http={}".format(
                        n, len(rows), sid, rec["call_index"], state,
                        rec["ttft_ms"], rec["completion_tokens"], rec["http_status"]
                    ),
                    flush=True,
                )

    threads = [threading.Thread(target=worker, args=(sid, srows)) for sid, srows in order.items()]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.time() - t0
    stop_evt.set()
    sampler.join(timeout=5)
    m_after = snapshot(ports["obs"], tries=5)

    records.sort(key=lambda r: (r["actual_ts_ms"], r["session_id"], r["call_index"] or 0))

    # ---- 实测峰值并发：同一把尺子，但用实际时刻 ----
    spans = {}
    for rec in records:
        sid = rec["session_id"]
        end = rec["actual_ts_ms"] + (rec["total_ms"] or 0)
        if sid not in spans:
            spans[sid] = [rec["actual_ts_ms"], end]
        else:
            spans[sid][0] = min(spans[sid][0], rec["actual_ts_ms"])
            spans[sid][1] = max(spans[sid][1], end)
    actual_intervals = [(s, e) for s, e in spans.values()]
    actual_peak = peak_concurrency(actual_intervals, grid_offset_ms=0, closed_end=True)
    actual_variants = peak_variants(actual_intervals)

    inflight = [
        (r["actual_ts_ms"], r["actual_ts_ms"] + (r["total_ms"] or 0))
        for r in records
        if r["kind"] != "event"
    ]
    inflight_peak = peak_concurrency(inflight, grid_offset_ms=0, closed_end=True)

    plan_dev = max(
        (abs(r["actual_ts_ms"] - r["plan_ts_ms"]) for r in records), default=0.0
    )

    # ---- 实测暂停窗（这才是"会话真的静默了多久"，plan 间距只是设计值）----
    wins = pause_windows(records)
    win_vals = sorted(wins.values())
    d_win = design_pause_window(order)
    def _q(p):
        if not win_vals:
            return None
        k = (len(win_vals) - 1) * p
        lo = int(k); hi = min(lo + 1, len(win_vals) - 1)
        return round(win_vals[lo] + (win_vals[hi] - win_vals[lo]) * (k - lo), 1)
    try:
        idle_offload_s = float((motor_env_before.get("config") or {}).get("idle_offload_s") or 30.0)
    except Exception:
        idle_offload_s = 30.0
    lastg = last_pause_gaps(records)
    lastg_vals = sorted(lastg.values())
    phases = pause_phases(records)

    def _lands_in():
        """idle_offload_s 到期那一刻落在窗的哪一段：front（第二个请求之前，会打在活请求上）/ tail（真空闲）/ never（窗内到不了期）"""
        idle_ms = idle_offload_s * 1000.0
        win = _ph("window_ms")
        f_, s_ = _ph("front_ms"), _ph("stream_ms")
        if win is None or f_ is None or s_ is None:
            return None
        if idle_ms >= win:
            return "never"
        return "tail" if idle_ms > (f_ + s_) else "front"
    def _ph(key):
        v = sorted(p[key] for p in phases.values() if p.get(key) is not None)
        return v[(len(v) - 1) // 2] if v else None
    win_summary = {
        "definition": "第一个 pause 的状态 POST 落地 -> resume 的状态 POST 落地（motor 不在重复 pause 上重排 idle 定时器，见 agent_hint_lifecycle.py:289）",
        "design_ms": d_win,
        "realized_min_ms": win_vals[0] if win_vals else None,
        "realized_p50_ms": _q(0.5),
        "realized_max_ms": win_vals[-1] if win_vals else None,
        "n_sessions": len(win_vals),
        "idle_offload_s": idle_offload_s,
        "over_idle_offload_sessions": sum(1 for v in win_vals if v > idle_offload_s * 1000.0),
        "front_p50_ms": _ph("front_ms"),
        "stream_p50_ms": _ph("stream_ms"),
        "tail_p50_ms": _ph("tail_ms"),
        "tail_max_ms": (max((p["tail_ms"] for p in phases.values()), default=None)),
        "phases_per_session": phases,
        "offload_lands_in": _lands_in(),
        "last_pause_to_resume_p50_ms": (lastg_vals[(len(lastg_vals) - 1) // 2] if lastg_vals else None),
        "per_session_ms": wins,
    }

    # ---- meta ----
    motor_env_after = motor_env_snapshot(args.lane)
    lc_before = motor_env_before.get("MOTOR_CACHE_CONTROL_LIFECYCLE")
    lc_after = motor_env_after.get("MOTOR_CACHE_CONTROL_LIFECYCLE")
    pid_before = motor_env_before.get("coordinator_pid")
    pid_after = motor_env_after.get("coordinator_pid")
    arm_consistent = (lc_before == lc_after) and (pid_before == pid_after)

    kv_vals = [v for _, v in samples]
    http_errors = sum(1 for r in records if not r["ok"])
    infer_recs = [r for r in records if r["kind"] != "event"]
    meta = {
        # --- B 要的必填字段 ---
        "run_id": run_id,
        "dataset": name,
        "arm": args.arm,
        "repeat": args.repeat,
        "design_peak_conc": d_peak,
        "actual_peak_conc": actual_peak,
        "design_kv_pct": d_pct,
        "run_start_ts": datetime.fromtimestamp(t0).astimezone().isoformat(timespec="seconds"),
        "run_end_ts": now_iso(),
        "total_requests": len(infer_recs),
        "http_errors": http_errors,
        "driver_sha256": sha256_file(os.path.abspath(__file__)),
        # motor_env = coordinator 进程里 MOTOR_* 全量（restart_laneA.sh 是 docker exec -d -e 起的，
        # 只有 /proc/<pid>/environ 里看得到，容器 .Config.Env 永远看不到）
        "motor_env": motor_env_before.get("env", {}),
        "motor_env_after": motor_env_after.get("env", {}),
        "coordinator_pid_before": motor_env_before.get("coordinator_pid"),
        "coordinator_pid_after": motor_env_after.get("coordinator_pid"),
        "coordinator_uptime_s": motor_env_before.get("coordinator_uptime_s"),
        "motor_config_path": (motor_env_before.get("config") or {}).get("config_path"),
        "motor_config": motor_env_before.get("config") or {},
        "arm_consistent": arm_consistent,
        "arm_selfcheck_ok": (not arm_mismatch),
        "arm_checks": checks,
        "hint_sent": send_hint,
        "anchor_mode": args.anchor,
        "prefix_cache_reset": reset_info,
        "kvc_metric_count_before": m_before["kvc_metric_count"],
        "pause_window": win_summary,
        # --- 附加 ---
        "dataset_file": path,
        "dataset_sha256": sha256_file(path),
        "lane": args.lane,
        "ports": ports,
        "rows_total": len(records),
        "rows_infer": len(infer_recs),
        "rows_event": len(records) - len(infer_recs),
        "sessions": len(order),
        "span_s": round(max((r["actual_ts_ms"] for r in records), default=0.0) / 1000.0, 3),
        "wall_s": round(wall, 3),
        "in_flight_peak_conc": inflight_peak,
        "actual_peak_variants": actual_variants,
        "max_plan_deviation_ms": plan_dev,
        "peak_match": (d_peak == actual_peak),
        "kv_cache_usage_perc_max": max(kv_vals) if kv_vals else None,
        "kv_cache_usage_perc_last": kv_vals[-1] if kv_vals else None,
        "kv_cache_usage_samples": samples,
        "metrics_before_kvc_metric_count": m_before["kvc_metric_count"],
        "metrics_after_kvc_metric_count": m_after["kvc_metric_count"],
        "warmup": warmup,
        "start_kv_pct": start_kv_pct,
        "host": os.uname().nodename,
        "driver_version": "v3-anchor-pause-kvtick",
        "note": "actual_peak_conc 与 design_peak_conc 同一口径（会话活动区间 100 ms 采样，闭区间，grid=0）",
    }
    if kvc_ticks is not None:
        meta["kvc_ticks"] = kvc_ticks
        meta["kvc_change_points"] = kvc_change_points(kvc_ticks)

    # ---- 落盘 ----
    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, indent=2, ensure_ascii=False)
    with open(os.path.join(outdir, "requests.jsonl"), "w", encoding="utf-8") as fp:
        for r in records:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(outdir, "metrics_before.json"), "w", encoding="utf-8") as fp:
        json.dump(m_before, fp, indent=2, ensure_ascii=False)
    with open(os.path.join(outdir, "metrics_after.json"), "w", encoding="utf-8") as fp:
        json.dump(m_after, fp, indent=2, ensure_ascii=False)

    # ---- 控制台摘要（不下性能结论，只报事实计数）----
    kvc_delta = {}
    for k in m_before.get("kvc_names", []):
        kvc_delta[k] = round(m_after.get(k, 0.0) - m_before.get(k, 0.0), 3)
    print(json.dumps({
        "run_id": run_id,
        "evidence_dir": outdir,
        "rows_infer": meta["rows_infer"],
        "rows_event": meta["rows_event"],
        "http_errors": http_errors,
        "design_peak_conc": d_peak,
        "actual_peak_conc": actual_peak,
        "in_flight_peak_conc": inflight_peak,
        "max_plan_deviation_ms": plan_dev,
        "wall_s": meta["wall_s"],
        "kvc_before_count": m_before["kvc_metric_count"],
        "kvc_after_count": m_after["kvc_metric_count"],
        "kvc_delta": kvc_delta,
        "arm_consistent": arm_consistent,
        "hint_sent": send_hint,
        "anchor_mode": args.anchor,
        "pause_window": win_summary,
        "coordinator_pid": pid_before,
        "kv_cache_usage_perc_max": meta["kv_cache_usage_perc_max"],
    }, ensure_ascii=False, indent=2))
    return meta


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="并发回放驱动（绝对时刻调度）")
    ap.add_argument("--dataset", required=True, help="数据集名（如 contend_real_S16）或 jsonl 路径")
    ap.add_argument("--arm", default="life", choices=sorted(ARM_SPEC),
                    help="三臂语义：life=生命周期开+发header / decl=生命周期关+发header / off=关+不发header")
    ap.add_argument("--repeat", type=int, default=1, help="重复序号，只做标注")
    ap.add_argument("--hint-mode", default="header", choices=["header", "body"],
                    help="cache_control 走哪条通道：header=X-Agent-Hint(spec, 默认)；"
                         "body=request_json.agent_hint(legacy)。body 模式下 header 仍发 "
                         "session/agent_status/prediction（这三个只有 header 路能被解析），"
                         "只把 cache_control 挪进 body：平铺 session_id + {type:ephemeral, ttl}。")
    ap.add_argument("--no-hint", action="store_true",
                    help="完全不发 X-Agent-Hint（off 臂本来就不发，这个开关用于临时排除 hint 影响）")
    ap.add_argument("--anchor", default="plan", choices=["plan", "pause"],
                    help="plan=按绝对时刻发（默认，与既有数据可比）；"
                         "pause=pause 之后的行锚在实测 pause 落地时刻+设计间距（保暂停窗精度，让绝对相位漂）")
    ap.add_argument("--reset-prefix-cache", action="store_true",
                    help="运行前 POST 引擎口 /reset_prefix_cache（默认 :18220），清前缀缓存，结果记进 meta.prefix_cache_reset")
    ap.add_argument("--engine-port", type=int, default=None, help="引擎口，默认 18220（laneA）/ 18230（laneB）")
    ap.add_argument("--kvc-ticks", action="store_true",
                    help="每秒连 9 个 kvc 计数一起记（meta 里多出 kvc_ticks / kvc_change_points，用来定位突变到秒）")
    ap.add_argument("--warmup", dest="warmup", action="store_true", default=True,
                    help="跑前预热（默认开）：两条不带 header 的请求把共享前缀推进池子，"
                         "等池占用稳定后起跑，结果记 meta.warmup / meta.start_kv_pct")
    ap.add_argument("--no-warmup", dest="warmup", action="store_false",
                    help="跳过预热（只在排障或对比旧证据时用；跨臂不可比）")
    ap.add_argument("--lane", default="A", choices=sorted(LANES))
    ap.add_argument("--infer-port", type=int, default=None)
    ap.add_argument("--mgmt-port", type=int, default=None)
    ap.add_argument("--obs-port", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out-root", default=None,
                    help="证据输出根目录（默认取 AH_EVIDENCE_ROOT，再回退到包内 results/）")
    ap.add_argument("--run-tag", default=None,
                    help="证据目录名附加标签（如 L2r1），便于脚本精确绑定 evidence_dir")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-arm-mismatch", action="store_true",
                    help="臂自检不过也照跑（只在排障时用；跑出来的证据不能用于对比）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    global _VERBOSE
    _VERBOSE = args.verbose

    path, rows, order, min_ts = load_rows(args.dataset)
    manifest = load_manifest()

    if args.dry_run:
        out = do_dry_run(args, path, rows, order, manifest)
        bad = [k for k, v in out["peak_variants"].items() if v != out["design_peak_conc"]]
        if out["design_peak_conc"] is None:
            print("!! manifest 里找不到 {} 的 _peak".format(out["dataset"]), file=sys.stderr)
            sys.exit(3)
        if not out["peak_match"]:
            print(
                "!! peak 不匹配: 实测 {} vs manifest {}. 四种口径: {}".format(
                    out["peak_concurrency"], out["design_peak_conc"],
                    json.dumps(out["peak_variants"]),
                ),
                file=sys.stderr,
            )
            other = [k for k, v in out["peak_variants"].items() if v == out["design_peak_conc"]]
            if other:
                print("   注意：口径 {} 与 manifest 一致".format(other), file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    meta = do_run(args, path, rows, order, manifest)
    sys.exit(0 if meta["http_errors"] == 0 else 1)


if __name__ == "__main__":
    main()
