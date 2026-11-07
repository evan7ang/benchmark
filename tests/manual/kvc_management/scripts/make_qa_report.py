#!/usr/bin/env python3
"""Build a single QA-friendly Markdown/HTML report from a qa_run manifest."""

from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime


def load_manifest(run_dir: str) -> tuple[dict, list[dict]]:
    meta_path = os.path.join(run_dir, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
    cases: list[dict] = []
    manifest = os.path.join(run_dir, "manifest.jsonl")
    if os.path.exists(manifest):
        for line in open(manifest, encoding="utf-8"):
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return meta, cases


def summarize_result(entry: dict) -> dict:
    result_dir = entry.get("result_dir", "")
    summary_path = os.path.join(result_dir, "summary.json")
    summary = json.load(open(summary_path, encoding="utf-8")) if os.path.exists(summary_path) else {}
    details = ((summary.get("aisbench") or {}).get("details") or {})
    posts = ((summary.get("aisbench") or {}).get("agent_status_posts") or {})
    motor = summary.get("motor_kvc_delta") or {}
    edits = summary.get("engine_edits") or {}
    metrics = summary.get("engine_metrics") or {}
    kv = metrics.get("kv_cache_usage_perc") or {}
    wait = metrics.get("num_requests_waiting") or {}
    preempt = metrics.get("num_preemptions_total") or {}

    nonzero_motor = {k: int(v) for k, v in motor.items() if v}
    edit_counts = edits.get("counts") or {}
    blocks = int(edits.get("actual_op_block_num_sum") or 0)

    run_rc_path = os.path.join(result_dir, "run_rc.txt")
    run_rc = None
    if os.path.exists(run_rc_path):
        try:
            run_rc = int(open(run_rc_path).read().strip())
        except ValueError:
            run_rc = None

    observed = []
    if details and details.get("rows", 0) > 0:
        observed.append(f"请求 {details.get('ok', 0)}/{details.get('rows', 0)} 成功")
    elif run_rc == 137:
        observed.append("请求统计：定时截断，未生成完整 details（只看压力/KVC 指标）")
    if posts:
        observed.append(
            "status: "
            + ", ".join(f"{k}={v}" for k, v in sorted(posts.items()))
        )
    if nonzero_motor:
        observed.append("KVC: " + ", ".join(f"{k}={v}" for k, v in sorted(nonzero_motor.items())))
    elif motor:
        observed.append("KVC 计数：全部为 0")
    if edit_counts:
        observed.append(
            "engine: " + ", ".join(f"{k}={v}" for k, v in sorted(edit_counts.items())) + f", blocks={blocks}"
        )
    elif edits:
        observed.append("engine edit：0")
    if kv:
        observed.append(
            f"KV p50={kv.get('median', 0) * 100:.1f}% max={kv.get('max', 0) * 100:.1f}%"
            f", Waiting={int(wait.get('max', 0))}"
            f", preemptions={int(preempt.get('delta', 0))}"
        )
    if not observed:
        observed.append("无可用观测数据")

    return {
        "case_id": entry.get("case_id", "?"),
        "name": entry.get("name", ""),
        "expected": entry.get("expected", ""),
        "result_dir": result_dir,
        "pass": bool(summary.get("pass", False)),
        "reasons": summary.get("reasons") or [],
        "observed": observed,
        "summary": summary,
    }


def render_markdown(meta: dict, rows: list[dict]) -> str:
    total = len(rows)
    passed = sum(1 for row in rows if row["pass"])
    overall = "PASS" if total and passed == total else "FAIL"
    lines = [
        "# KVC 管理功能手工测试报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 测试模式：{meta.get('mode', 'unknown')}",
        f"- 隔离栈：卡 {meta.get('card', '?')}，engine :{meta.get('engine_port', '?')}，"
        f"Motor :{meta.get('motor_ports', '?')}",
        f"- 用例：{passed}/{total} 通过",
        "",
        f"## 总结论：**{overall}**",
        "",
        "| 用例 | 目的 | 结果 | 关键观测 | 期望 | 证据 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        status = "PASS" if row["pass"] else "**FAIL**"
        evidence = os.path.basename(row["result_dir"]) if row["result_dir"] else "-"
        lines.append(
            f"| {row['case_id']} | {row['name']} | {status} | "
            f"{'<br>'.join(row['observed'])} | {row['expected']} | `{evidence}` |"
        )

    lines += ["", "## 逐项详情", ""]
    for row in rows:
        lines += [
            f"### {row['case_id']} {row['name']}",
            "",
            f"- 结果：**{'PASS' if row['pass'] else 'FAIL'}**",
            f"- 期望：{row['expected']}",
        ]
        for item in row["observed"]:
            lines.append(f"- 观测：{item}")
        if row["reasons"]:
            lines.append("- 失败原因：" + "; ".join(row["reasons"]))
            lines.append(
                "- 排查建议：先看该目录下 `summary.txt`、`coordinator-kvc.log`、"
                "`engine-kvc.log`；确认 Motor 是否在每轮前重启、KV 是否压满、"
                "是否出现 `no session pin` 或 `gate_skip`。"
            )
        lines.append(f"- 证据目录：`{row['result_dir']}`")
        lines.append("")

    lines += [
        "## 人工观察入口",
        "",
        "```bash",
        "# 实时观察 KV usage / Waiting / KVC 计数",
        "./qa_run.sh observe",
        "",
        "# 单看某个用例的自动结论",
        "cat <证据目录>/summary.txt",
        "```",
        "",
        "## 判定口径",
        "",
        "- S1：KV usage max ≥70%，或 Waiting>0，或出现 preemption。",
        "- S2：finalize 数 = evict 数；无 offload/prefetch；`edit_fail=0`。",
        "- S3：offload>0，prefetch>0 且 ≤ offload；`edit_fail=0`。",
        "- S5：KV 压满且 offload>0；`gate_skip` 可为非 0（自限正常）。",
        "- S6：`/agent-status` 200；KVC 计数与 engine edit 全 0。",
        "- S7：`gate_skip>0`，offload=0、evict=0。",
        "",
    ]
    return "\n".join(lines)


def render_html(meta: dict, rows: list[dict]) -> str:
    total = len(rows)
    passed = sum(1 for row in rows if row["pass"])
    overall = "PASS" if total and passed == total else "FAIL"
    color = "#0a7f3f" if overall == "PASS" else "#b42318"
    body = []
    for row in rows:
        status = "PASS" if row["pass"] else "FAIL"
        badge = "#0a7f3f" if row["pass"] else "#b42318"
        reasons = "<br>".join(html.escape(r) for r in row["reasons"])
        body.append(
            "<tr>"
            f"<td>{html.escape(row['case_id'])}</td>"
            f"<td>{html.escape(row['name'])}</td>"
            f'<td><b style="color:{badge}">{status}</b></td>'
            f"<td>{'<br>'.join(html.escape(x) for x in row['observed'])}</td>"
            f"<td>{html.escape(row['expected'])}</td>"
            f"<td><code>{html.escape(os.path.basename(row['result_dir']))}</code>"
            + (f"<br><small>{reasons}</small>" if reasons else "")
            + "</td></tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>KVC 管理功能手工测试报告</title>
<style>
body{{font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;margin:32px;color:#1f2328}}
table{{border-collapse:collapse;width:100%;margin:16px 0}}
th,td{{border:1px solid #d0d7de;padding:8px 10px;vertical-align:top;font-size:14px}}
th{{background:#f6f8fa;text-align:left}}
h1{{margin-bottom:4px}} .meta{{color:#57606a;font-size:14px;margin-bottom:16px}}
.overall{{font-size:22px;font-weight:700;color:{color};margin:12px 0}}
code{{background:#f6f8fa;padding:1px 4px;border-radius:3px}}
</style></head><body>
<h1>KVC 管理功能手工测试报告</h1>
<div class="meta">生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ·
模式：{html.escape(str(meta.get('mode', 'unknown')))} ·
卡 {html.escape(str(meta.get('card', '?')))} ·
engine :{html.escape(str(meta.get('engine_port', '?')))}</div>
<div class="overall">总结论：{overall}（{passed}/{total} 通过）</div>
<table><thead><tr>
<th>用例</th><th>目的</th><th>结果</th><th>关键观测</th><th>期望</th><th>证据</th>
</tr></thead><tbody>
{''.join(body)}
</tbody></table>
<p>实时观察：<code>./qa_run.sh observe</code></p>
</body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    meta, entries = load_manifest(args.run_dir)
    rows = [summarize_result(entry) for entry in entries]
    md = render_markdown(meta, rows)
    html_text = render_html(meta, rows)
    md_path = os.path.join(args.run_dir, "QA_REPORT.md")
    html_path = os.path.join(args.run_dir, "QA_REPORT.html")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(md)
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    latest = os.path.join(os.path.dirname(args.run_dir), "QA_REPORT_latest.md")
    with open(latest, "w", encoding="utf-8") as handle:
        handle.write(md)
    latest_html = os.path.join(os.path.dirname(args.run_dir), "QA_REPORT_latest.html")
    with open(latest_html, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    print(md_path)
    print(html_path)


if __name__ == "__main__":
    main()
