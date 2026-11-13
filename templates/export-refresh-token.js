/*
 * WorkBuddy 长效令牌（refresh token）导出工具 · 多账号版
 * ---------------------------------------------------------------
 * 用途：在**你本人已登录**的 WorkBuddy 网页端运行一次，把该账号的明文
 *       长效令牌复制到剪贴板，供 scripts/add_account.py 读取。
 *
 * 用法（每个账号重复一次）：
 *   1. 浏览器打开并登录 https://www.workbuddy.cn （切换到要导出的账号）
 *   2. F12 打开 DevTools → Console，粘贴本文件全部内容后回车
 *      （或把下面 BOOKMARKLET 一行存成书签，登录后点一下）
 *   3. 看到「已复制」提示后回到对话，说一句「导 xxx 账号」，
 *      助手会运行 add_account.py 从剪贴板读取并写入 accounts.json
 *
 * 安全边界：
 *   - 只读取当前页面自身的 localStorage / sessionStorage，不发任何网络请求
 *   - 令牌不打印到屏幕、不写日志、不进 Console 输出
 *   - 成功后剪贴板由 add_account.py 自动清空
 *
 * 若提示「未找到」：说明该站点把 refreshToken 放在 HttpOnly Cookie 或内存里。
 *   改用 DevTools → Network → 过滤 "token/refresh" → 点该请求 → Response，
 *   复制其中 refreshToken 字段的整段字符串（不含引号），再回到对话。
 */

(() => {
  "use strict";
  const stores = [localStorage, sessionStorage];
  const isLongJwt = (v) =>
    typeof v === "string" &&
    v.length >= 200 &&
    (v.match(/\./g) || []).length === 2 &&
    /^[A-Za-z0-9._~-]+$/.test(v);

  const preferred = [
    "refreshToken", "refresh_token", "rt", "wbRefreshToken",
    "workbuddy_refresh_token", "auth.refreshToken"
  ];

  let found = "";
  let where = "";

  for (const key of preferred) {
    for (const s of stores) {
      try {
        const v = s.getItem(key);
        if (isLongJwt(v)) { found = v.trim(); where = key; break; }
      } catch (e) { /* 某些存储不可访问，跳过 */ }
    }
    if (found) break;
  }

  // 兜底：扫描全部键值，取最长的三段式长串（refresh token 通常长于 access token）
  if (!found) {
    for (const s of stores) {
      try {
        for (let i = 0; i < s.length; i++) {
          const k = s.key(i);
          const v = s.getItem(k);
          if (isLongJwt(v) && v.length > found.length) {
            found = v.trim(); where = k;
          }
        }
      } catch (e) { /* ignore */ }
    }
  }

  if (!found) {
    alert(
      "未在本页存储中找到长效令牌。\n\n" +
      "请改用：DevTools → Network → 过滤 token/refresh → " +
      "点该请求 → Response，复制 refreshToken 字段的整段字符串。" +
      "\n\n注意：网页授权码（WBAUTH1: 开头）和短效 accessToken 都不能用。"
    );
    return;
  }

  const copy = (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    // 兜底：临时 textarea，不把明文显示在页面上
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok ? Promise.resolve() : Promise.reject(new Error("copy failed"));
  };

  copy(found).then(
    () => alert("已复制该账号的长效令牌到剪贴板（长度 " + found.length +
                "，位置 " + where + "）。\n回到对话说「导 xxx 账号」即可，令牌不会显示在屏幕上。"),
    () => alert("复制失败，请检查浏览器剪贴板权限后重试。")
  );
})();

/*
 * BOOKMARKLET（把下面这一行存成书签，登录后点一下即可；不含任何凭据）
 * javascript:(()=>{const S=[localStorage,sessionStorage],ok=v=>typeof v==="string"&&v.length>=200&&(v.match(/\./g)||[]).length===2&&/^[A-Za-z0-9._~-]+$/.test(v);let t="";for(const s of S){try{for(let i=0;i<s.length;i++){const v=s.getItem(s.key(i));if(ok(v)&&v.length>t.length)t=v.trim()}}catch(e){}}if(!t){alert("未找到长效令牌，请用 DevTools Network 抓 token/refresh 响应里的 refreshToken");return}const a=document.createElement("textarea");a.value=t;a.style.position="fixed";a.style.opacity="0";document.body.appendChild(a);a.select();document.execCommand("copy");document.body.removeChild(a);alert("已复制长效令牌（长度 "+t.length+"），回到对话说「导 xxx 账号」")})();
 */
