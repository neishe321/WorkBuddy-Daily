#!/usr/bin/env python3
"""daily_forward.py — 把国内版 WorkBuddy Daily 的签到结果转发到微信（WxPusher）。

适用场景
--------
主脚本 workbuddy_daily.py 已内置 WxPusher（配置环境变量即启用）。
本脚本用于按需转发的场景：在 Actions 跑完签到后，读取运行日志重建「签到报告」，
再单独推送到 WxPusher / Bark；适合临时补发，或已有日志想统一转发的用法。

用法
----
  gh run view <RUN_ID> --log > /tmp/daily.log
  python3 daily_forward.py /tmp/daily.log                    # 仅打印
  python3 daily_forward.py /tmp/daily.log --wx                # 推送 WxPusher（BARK_URL 兜底）
  python3 daily_forward.py /tmp/daily.log --wx --wx-title "🌱 国内签到报告"

日志行形如：`Run WorkBuddy\tRun WorkBuddy\t2026-10-12T22:34:07Z [06:32:48][账号1] 💰 积分: ...`
"""
import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from credits_report import bark, wxpusher  # noqa: E402

# 每步日志行：<step>\t<step>\t<ISO时间戳> <内容>
LINE = re.compile(r"^[^\t]*\t[^\t]*\t\d{4}-\d\d-\d\dT[\d:.]+Z ?(.*)$")
# 脚本输出行：[06:32:48][账号1] 内容
ACC = re.compile(r"^\[[\d:]+\]\s*\[账号(\d+)\]\s*(.*)$")
# 需要保留的关键行（其余任务明细过于啰嗦）
KEEP = ("💰 积分:", "🌱 成长:", "签到成功", "签到失败", "🏁", "🎰抽奖", "📦盲盒",
        "🐾旅行", "🏅徽章", "🩹", "🎁 ── 领奖", "无待领奖励", "已领取", "✅", "❌", "⚠️")


def parse_log(text):
    """→ {账号序号: [行...]}（按出现顺序）"""
    accounts, order = {}, []
    for raw in text.splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        body = m.group(1).strip()
        am = ACC.match(body)
        if not am:
            continue
        idx, content = am.group(1), am.group(2).strip()
        if idx not in accounts:
            accounts[idx] = []
            order.append(idx)
        # 关键行保留原文；任务明细行丢弃（避免报告过长）
        if any(k in content for k in KEEP) and "已领取，跳过" not in content:
            # 去掉重复前缀的 ── xxx ── 分节标题，保留有信息量的
            accounts[idx].append(content)
    return accounts, order


def build_report(accounts, order):
    now = datetime.datetime.now().strftime("%m-%d %H:%M")
    lines = ["🌱 WorkBuddy 签到报告（%s）" % now]
    for idx in order:
        rows = accounts[idx]
        if not rows:
            continue
        # 轻量清理：去掉内部任务码、把「🎁 领奖：」与其后续行合并
        merged = []
        for r in rows:
            if merged and merged[-1].endswith("领奖："):
                merged[-1] += r
                continue
            r = re.sub(r"\bwb_[a-z0-9_]+\s+", "", r)
            merged.append(r.replace("🎁 ── 领奖 ──", "🎁 领奖："))
        rows = merged
        # 账号名从 🏁 行里取（已脱敏）
        name = ""
        for r in rows:
            mm = re.search(r"🏁\s*(\S+?):", r)
            if mm:
                name = mm.group(1)
        lines.append("")
        lines.append("👤 账号%s%s" % (idx, "  " + name if name else ""))
        for r in rows:
            lines.append("   " + r)
    if len(lines) == 1:
        lines.append("")
        lines.append("（未从日志中提取到签到信息，请到 Actions 查看）")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", help="gh run view --log 的输出文件")
    ap.add_argument("--wx", action="store_true", help="推送 WxPusher（读取 WXPUSHER_APP_TOKEN / WXPUSHER_UID）")
    ap.add_argument("--bark", action="store_true", help="推送 Bark（读取 BARK_URL）")
    ap.add_argument("--wx-title", default="🌱 国内签到报告")
    ap.add_argument("--bark-url", default=os.environ.get("BARK_URL", "").strip())
    args = ap.parse_args()

    try:
        text = open(args.logfile, encoding="utf-8", errors="replace").read()
    except Exception as e:
        print("读取日志失败: %s" % e)
        raise SystemExit(1)

    accounts, order = parse_log(text)
    report = build_report(accounts, order)
    print(report)

    if args.wx or args.bark:
        sent = False
        if args.wx:
            sent = wxpusher(os.environ.get("WXPUSHER_APP_TOKEN", "").strip(),
                            os.environ.get("WXPUSHER_UID", "").strip(),
                            args.wx_title, report)
            if not sent and args.bark_url:
                print("↩️ WxPusher 未生效，回落到 Bark")
        if not sent and args.bark_url:
            bark(args.bark_url, args.wx_title, report)


if __name__ == "__main__":
    main()
