#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用官方插件短信登录接口取明文长效令牌，并写入多账号 accounts.json。

为什么需要它
------------
WorkBuddy 5.6.2+ 的本机登录态是 AES-256-GCM 加密信封，解密密钥只在客户端内存，
macOS 上本机脚本拿不到明文 refresh token。而官方插件登录接口由**服务端**直接
下发明文 accessToken / refreshToken，与本机登录态无关，因此可在任意机器上完成，
结果可直接交给 GitHub Actions 云端签到（不用开机、不用装客户端）。

接口契约（三处独立开源实现交叉核对 + 空 body 实测）
---------------------------------------------------
  POST https://www.workbuddy.cn/v2/plugin/login/send-sms   {"phone": "..."}
      -> {"code": 0, "data": {"expires_in": 300}}
  POST https://www.workbuddy.cn/v2/plugin/login/token
      {"login_method": "phone", "phone": "...", "sms_code": "..."}
      -> {"code": 0, "data": {"accessToken": "...", "refreshToken": "..."}}

安全约束
--------
  * 只用 Python 标准库（urllib），不依赖 requests。
  * accessToken / refreshToken 绝不出现在 stdout、stderr、命令行、日志、异常里。
  * 服务端返回的任何文本先过 redact()（长串一律替换）再输出。
  * accounts.json 复用 add_account.py 的 0600 原子写；同名别名覆盖、异名追加。
  * 中间状态文件只存手机号与别名（绝不存验证码或令牌），成功后删除。
  * 默认在写入前用真实 RT 向云端刷新端点试一次，证明凭据可用。

用法（在仓库根目录执行）
------------------------
  # 交互：一条命令完成一个账号（提示输手机号 -> 收短信 -> 输验证码）
  python3 scripts/sms_login.py --alias account01

  # 分离两步（便于由助手代跑，验证码不落盘）
  python3 scripts/sms_login.py --alias account01 --phone 13800000000 --send-only
  python3 scripts/sms_login.py --alias account01 --code 123456

  # 看已有账号（脱敏）
  python3 scripts/add_account.py --list

  # 用真实 RT 连刷两次，实证「轮换后旧值是否仍可用」（不签到、不改配置）
  python3 scripts/sms_login.py --alias account01 --check

注意：短信登录可能使该账号其他端的会话失效，桌面客户端或许需要重新登录一次。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from add_account import (  # noqa: E402
    ALIAS_RE, load_accounts, save_accounts, looks_like_refresh_token, masked,
)

BASE = "https://www.workbuddy.cn"
SEND_SMS_URL = BASE + "/v2/plugin/login/send-sms"
LOGIN_URL = BASE + "/v2/plugin/login/token"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 20

LONG_STRING = re.compile(r"[A-Za-z0-9._~+\-/=]{80,}")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
CODE_RE = re.compile(r"^\d{4,8}$")


def redact(text) -> str:
    """所有外部文本输出前的统一脱敏闸门：任何疑似令牌的长串都不外泄。"""
    return LONG_STRING.sub(lambda m: "<REDACTED len=%d>" % len(m.group()),
                           str(text or ""))


def emit(obj, stream=None):
    (stream or sys.stdout).write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def fail(msg, code=2):
    sys.stderr.write("错误：%s\n" % redact(msg))
    raise SystemExit(code)


def mask_phone(p):
    return (p[:3] + "****" + p[-4:]) if len(p) >= 7 else "***"


def post_json(url, payload, extra_headers=None):
    """返回 (body_dict, http_status)。HTTP 错误也尝试解析业务响应。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE,
        "Referer": BASE + "/",
        "User-Agent": UA,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        return {"code": -1, "msg": "网络错误：%s" % redact(e.reason)}, 0
    except Exception as e:
        return {"code": -1, "msg": "请求异常：%s" % type(e).__name__}, 0
    try:
        body = json.loads(raw)
    except ValueError:
        return {"code": -1, "msg": "响应非 JSON（HTTP %s）" % status}, status
    return (body if isinstance(body, dict) else {"code": -1, "msg": "响应结构异常"}), status


def biz_msg(body):
    return redact(str(body.get("msg") or body.get("message") or ""))[:160]


def state_path(accounts_file):
    return Path(accounts_file).expanduser().resolve().parent / ".sms-state.json"


def _state_raw(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_save(path, data):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(str(path), 0o600)


def write_state(path, alias, phone):
    """按别名存手机号，0600；支持多账号同时待验证，绝不写验证码或令牌。"""
    d = _state_raw(path)
    pending = d.get("pending") if isinstance(d.get("pending"), dict) else {}
    # 兼容早期单账号结构
    if isinstance(d.get("alias"), str) and d.get("phone"):
        pending.setdefault(d["alias"], {"phone": d["phone"],
                                        "sent_at": d.get("sent_at", "")})
    pending[alias] = {"phone": phone,
                      "sent_at": datetime.now().isoformat(timespec="seconds")}
    _state_save(path, {"pending": pending})


def read_state(path, alias):
    """只取指定别名，避免多账号之间串号。"""
    d = _state_raw(path)
    pending = d.get("pending") if isinstance(d.get("pending"), dict) else {}
    row = pending.get(alias)
    if isinstance(row, dict) and row.get("phone"):
        return {"alias": alias, "phone": str(row["phone"])}
    if d.get("alias") == alias and d.get("phone"):  # 兼容早期结构
        return {"alias": alias, "phone": str(d["phone"])}
    return {}


def drop_state(path, alias):
    """成功后只移除该别名；没有待验证账号时才删文件。"""
    d = _state_raw(path)
    pending = d.get("pending") if isinstance(d.get("pending"), dict) else {}
    pending.pop(alias, None)
    try:
        if pending:
            _state_save(path, {"pending": pending})
        else:
            Path(path).unlink()
        return True
    except OSError:
        return False


def send_sms(phone):
    body, status = post_json(SEND_SMS_URL, {"phone": phone})
    if body.get("code") != 0:
        fail("验证码发送失败（HTTP %s）：%s" % (status, biz_msg(body)))
    exp = ((body.get("data") or {}) or {}).get("expires_in", 300) \
        if isinstance(body.get("data"), dict) else 300
    return int(exp) if isinstance(exp, (int, float)) else 300


def login(phone, sms_code):
    body, status = post_json(LOGIN_URL, {
        "login_method": "phone", "phone": phone, "sms_code": sms_code})
    if body.get("code") != 0:
        fail("登录失败（HTTP %s）：%s" % (status, biz_msg(body)))
    data = body.get("data")
    if not isinstance(data, dict):
        data = {}
    # 兼容顶层直返与 data 包裹两种形状
    at = (data.get("accessToken") or data.get("access_token")
          or body.get("accessToken") or "")
    rt = (data.get("refreshToken") or data.get("refresh_token")
          or body.get("refreshToken") or "")
    if not isinstance(rt, str) or not looks_like_refresh_token(rt.strip()):
        fail("登录响应里的 refreshToken 形态不对（应 ≥200 字符、含两个点号的 JWT）。"
             "响应字段：%s" % ",".join(sorted(data.keys())[:12]))
    return at.strip(), rt.strip()


def refresh_probe(rt):
    """用云端同一套刷新实现试一次，确认该 RT 能被 Actions 消费。

    返回 (ok, rotated, err)。不打印任何令牌。
    """
    try:
        import workbuddy_checkin as wc
        new_at, new_rt, err = wc.refresh_access_token(rt)
    except Exception as e:
        return False, False, "%s: %s" % (type(e).__name__, redact(str(e))[:120])
    return bool(new_at), bool(new_rt and new_rt != rt), redact(err or "")


def find_token(accounts_file, alias):
    data = load_accounts(str(accounts_file))
    for a in data["accounts"]:
        if isinstance(a, dict) and a.get("name") == alias:
            return str(a.get("refresh_token") or "")
    return ""


def do_check(accounts_file, alias):
    rt = find_token(accounts_file, alias)
    if not rt:
        fail("accounts.json 里没有别名 %s 的 refresh_token" % alias)
    # 连续两次都用旧值：判定轮换后旧 RT 是否仍可复用（决定 Actions 要不要回写）
    ok1, rot1, err1 = refresh_probe(rt)
    ok2, rot2, err2 = refresh_probe(rt)
    emit({
        "status": "ok" if ok1 else "warning",
        "action": "check",
        "alias": alias,
        "masked": masked(rt),
        "refresh_1st_ok": ok1,
        "refresh_2nd_same_token_ok": ok2,
        "rotated": rot1 or rot2,
        "old_token_reusable": bool(ok1 and ok2),
        "conclusion": ("旧令牌仍可复用，Actions 无需回写轮换值"
                       if (ok1 and ok2) else
                       "旧令牌不可复用：需要回写轮换值，或每次重导令牌"),
        "errors": [e for e in (err1, err2) if e],
        "note": "只做刷新验证，未签到、未修改任何配置",
    })
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="短信登录取明文长效令牌，安全写入 accounts.json")
    ap.add_argument("--alias", required=True, help="账号别名，仅字母/数字/_/-，≤32 字符")
    ap.add_argument("--phone", help="手机号；不填则交互输入或读取上一步状态")
    ap.add_argument("--code", help="短信验证码；填 '-' 表示从 stdin 读；不填则交互输入")
    ap.add_argument("--send-only", action="store_true", help="只发验证码并保存状态，不登录")
    ap.add_argument("--check", action="store_true",
                    help="用已有 RT 连刷两次，验证轮换行为（不签到）")
    ap.add_argument("--skip-probe", action="store_true",
                    help="跳过写入前的刷新验证（不推荐）")
    ap.add_argument("--accounts", default=os.environ.get("WB_ACCOUNTS_FILE", "accounts.json"),
                    help="配置文件路径，默认 accounts.json")
    args = ap.parse_args()

    if not ALIAS_RE.match(args.alias):
        fail("别名不合法：只允许字母/数字/_/-，长度 1–32")
    accounts_file = Path(args.accounts).expanduser()
    st_path = state_path(accounts_file)
    if args.check:
        return do_check(accounts_file, args.alias)

    state = read_state(st_path, args.alias)
    phone = (args.phone or "").strip()
    if not phone and state.get("alias") == args.alias:
        phone = str(state.get("phone") or "").strip()
    if not phone:
        if args.send_only and not sys.stdin.isatty():
            fail("非交互模式下 --send-only 需要 --phone")
        try:
            phone = input("手机号：").strip()
        except (EOFError, KeyboardInterrupt):
            fail("未提供手机号")
    if not PHONE_RE.match(phone):
        fail("手机号格式不对（应为 11 位中国大陆号码）")

    sms_code = (args.code or "").strip()
    if sms_code == "-":
        sms_code = sys.stdin.read().strip()

    if not sms_code:
        # 先发短信。交互模式继续问验证码；非交互模式返回等第二步（--code）
        exp = send_sms(phone)
        write_state(st_path, args.alias, phone)
        if args.send_only or not sys.stdin.isatty():
            emit({"status": "ok", "action": "sms_sent", "alias": args.alias,
                  "phone": mask_phone(phone), "expires_in": exp,
                  "next": "python3 scripts/sms_login.py --alias %s --code <验证码>"
                          % args.alias})
            return 0
        print("验证码已发送至 %s（有效期约 %d 秒）" % (mask_phone(phone), exp))
        try:
            sms_code = input("验证码：").strip()
        except (EOFError, KeyboardInterrupt):
            fail("未提供验证码")

    if not CODE_RE.match(sms_code):
        fail("验证码格式不对（应为 4–8 位数字）")

    at, rt = login(phone, sms_code)

    probe = {"done": False, "ok": False, "rotated": False, "err": ""}
    if not args.skip_probe:
        ok, rotated, err = refresh_probe(rt)
        probe = {"done": True, "ok": ok, "rotated": rotated, "err": err}

    # 写配置：复用同一套 0600 原子写与别名覆盖规则
    data = load_accounts(str(accounts_file))
    accounts = data["accounts"]
    row = {"name": args.alias, "refresh_token": rt}
    replaced = False
    for i, a in enumerate(accounts):
        if isinstance(a, dict) and a.get("name") == args.alias:
            if a.get("domain"):
                row["domain"] = a["domain"]
            accounts[i] = row
            replaced = True
            break
    if not replaced:
        accounts.append(row)
    save_accounts(str(accounts_file), data)
    cleared = drop_state(st_path, args.alias)

    result = {
        "status": "ok" if (probe["done"] is False or probe["ok"]) else "warning",
        "action": "updated" if replaced else "added",
        "alias": args.alias,
        "phone": mask_phone(phone),
        "masked": masked(rt),
        "access_token_present": bool(at),
        "refresh_probe": probe,
        "total": len(accounts),
        "file": str(accounts_file),
        "file_mode": "0600",
        "state_cleared": cleared,
        "note": "令牌未出现在任何输出中；请继续执行 --check 或写 Secret",
    }
    if probe["done"] and not probe["ok"]:
        result["warning"] = "刷新验证未通过，写 Secret 前请复查：%s" % probe["err"]
    emit(result)
    return 0 if result["status"] == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
