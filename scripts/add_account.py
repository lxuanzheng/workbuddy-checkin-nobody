#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一个账号的长效令牌安全地加入 accounts.json（多账号配置构建器）。

设计前提
--------
macOS 上的 WorkBuddy 5.6.2+ 登录态是 AES-256-GCM 信封，解密密钥只在客户端
进程内存中，本机脚本拿不到明文 refresh token；而 N 个账号又必须逐个登录。
因此本脚本不尝试破解登录态，只负责「用户已拿到明文令牌之后」的全部工作：

  * 令牌来源：剪贴板（默认）/ 文件 / stdin / 本机登录态（Windows 等可解密时）
  * 令牌**绝不**出现在 stdout / 命令行参数 / 日志 / 异常信息里
  * 严格校验形态，拒绝加密信封、网页授权码、短效 access token 等误粘贴内容
  * accounts.json 以 0600 原子写入，默认追加，同名则覆盖
  * 成功后默认清空剪贴板，避免令牌长期滞留

用法
----
  python3 scripts/add_account.py --alias main                 # 从剪贴板读取
  python3 scripts/add_account.py --alias alt01 --source file --file /tmp/rt.txt
  cat /tmp/rt.txt | python3 scripts/add_account.py --alias alt02 --source stdin
  python3 scripts/add_account.py --list                      # 只看别名与脱敏指纹
  python3 scripts/add_account.py --remove alt01
  python3 scripts/add_account.py --alias main --source local  # 尝试本机登录态解密

输出只有别名、脱敏指纹与账号总数，可直接贴进对话。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_FILE = "accounts.json"


def fail(msg, code=2):
    # 异常信息一律不含令牌本体
    sys.stderr.write("错误：%s\n" % msg)
    raise SystemExit(code)


def looks_like_refresh_token(s):
    """宽松校验：三段式 base64url JWT，且足够长。

    与已装签到 Skill 的 rt_auth._looks_like_token 保持一致；长效令牌通常
    数百字符，短效令牌/网页授权码/加密信封都不会通过。
    """
    s = (s or "").strip()
    return (len(s) >= 200 and s.count(".") == 2
            and all(ch.isalnum() or ch in "-_.~" for ch in s))


def read_from_clipboard():
    if sys.platform != "darwin":
        fail("剪贴板模式目前仅支持 macOS（pbpaste）；请改用 --source file 或 stdin")
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        fail("读取剪贴板失败（%s）" % type(e).__name__)
    if r.returncode != 0:
        fail("pbpaste 返回非零，无法读取剪贴板")
    return r.stdout.decode("utf-8", "replace").strip()


def clear_clipboard():
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["pbcopy"], input=b"", timeout=10, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def read_from_local():
    """复用本机已安装签到 Skill 的解密能力；macOS 5.6.2+ 通常失败。"""
    skill = os.path.expanduser(
        "~/.workbuddy/skills/totorosir-workbuddy-checkin/scripts")
    if not os.path.isdir(skill):
        fail("未找到本机签到 Skill，无法尝试本机解密；请用剪贴板/文件方式")
    sys.path.insert(0, skill)
    try:
        import rt_auth  # noqa
        token, _path = rt_auth._load_local_rt()
        return token.strip()
    except Exception as e:
        # 只透出错误类型与截断后的原因，避免任何凭据片段
        fail("本机登录态无法导出明文长效令牌（%s：%s）。"
             "macOS 5.6.2+ 属预期，请在浏览器登录后取明文令牌。"
             % (type(e).__name__, str(e).splitlines()[0][:120]))


def read_token(source, file_path):
    if source == "clipboard":
        return read_from_clipboard()
    if source == "stdin":
        return sys.stdin.read().strip()
    if source == "file":
        if not file_path:
            fail("--source file 需同时指定 --file")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            fail("读取文件失败（%s）" % type(e).__name__)
    if source == "local":
        return read_from_local()
    fail("未知来源：%s" % source)


def validate_token(token):
    if not token:
        fail("取到的令牌为空。请先在已登录的浏览器页面运行导出工具复制到剪贴板")
    if "WBAUTH1:" in token:
        fail("剪贴板里是网页授权码（短效会话凭据），不是长效令牌。"
             "请改用导出刷新令牌的工具，或从 DevTools 的 token/refresh 响应里取 refreshToken")
    if "$wbEncrypted" in token or token.lstrip().startswith("{"):
        fail("剪贴板里是加密信封/JSON，云端无法解密。需要的是明文长效令牌")
    if not looks_like_refresh_token(token):
        fail("内容不像长效令牌（应为 200 字符以上、含两个点号的单段字符串，"
             "长度 %d / 点号 %d）。可能复制了短效令牌或不完整片段"
             % (len(token), token.count(".")))
    return token


def load_accounts(path):
    if not os.path.isfile(path):
        return {"accounts": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError) as e:
        fail("已有 %s 无法解析（%s），请检查或改名后重试" % (path, type(e).__name__))
    if isinstance(data, list):
        data = {"accounts": data}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        fail("已有 %s 结构不合法，应包含 accounts 数组" % path)
    return data


def save_accounts(path, data):
    """0600 原子写，避免半写入或宽松权限。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".accounts-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def masked(token):
    # 只留首 6 位与长度；不足以反推令牌
    return "%s…(len=%d)" % (token[:6], len(token))


def main():
    ap = argparse.ArgumentParser(description="安全地构建多账号签到配置")
    ap.add_argument("--alias", help="账号别名，仅字母/数字/_/-，≤32 字符")
    ap.add_argument("--source", default="clipboard",
                    choices=["clipboard", "file", "stdin", "local"],
                    help="令牌来源，默认剪贴板")
    ap.add_argument("--file", help="--source file 时的令牌文件路径")
    ap.add_argument("--domain", help="可选：该账号的接口域名")
    ap.add_argument("--accounts", default=os.environ.get("WB_ACCOUNTS_FILE", DEFAULT_FILE),
                    help="配置文件路径，默认 accounts.json")
    ap.add_argument("--list", action="store_true", help="列出现有账号别名（脱敏）")
    ap.add_argument("--remove", help="删除指定别名")
    ap.add_argument("--keep-clipboard", action="store_true",
                    help="成功后保留剪贴板内容（默认清空）")
    args = ap.parse_args()

    data = load_accounts(args.accounts)
    accounts = data["accounts"]

    if args.list:
        print(json.dumps({
            "status": "ok",
            "file": args.accounts,
            "total": len(accounts),
            "accounts": [
                {"name": str(a.get("name", "")),
                 "masked": masked(str(a.get("refresh_token", ""))),
                 "domain": a.get("domain", "")}
                for a in accounts if isinstance(a, dict)
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.remove:
        before = len(accounts)
        data["accounts"] = [a for a in accounts
                            if not (isinstance(a, dict) and a.get("name") == args.remove)]
        save_accounts(args.accounts, data)
        print(json.dumps({"status": "ok", "action": "removed",
                          "alias": args.remove,
                          "removed": before - len(data["accounts"]),
                          "total": len(data["accounts"])}, ensure_ascii=False))
        return 0

    if not args.alias:
        fail("请用 --alias 指定账号别名，或用 --list 查看现有账号")
    if not ALIAS_RE.match(args.alias):
        fail("别名不合法：只允许字母/数字/_/-，长度 1–32")

    token = validate_token(read_token(args.source, args.file))

    # 同名覆盖、异名追加；别名去重由同名覆盖保证
    row = {"name": args.alias, "refresh_token": token}
    if args.domain:
        row["domain"] = args.domain
    replaced = False
    for i, a in enumerate(accounts):
        if isinstance(a, dict) and a.get("name") == args.alias:
            if args.domain is None and a.get("domain"):
                row["domain"] = a["domain"]
            accounts[i] = row
            replaced = True
            break
    if not replaced:
        accounts.append(row)

    save_accounts(args.accounts, data)
    cleared = False
    if args.source == "clipboard" and not args.keep_clipboard:
        cleared = clear_clipboard()

    # 输出仅含别名与脱敏指纹，可直接贴进对话
    print(json.dumps({
        "status": "ok",
        "action": "updated" if replaced else "added",
        "alias": args.alias,
        "masked": masked(token),
        "total": len(accounts),
        "file": args.accounts,
        "file_mode": "0600",
        "clipboard_cleared": cleared,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
