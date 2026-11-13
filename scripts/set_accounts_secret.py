#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本地 accounts.json 一次性写入 GitHub Actions Secret: WB_ACCOUNTS_JSON。

安全要点：
  * 令牌只经 stdin 传给 gh，不出现在命令行参数、ps、shell history 里。
  * 不打印文件内容、不打印令牌，只打印账号别名与数量。
  * 先用 multi_checkin.load_accounts 做同一套严格校验，坏配置不会写进 Secret。
  * 文件权限非 0600 时给出警告（不擅自 chmod）。

前置：已安装 GitHub CLI 并执行过 gh auth login。
用法：
  python scripts/set_accounts_secret.py accounts.json --repo <owner>/<repo> --dry-run
  python scripts/set_accounts_secret.py accounts.json --repo <owner>/<repo>
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from multi_checkin import load_accounts  # noqa: E402

SECRET_NAME = "WB_ACCOUNTS_JSON"


def main():
    ap = argparse.ArgumentParser(description="写入多账号 Secret")
    ap.add_argument("file", nargs="?", default="accounts.json",
                    help="本地账号 JSON 文件（默认 accounts.json）")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", ""),
                    help="目标仓库 owner/repo；不填则用 gh 当前目录的仓库")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不写入")
    args = ap.parse_args()

    path = Path(args.file).expanduser()
    if not path.is_file():
        print(json.dumps({"status": "error", "msg": "账号文件不存在: %s" % path},
                         ensure_ascii=False))
        return 2

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print("! 警告：%s 权限为 %o，同机其他用户可能可读。建议 chmod 600 %s"
              % (path, mode, path))
        print("  （含明文长效令牌的文件只应本人可读）")

    raw = path.read_text(encoding="utf-8")
    try:
        accounts = load_accounts(raw)
    except ValueError as e:
        print(json.dumps({"status": "error", "msg": str(e)}, ensure_ascii=False))
        return 2

    # 重新序列化：去掉空字段、压缩体积，且不带任何注释/多余键
    payload = json.dumps(
        [{k: v for k, v in a.items() if v} for a in accounts],
        ensure_ascii=False, separators=(",", ":"))
    result = {"status": "ok", "secret": SECRET_NAME,
              "count": len(accounts), "accounts": [a["name"] for a in accounts],
              "bytes": len(payload.encode("utf-8"))}

    if args.dry_run:
        result["msg"] = "仅校验，未写入任何 Secret"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    cmd = ["gh", "secret", "set", SECRET_NAME]
    if args.repo:
        cmd += ["--repo", args.repo]
    # gh secret set 未提供 --body 时从 stdin 读取，令牌不进命令行
    try:
        proc = subprocess.run(cmd, input=payload, text=True,
                              capture_output=True, timeout=120)
    except FileNotFoundError:
        print(json.dumps({"status": "error",
                          "msg": "未找到 gh 命令，请先安装 GitHub CLI 并执行 gh auth login"},
                         ensure_ascii=False))
        return 1
    except Exception as e:
        print(json.dumps({"status": "error", "msg": "写入失败: %s" % type(e).__name__},
                         ensure_ascii=False))
        return 1

    if proc.returncode != 0:
        # gh 的报错可能含仓库名，但不会含 stdin 内容；仍做长度限制
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        print(json.dumps({"status": "error", "msg": tail}, ensure_ascii=False))
        return 1

    result["msg"] = "已写入 %s（%d 个账号，未输出任何令牌）" % (SECRET_NAME, len(accounts))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
