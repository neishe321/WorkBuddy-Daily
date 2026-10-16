#!/usr/bin/env python3
"""credits_report.py — 积分余额 + 到期时间报告（国内/国际版共用）

数据来源（均为只读）：
  · 余额口径：POST /billing/meter/get-user-resource-summary
              → data.Packages[].CycleRemainCapacity 之和（与 App 顶部"剩余积分"一致）
  · 到期口径：POST /billing/meter/get-user-resource
              → data.Response.Data.Accounts[]，每笔积分带 CycleStartTime/CycleEndTime

积分分两种，规则不同：
  · CapacityType=4  月度套餐：每月 1 日重置，当月最后一天 23:59:59 清零，不结转
  · CapacityType=1  活动赠送：签到/任务逐笔发放，领取后 30 天有效，逐笔独立到期

用法：
  python3 credits_report.py --cn  [--store wb_refresh_tokens.json] [--out FILE] [--num-out FILE]
  python3 credits_report.py --intl "备注|AT|uid|RT" [--out FILE] [--num-out FILE]

推送（二选一或同时）：
  --bark              推送 Bark（iOS），读取 BARK_URL / --bark-url
  --wx                推送 WxPusher（微信），读取 WXPUSHER_APP_TOKEN / WXPUSHER_UID
                       （UID 多个时用逗号/分号/空格分隔）
  --wx 在 WxPusher 未配置或推送失败时，会自动回落到 BARK_URL（若已配置），
  保证渠道切换期间通知不中断。环境变量与对应参数等效。
预警窗口默认 7 天，可用 --days N 调整。
"""
import argparse
import base64
import datetime
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

UA = "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/2.137.1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- 基础工具
def post(url, token, extra_headers=None):
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
        "X-IDE-Type": "WorkBuddy", "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.5.2", "X-Product": "WorkBuddy",
        **(extra_headers or {}),
    })
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def refresh_cn(rt):
    """国内版 RT 刷新（口径同 workbuddy_daily.py refresh_one）→ (新AT, 新RT) 或 (None, 错误)"""
    req = urllib.request.Request(
        "https://copilot.tencent.com/v2/plugin/auth/token/refresh",
        data=b"{}", method="POST", headers={
            "X-Refresh-Token": rt, "X-Auth-Refresh-Source": "plugin",
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        d = json.loads(resp.read().decode("utf-8", "replace"))
    inner = d.get("data") or {}
    if d.get("code") == 0 and inner.get("accessToken"):
        return inner["accessToken"], inner.get("refreshToken") or rt
    return None, str(d.get("msg", ""))[:80]


def refresh_intl(rt):
    """国际版 RT 刷新（口径同 workbuddy_intl_daily.py）→ (新AT, 新RT) 或 (None, 错误)"""
    req = urllib.request.Request(
        "https://www.workbuddy.ai/v2/plugin/auth/token/refresh",
        data=b"{}", method="POST", headers={
            "X-Refresh-Token": rt, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        d = json.loads(resp.read().decode("utf-8", "replace"))
    inner = d.get("data") or d
    if isinstance(inner, dict) and inner.get("accessToken"):
        return inner["accessToken"], inner.get("refreshToken") or rt
    return None, str(d.get("msg", d))[:80]


def jwt_user(tok):
    try:
        pay = tok.split(".")[1]
        pay += "=" * (4 - len(pay) % 4)
        return json.loads(base64.urlsafe_b64decode(pay)).get("preferred_username", "?")
    except Exception:
        return "?"


def jwt_claims(tok):
    """解析 JWT payload（不校验签名），失败返回 {}"""
    try:
        pay = tok.split(".")[1]
        pay += "=" * (4 - len(pay) % 4)
        return json.loads(base64.urlsafe_b64decode(pay)) or {}
    except Exception:
        return {}


def account_label(tok, fallback=""):
    """账号标识：JWT 的 preferred_username（登录名，如 wuge0）+ 显示名 name（如 Aaron）。
    两者都有且不同时输出「wuge0（Aaron）」；缺失时依次回落 name、token 串备注。"""
    c = jwt_claims(tok)
    user = str(c.get("preferred_username") or "").strip()
    name = str(c.get("name") or "").strip()
    if user and name and name != user:
        return "%s（%s）" % (user, name)
    return user or name or (fallback or "").strip()


def mask_phone(s):
    """手机号脱敏：隐藏中间 7 位，保留前 2 后 2（如 17********02）。
    非 11 位数字原样返回（账号名/昵称等不受影响）。与主脚本 workbuddy_daily.py 同口径。"""
    s = str(s).strip()
    if len(s) == 11 and s.isdigit():
        return s[:2] + "*******" + s[-2:]
    return s


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 到期解析
def parse_end(text):
    """'2026-10-31 23:59:59' → datetime；失败返回 None"""
    try:
        return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_accounts(base, token):
    """到期明细（Accounts 数组）；HTTP 401 抛给上层走刷新重试"""
    _, body = post(base + "/billing/meter/get-user-resource", token)
    return ((body.get("data") or {}).get("Response", {}).get("Data", {}) or {}).get("Accounts") or []


def fetch_summary(base, token):
    _, body = post(base + "/billing/meter/get-user-resource-summary", token)
    return body.get("data") or {}


def expiry_line(accounts, now, days=7):
    """把 Accounts 解析成一行到期播报 + 可选预警行。

    按到期时刻分桶（月包与赠送积分同刻到期则合并），输出：
      ⏰ 10-24 到期 382 分（11 天后）｜10-31 月包清零 500｜11-12 全到期
    窗口（days）内有到期时，额外返回预警行。
    """
    live = [a for a in accounts if f(a.get("CycleCapacityRemain")) > 0 and parse_end(a.get("CycleEndTime"))]
    if not live:
        return "", ""
    live.sort(key=lambda a: parse_end(a["CycleEndTime"]))
    horizon = now + datetime.timedelta(days=days)

    buckets = []  # [end_dt, remain_sum, is_monthly]
    for a in live:
        dt = parse_end(a["CycleEndTime"])
        if buckets and buckets[-1][0] == dt:
            buckets[-1][1] += f(a.get("CycleCapacityRemain"))
            if str(a.get("CapacityType")) == "4":
                buckets[-1][2] = True
        else:
            buckets.append([dt, f(a.get("CycleCapacityRemain")), str(a.get("CapacityType")) == "4"])

    def seg(b):
        dt, v, monthly = b
        d_left = (dt.date() - now.date()).days
        when = "今天" if d_left <= 0 else "%d 天后" % d_left
        tag = "月包清零" if monthly else "到期"
        return "%s %s %g 分（%s）" % (dt.strftime("%m-%d"), tag, v, when)

    soon = [b for b in buckets if b[0] <= horizon]
    later = [b for b in buckets if b[0] > horizon]

    parts = [seg(b) for b in soon[:2]]
    if len(soon) > 2:
        parts.append("%d 天内共 %d 批 %g 分" % (days, len(soon), sum(b[1] for b in soon)))
    shown = [b[0] for b in soon[:2]]
    if later:
        uniq = []
        nxt = next((b for b in later if not b[2]), None)  # 窗口外最近的非月包批
        if nxt and nxt[0] not in shown:
            uniq.append(nxt)
        for b in later:                                   # 所有月包批
            if b[2] and b[0] not in shown and b[0] not in [u[0] for u in uniq]:
                uniq.append(b)
        uniq.sort(key=lambda b: b[0])
        for b in uniq[:2]:
            parts.append(seg(b))
            shown.append(b[0])
        last = later[-1]
        if last[0] not in shown:
            parts.append("%s 全到期" % last[0].strftime("%m-%d"))
    line = "⏰ " + "｜".join(parts)

    warn = ""
    if soon:
        soon_sum = sum(b[1] for b in soon)
        first = soon[0][0]
        d_left = (first.date() - now.date()).days
        when = "今天" if d_left <= 0 else "%d 天后" % d_left
        warn = "⚠️ 积分到期预警：%g 分将在 %d 天内失效（最早 %s，%s），签到/任务别断" % (
            soon_sum, days, first.strftime("%m-%d"), when)
    return line, warn


# ---------------------------------------------------------------- 报告组装
def report_cn(store_path, days, verbose=True):
    """国内版：多账号，逐个查余额+到期；AT 失效自动 RT 刷新重试"""
    now = datetime.datetime.now()
    try:
        store = json.load(open(store_path, encoding="utf-8"))
    except Exception as e:
        return "💰 国内积分 查询失败: %s" % str(e)[:60], []
    if not isinstance(store, dict) or not store:
        return "💰 国内积分 —（无账号，wb_refresh_tokens.json 为空）", []

    lines, warns, total_all = [], [], 0.0
    for user, ent in store.items():
        at = (ent or {}).get("access_token", "")
        rt = (ent or {}).get("refresh_token", "")
        label = mask_phone(user)
        try:
            try:
                accounts = fetch_accounts("https://copilot.tencent.com", at)
            except urllib.error.HTTPError as e:
                if e.code != 401 or not rt:
                    raise
                if verbose:
                    print("   🔄 %s AT 失效，用 RT 刷新" % label)
                at, rt = refresh_cn(rt)
                accounts = fetch_accounts("https://copilot.tencent.com", at)
            summary = fetch_summary("https://copilot.tencent.com", at)
            pkgs = summary.get("Packages") or []
            total = sum(f(p.get("CycleRemainCapacity")) for p in pkgs)
            total_all += total
            detail = " + ".join("%g" % f(p.get("CycleRemainCapacity"))
                                for p in pkgs if f(p.get("CycleRemainCapacity")) > 0)
            line = "💰 %s 剩 %g%s" % (label, total, "（%s）" % detail if detail else "")
            exp, warn = expiry_line(accounts, now, days)
            if exp:
                line += "\n   " + exp
            lines.append(line)
            if warn:
                warns.append("%s — %s" % (label, warn))
        except Exception as e:
            lines.append("💰 %s 查询失败: %s" % (label, str(e)[:50]))
    body = "国内积分（%s）\n\n%s" % (
        now.strftime("%m-%d %H:%M"), "\n\n".join(lines))
    return body, warns


def report_intl(tokens_raw, days, verbose=True):
    """国际版：取第一个可用 token（口径同 workbuddy_intl.yml 原步骤）"""
    now = datetime.datetime.now()
    lines = [l.strip() for l in re.split(r"[\n&]", tokens_raw or "") if l.strip()]
    if not lines:
        return "💰 剩余积分 —（无可用账号）", []
    parts = [p.strip() for p in lines[0].split("|")]
    token = parts[1] if len(parts) > 1 else parts[0]
    rt = parts[3] if len(parts) > 3 else ""
    # 账号昵称：JWT preferred_username 优先；裸 AT（无 | 分隔）时不把 token 当备注
    who = account_label(token, parts[0] if len(parts) > 1 else "")
    try:
        try:
            accounts = fetch_accounts("https://www.workbuddy.ai", token)
        except urllib.error.HTTPError as e:
            if e.code != 401 or not rt:
                raise
            if verbose:
                print("   🔄 国际版 AT 失效，用 RT 刷新")
            token, rt = refresh_intl(rt)
            accounts = fetch_accounts("https://www.workbuddy.ai", token)
        summary = fetch_summary("https://www.workbuddy.ai", token)
        pkgs = summary.get("Packages") or []
        total = sum(f(p.get("CycleRemainCapacity")) for p in pkgs)
        detail = " + ".join("%g" % f(p.get("CycleRemainCapacity"))
                            for p in pkgs if f(p.get("CycleRemainCapacity")) > 0)
        head = ("💰 %s " % who) if who else "💰 "
        line = "%s剩余积分 %g%s%s" % (
            head, total, "（付费）" if summary.get("IsPaidUser") else "",
            "（%s）" % detail if len(pkgs) > 1 else "")
        exp, warn = expiry_line(accounts, now, days)
        if exp:
            line += "\n" + exp
        return line, ([warn] if warn else [])
    except Exception as e:
        return "💰 剩余积分 查询失败: %s" % str(e)[:60], []


# ---------------------------------------------------------------- Bark
def bark(url, title, body):
    if not url:
        return False
    try:
        req = urllib.request.Request(url.rstrip("/"), method="POST",
                                     data=json.dumps({"title": title, "body": body,
                                                      "group": "WorkBuddy"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        ok = d.get("code") == 200
        print("📢 Bark %s" % ("推送成功" if ok else "失败: %s" % str(d.get("message", ""))[:60]))
        return ok
    except Exception as e:
        print("📢 Bark 异常: %s" % str(e)[:60])
        return False


# ---------------------------------------------------------------- WxPusher
WXPUSHER_URL = "https://wxpusher.zjiecode.com/api/send/message"


def wxpusher(app_token, uids_raw, title, body):
    """WxPusher 推送（微信）；未配置 appToken/uid 则跳过并提示。

    appToken：应用令牌（AT_ 开头，官网创建应用获得）
    uids_raw：用户 UID（UID_ 开头），多个用逗号/分号/空格分隔
    成功码 1000；data 数组里每个 uid 各自也有状态码。
    """
    uids = [u.strip() for u in re.split(r"[,;，\s]+", uids_raw or "") if u.strip()]
    if not app_token or not uids:
        print("⚠️ 未配置 WXPUSHER_APP_TOKEN / WXPUSHER_UID，跳过 WxPusher 推送")
        return False
    first_line = body.splitlines()[0] if body else ""
    summary = (title + " " + first_line).strip()[:100]
    payload = {
        "appToken": app_token,
        "content": body,
        "summary": summary,
        "contentType": 1,          # 1 = 纯文本
        "uids": uids,
        "verifyPayType": 0,        # 不校验付费订阅
    }
    try:
        req = urllib.request.Request(WXPUSHER_URL, method="POST",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        if d.get("code") == 1000:
            items = d.get("data") or []
            ok = [x for x in items if x.get("code") == 1000]
            if not ok:
                # 接口受理 ≠ 送达：常见为「UID 未订阅应用」，此时必须视为失败
                # 让上层回落 Bark，避免通知静默丢失
                why = "；".join(sorted({str(x.get("status") or x.get("code"))[:80] for x in items}))
                print("📢 WxPusher 受理成功但 0 个接收人送达：%s" % (why[:160] or "无明细"))
                return False
            print("📢 WxPusher 推送成功（%d/%d 个接收人）" % (len(ok), len(uids)))
            return True
        print("📢 WxPusher 推送失败: code=%s %s" % (d.get("code"), str(d.get("msg"))[:60]))
    except Exception as e:
        print("📢 WxPusher 异常: %s" % str(e)[:60])
    return False


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cn", action="store_true", help="国内版模式（读 wb_refresh_tokens.json）")
    ap.add_argument("--intl", metavar="TOKENS", help="国际版模式（token 串）")
    ap.add_argument("--store", default="wb_refresh_tokens.json")
    ap.add_argument("--days", type=int, default=7, help="到期预警窗口天数")
    ap.add_argument("--out", help="报告写入文件")
    ap.add_argument("--num-out", help="积分合计写入文件（国际版工作流用）")
    ap.add_argument("--bark-url", default=os.environ.get("BARK_URL", "").strip())
    ap.add_argument("--bark-title", default=None)
    ap.add_argument("--bark", action="store_true", help="推送 Bark（默认仅打印）")
    ap.add_argument("--wx", action="store_true",
                    help="推送 WxPusher 微信（读取 WXPUSHER_APP_TOKEN / WXPUSHER_UID）")
    ap.add_argument("--wx-title", default=None)
    args = ap.parse_args()

    if args.cn:
        body, warns = report_cn(args.store, args.days)
        title = args.bark_title or "💰 国内积分播报"
        num = ""
    elif args.intl is not None:
        body, warns = report_intl(args.intl, args.days)
        title = args.bark_title or "🌍 国际版积分播报"
        m = re.search(r"剩余积分\s+([\d.]+)", body)
        num = m.group(1) if m else ""
    else:
        ap.error("必须指定 --cn 或 --intl")

    print(body)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
    if args.num_out:
        with open(args.num_out, "w", encoding="utf-8") as fh:
            fh.write(num)
    if args.wx or args.bark:
        payload = body
        if warns:
            payload = "\n".join(warns) + "\n" + payload
        sent = False
        if args.wx:
            sent = wxpusher(os.environ.get("WXPUSHER_APP_TOKEN", "").strip(),
                            os.environ.get("WXPUSHER_UID", "").strip(),
                            args.wx_title or title, payload)
            if not sent and args.bark_url:
                print("↩️ WxPusher 未生效，回落到 Bark")
        if not sent and args.bark_url:
            bark(args.bark_url, title, payload)


if __name__ == "__main__":
    main()
