#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号签到编排器（基于 tslshuli/workbuddy-checkin-public，MIT）

原项目是单账号：一份 WB_REFRESH_TOKEN Secret 跑一次签到。
本编排器把 N 个账号装进一份 WB_ACCOUNTS_JSON Secret，逐个调用原脚本：

  * 每个账号在独立子进程里执行，凭据只通过子进程 env 注入，跑完即销毁。
    避免原脚本用 os.environ / sys.exit / 全局通知状态导致的串号。
  * 子进程统一加 --no-notify，由本脚本按聚合结果发一条汇总通知。
  * 输出只含白名单结论字段；任何 >=80 字符的疑似凭据串一律 <REDACTED>。
  * 单个账号失败不影响其余账号；任一失败则整体退出码 1，触发 GitHub 失败通知。

WB_ACCOUNTS_JSON 格式（数组，或 {"accounts": [...]}）：
  [
    {"name": "main",   "refresh_token": "<长效令牌>"},
    {"name": "alt01",  "refresh_token": "<长效令牌>", "domain": "www.codebuddy.cn"},
    {"name": "legacy", "access_token":  "<短效令牌>"}
  ]
name 只允许 [A-Za-z0-9_-]，长度 1..32，且不得重复。

用法：
  python scripts/multi_checkin.py --validate            # 离线校验配置结构，不联网
  python scripts/multi_checkin.py --check-only          # 逐账号只查状态，不领取
  python scripts/multi_checkin.py --out result.json     # 正式签到，结果写文件
  python scripts/multi_checkin.py --accounts-file accounts.json --validate
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHILD = HERE / "workbuddy_checkin.py"

NAME_RX = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DOMAIN_RX = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
LONG_TOKEN_RX = re.compile(r"[A-Za-z0-9_\-]{80,}")
SENSITIVE_KEY_RX = re.compile(r"(token|secret|password|cookie|authorization)", re.I)
ALLOWED_RESULT_KEYS = ("status", "action", "points", "balance", "msg")


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------
def redact(value):
    """递归脱敏：长串打码、敏感键剔除、字符串截断。"""
    if isinstance(value, str):
        return LONG_TOKEN_RX.sub("<REDACTED>", value)[:600]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()
                if not SENSITIVE_KEY_RX.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


# --------------------------------------------------------------------------
# 配置加载与校验
# --------------------------------------------------------------------------
def load_accounts(raw):
    """解析并严格校验账号列表。任何异常都不回显令牌内容。"""
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError("WB_ACCOUNTS_JSON 不是合法 JSON: %s" % type(e).__name__)
    if isinstance(data, dict):
        data = data.get("accounts")
    if not isinstance(data, list) or not data:
        raise ValueError("WB_ACCOUNTS_JSON 必须是非空数组（或含 accounts 数组的对象）")

    accounts, seen = [], set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("第 %d 项不是对象" % (i + 1))
        name = str(item.get("name") or "").strip()
        if not NAME_RX.match(name):
            raise ValueError("第 %d 项 name 不合法（只允许字母/数字/_/-，1..32 位）" % (i + 1))
        if name in seen:
            raise ValueError("账号别名重复: %s" % name)
        seen.add(name)

        rt = str(item.get("refresh_token") or "").strip()
        at = str(item.get("access_token") or "").strip()
        if not rt and not at:
            raise ValueError("账号 %s 缺少 refresh_token（或 access_token）" % name)

        domain = str(item.get("domain") or "").strip()
        if domain and not DOMAIN_RX.match(domain):
            raise ValueError("账号 %s 的 domain 不合法" % name)

        accounts.append({"name": name, "refresh_token": rt,
                         "access_token": at, "domain": domain})
    return accounts


def child_env(account, base_env):
    """为该账号构造干净 env：先剔除所有可能串号的凭据变量，再单独注入。"""
    env = {k: v for k, v in base_env.items()
           if not (k.startswith("WB_") or k.startswith("WORKBUDDY_"))}
    if account["refresh_token"]:
        env["WB_REFRESH_TOKEN"] = account["refresh_token"]
    if account["access_token"]:
        env["WB_ACCESS_TOKEN"] = account["access_token"]
    if account["domain"]:
        env["WB_DOMAIN"] = account["domain"]
    return env


# --------------------------------------------------------------------------
# 子进程执行
# --------------------------------------------------------------------------
def last_json_object(text):
    """从 stdout 里取最后一个 JSON 对象（原脚本正常情况下只输出一个）。"""
    if not text or not text.strip():
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "{":
            continue
        try:
            obj = json.loads(text[i:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def error_row(name, msg, action):
    return {"account": name, "status": "error", "action": action,
            "points": None, "balance": None, "msg": redact(msg),
            "elapsed_sec": None, "exit_code": None}


def run_one(account, child_args, base_env, timeout):
    cmd = [sys.executable, str(CHILD)] + list(child_args) + ["--no-notify"]
    started = time.time()
    try:
        proc = subprocess.run(cmd, env=child_env(account, base_env),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return error_row(account["name"], "子进程超时（%ss）" % timeout, "timeout")
    except Exception as e:
        return error_row(account["name"],
                         "子进程启动失败: %s" % type(e).__name__, "spawn_failed")

    obj = last_json_object(proc.stdout)
    if obj is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return error_row(account["name"],
                         "子进程未输出可解析的 JSON（exit=%s）: %s" % (proc.returncode, tail),
                         "bad_output")

    row = {"account": account["name"],
           "elapsed_sec": round(time.time() - started, 1),
           "exit_code": proc.returncode}
    for key in ALLOWED_RESULT_KEYS:
        value = obj.get(key)
        row[key] = redact(value) if value is not None else None
    # 子进程崩溃/异常退出但恰好输出了 JSON 时，以退出码为准
    if proc.returncode != 0 and row.get("status") == "ok":
        row["status"] = "error"
        row["msg"] = redact((row.get("msg") or "") + "（子进程非零退出）")
    if not row.get("status"):
        row["status"] = "ok" if proc.returncode == 0 else "error"
    return row


# --------------------------------------------------------------------------
# 汇总通知（复用原项目的推送实现，只发一条）
# --------------------------------------------------------------------------
def notify_summary(summary, disabled):
    if disabled:
        return
    try:
        sys.path.insert(0, str(HERE))
        import workbuddy_checkin as wc
        res = {"status": summary["status"],
               "action": "clicked" if summary["status"] == "ok" else "failed",
               "points": None, "balance": None, "msg": summary["msg"],
               "detail": {}}
        if summary["status"] == "ok":
            wc.notify_success(res)
        else:
            wc.notify_failure(res)
    except Exception:
        pass  # 推送失败不影响签到结果与退出码


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def emit(summary, out_path):
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        # stdout 只给人类读的结论，绝不回显令牌
        print("%s 成功 %s/%s" % (
            "多账号签到" if summary.get("mode") == "multi" else "配置校验",
            summary.get("succeeded", 0), summary.get("total", 0)))
    else:
        print(text)


def fail(msg, code=2):
    print(json.dumps({"status": "error", "msg": msg},
                     ensure_ascii=False, indent=2))
    return code


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 多账号签到编排器")
    ap.add_argument("--check-only", action="store_true", help="逐账号只查状态，不领取")
    ap.add_argument("--validate", action="store_true", help="仅离线校验配置，不联网")
    ap.add_argument("--dry-run", action="store_true", help="同 --validate，输出将执行的账号别名")
    ap.add_argument("--no-notify", action="store_true", help="不发汇总推送")
    ap.add_argument("--accounts-file", help="从本地文件读取账号 JSON（本地调试用）")
    ap.add_argument("--out", help="把聚合结果 JSON 写入该文件（权限 0600）")
    ap.add_argument("--timeout", type=int, default=180, help="单账号子进程超时秒数，默认 180")
    args = ap.parse_args()

    if args.check_only and (args.validate or args.dry_run):
        return fail("--check-only 与 --validate/--dry-run 不能同时使用")
    if args.timeout < 10 or args.timeout > 900:
        return fail("--timeout 需在 10..900 秒之间")

    raw = os.environ.get("WB_ACCOUNTS_JSON", "")
    if args.accounts_file:
        path = Path(args.accounts_file).expanduser()
        if not path.is_file():
            return fail("账号文件不存在: %s" % path)
        raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return fail("未配置 WB_ACCOUNTS_JSON。请在仓库 Secrets 中添加，"
                    "或用 --accounts-file 指定本地账号文件。")

    try:
        accounts = load_accounts(raw)
    except ValueError as e:
        return fail(str(e))
    names = [a["name"] for a in accounts]

    if args.validate or args.dry_run:
        emit({"mode": "validate", "status": "ok", "total": len(accounts),
              "succeeded": len(accounts), "failed": 0,
              "accounts": names,
              "msg": "配置校验通过：%d 个账号" % len(accounts)}, args.out)
        return 0

    child_args = ["--check-only"] if args.check_only else []
    results = [run_one(a, child_args, dict(os.environ), args.timeout)
               for a in accounts]
    ok = sum(1 for r in results if r.get("status") == "ok")
    failed_names = [r["account"] for r in results if r.get("status") != "ok"]

    summary = {
        "mode": "multi",
        "status": "ok" if not failed_names else "error",
        "total": len(results),
        "succeeded": ok,
        "failed": len(failed_names),
        "results": results,
        "msg": ("成功 %d/%d" % (ok, len(results)) +
                ("，失败账号: %s" % ", ".join(failed_names) if failed_names else "")),
        "detail": {"credential": "WB_ACCOUNTS_JSON",
                   "check_only": args.check_only,
                   "accounts": names},
    }
    emit(summary, args.out)
    notify_summary(summary, args.no_notify)
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
