#!/usr/bin/env python3

import os
import sys
import time
import json
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional
from urllib.parse import urlparse

import requests
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
DASHBOARD_URL = "https://wispbyte.com/client/dashboard"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("wispbyte_restart")

for _noisy in ("seleniumbase", "selenium", "urllib3", "undetected_chromedriver"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ====================== 工具函数 ======================
def mask_email(email: str) -> str:
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        first_char = domain[0]
        masked_domain = f"{first_char}***.{tld}"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"


def mask_server_id(identifier: str) -> str:
    if not identifier:
        return "***"
    if len(identifier) <= 4:
        return "***"
    return identifier[:2] + "***" + identifier[-2:]


def log(msg: str, level: str = "INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    logger.info(f"{prefix} {msg}")


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        return
    if not photo_path or not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30
            )
        resp.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                       check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                           check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            log("删除注册失败（可能未注册），继续...", "WARN")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False


def take_screenshot(sb, account_index: int, suffix: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"acc{account_index}-{suffix}-{timestamp}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        sb.save_screenshot(filepath)
        log(f"📸 截图保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return ""


# ====================== 兼容 CDP 与 WebDriver 的 JS 执行器 ======================
def run_js(sb, js_body: str):
    """
    将 js_body 封装在 (function(){ ... })() 内部执行。
    避免在 CDP Runtime.evaluate 模式下因顶层 return 触发 SyntaxError: Illegal return statement。
    """
    iife = f"(function(){{\n{js_body}\n}})()"
    try:
        return sb.execute_script("return " + iife)
    except Exception as e:
        if "Illegal return statement" in str(e):
            try:
                return sb.execute_script(iife)
            except Exception as inner_e:
                log(f"JS IIFE 执行异常: {str(inner_e)[:80]}", "WARN")
                return None
        log(f"JS 执行异常: {str(e)[:80]}", "WARN")
        return None


# ====================== 屏蔽干扰模态框 ======================
def block_ads_modals(sb):
    css = """
    .wisp-offer-modal, .instagram-modal {
        display: none !important;
    }
    """
    run_js(sb, f'''
        var style = document.createElement('style');
        style.textContent = {json.dumps(css)};
        if (document.head) document.head.appendChild(style);
        return true;
    ''')
    log("✅ 已注入基础屏蔽 CSS")


# ====================== 页面与 URL 判断辅助 ======================
def is_on_console_page(url: str, identifier: str) -> bool:
    """严格检查 URL 的 path 部分，避免被 ?return=... 参数里的 console 路径误导"""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if any(k in path for k in ['venatus', 'reward']):
            return False
        return f"/servers/{identifier.lower()}/console" in path
    except Exception:
        return False


def _get_page_situation(sb, identifier: str) -> str:
    """
    检测当前页面情况，返回:
      'adblocker' - 广告拦截器检测页
      'reward'    - 广告奖励页 (venatus / reward-video)
      'console'   - 正常控制台页
      'unknown'   - 其他
    """
    try:
        current_url = sb.get_current_url()
        path = urlparse(current_url).path.lower()
    except Exception:
        return 'unknown'

    if any(k in path for k in ["venatus", "reward-video", "reward_video", "reward"]):
        return 'reward'

    adblocker = run_js(sb, '''
        var box = document.querySelector('.check-box');
        var title = document.querySelector('.check-title');
        return !!(box || (title && title.textContent.toLowerCase().includes('adblocker')));
    ''')
    if adblocker:
        return 'adblocker'

    has_embed = run_js(sb, '''
        return !!(document.getElementById('embedWatchBtn') || document.getElementById('embedPlayBtn'));
    ''')
    if has_embed:
        return 'reward'

    if is_on_console_page(current_url, identifier):
        return 'console'

    return 'unknown'


# ====================== Turnstile 处理 ======================
def check_turnstile_solved(sb) -> bool:
    res = run_js(sb, '''
        var inp = document.querySelector('input[name="cf-turnstile-response"]');
        if (inp && inp.value && inp.value.length > 20) return true;
        var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        if (iframe && iframe.getAttribute("data-state") === "solved") return true;
        var success = document.getElementById('success');
        return !!(success && getComputedStyle(success).display !== 'none');
    ''')
    return bool(res)


def wait_for_turnstile_success(sb, timeout: int = 35) -> bool:
    log("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        if time.time() - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = time.time()
                log("点击 Turnstile")
            except Exception as e:
                log(f"点击 Turnstile 异常: {e}", "WARN")
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


def handle_restart_turnstile_modal(sb, timeout: int = 60) -> bool:
    log("等待 CF Turnstile 重启验证弹窗...")
    start = time.time()

    modal_appeared = False
    for _ in range(15):
        exists = run_js(sb, '''
            var el = document.querySelector('.wisp-start-captcha-modal');
            return !!(el && getComputedStyle(el).display !== 'none');
        ''')
        if exists:
            modal_appeared = True
            log("CF Turnstile 弹窗已出现")
            break
        time.sleep(1)

    if not modal_appeared:
        log("CF Turnstile 弹窗未出现，可能无需验证", "WARN")
        return True

    last_click = 0
    while time.time() - start < timeout:
        modal_visible = run_js(sb, '''
            var el = document.querySelector('.wisp-start-captcha-modal');
            return !!(el && getComputedStyle(el).display !== 'none');
        ''')
        if not modal_visible:
            log("✅ CF Turnstile 弹窗已关闭，验证完成")
            return True

        if check_turnstile_solved(sb):
            log("Turnstile 已解决，等待弹窗自动关闭...")
            for _ in range(10):
                closed = run_js(sb, '''
                    var el = document.querySelector('.wisp-start-captcha-modal');
                    return !(el && getComputedStyle(el).display !== 'none');
                ''')
                if closed:
                    log("✅ 弹窗已自动关闭")
                    return True
                time.sleep(1)
            run_js(sb, '''
                var btn = document.querySelector(
                    '.wisp-start-captcha-btn[data-action="cancel"],' +
                    '.wisp-start-captcha-modal button[type="submit"],' +
                    '.wisp-start-captcha-modal .submit-btn'
                );
                if (btn) btn.click();
                return true;
            ''')
            time.sleep(2)
            return True

        now = time.time()
        if now - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = now
                log("CF弹窗内点击 Turnstile (uc_gui)")
            except Exception:
                run_js(sb, '''
                    var ts = document.querySelector(
                        '.wisp-start-captcha-modal .cf-turnstile,' +
                        '.wisp-start-captcha-modal iframe'
                    );
                    if (ts) ts.click();
                    return true;
                ''')
                last_click = now

        time.sleep(1)

    return True


# ====================== 广告页面完整处理 ======================
def _dismiss_alert_if_present(sb) -> bool:
    try:
        alert = sb.driver.switch_to.alert
        alert_text = alert.text
        log(f"发现 Alert 弹窗: {alert_text[:100]}")
        alert.accept()
        log("✅ Alert 弹窗已关闭")
        time.sleep(1)
        return True
    except Exception:
        return False


def _handle_adblocker_page(sb) -> bool:
    log("检测到广告拦截器页面，尝试点击 'Check again'...")
    run_js(sb, '''
        var btn = document.getElementById('recheck-btn');
        if (btn) btn.click();
        return true;
    ''')
    time.sleep(3)
    return True


def _wait_for_reward_btn_ready(sb, identifier: str, timeout: int = 90) -> bool:
    """
    等待 'Loading reward video...' 结束并且 'Watch ad to continue' 按钮变为可见可点击状态
    """
    log(f"等待广告视频加载就绪（最长 {timeout}s）...")
    start = time.time()

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        if _dismiss_alert_if_present(sb):
            return False

        # 如果已经自动跳回控制台
        if is_on_console_page(sb.get_current_url(), identifier):
            return False

        result = run_js(sb, '''
            var btn = document.getElementById('embedWatchBtn');
            var panel = document.getElementById('embedPlayBtn');
            var status = document.getElementById('embedStatus');

            if (!btn) {
                var btns = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].innerText || btns[i].textContent || '').trim().toLowerCase();
                    if (t.includes('watch ad')) {
                        btn = btns[i];
                        break;
                    }
                }
            }

            var statusText = status ? (status.innerText || status.textContent || '').trim() : '';

            if (!btn) {
                return {ready: false, reason: 'no_btn', statusText: statusText};
            }

            if (panel && window.getComputedStyle(panel).display === 'none') {
                return {ready: false, reason: 'panel_hidden', statusText: statusText};
            }

            var style = window.getComputedStyle(btn);
            var visible = (style.display !== 'none' && style.visibility !== 'hidden' && btn.offsetParent !== null);
            return {
                ready: visible,
                reason: visible ? 'ok' : 'btn_hidden',
                statusText: statusText
            };
        ''')

        if isinstance(result, dict) and result.get('ready'):
            log("✅ 广告已就绪，'Watch ad to continue' 按钮可点击")
            return True
        else:
            reason = result.get('reason', '?') if isinstance(result, dict) else '?'
            status_text = result.get('statusText', '') if isinstance(result, dict) else ''
            if elapsed % 10 == 0:
                log(f"广告加载中... [{elapsed}s] reason={reason} status='{status_text[:60]}'")

        time.sleep(2)

    log(f"⏰ 等待广告按钮就绪超时 ({timeout}s)", "WARN")
    return False


def _click_watch_ad_btn(sb) -> bool:
    """点击 'Watch ad to continue' 按钮"""
    log("点击 'Watch ad to continue' 按钮...")

    # 方式 1: 通过 SeleniumBase 原生 click
    try:
        if sb.is_element_visible('#embedWatchBtn'):
            sb.click('#embedWatchBtn')
            log("✅ 已通过 sb.click 点击 #embedWatchBtn")
            time.sleep(1)
            return True
    except Exception:
        pass

    # 方式 2: 通过 JS 精准点击
    clicked = run_js(sb, '''
        var btn = document.getElementById('embedWatchBtn');
        if (!btn) {
            var btns = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || btns[i].textContent || '').trim().toLowerCase();
                if (t.includes('watch ad')) {
                    btn = btns[i];
                    break;
                }
            }
        }
        if (btn) {
            btn.click();
            return true;
        }
        return false;
    ''')
    if clicked:
        log("✅ 已通过 JS 点击 'Watch ad to continue' 按钮")
        time.sleep(1)
        return True

    log("❌ 未能点击到广告播放按钮", "ERROR")
    return False


def _wait_for_ad_completion(sb, identifier: str, timeout: int = 180) -> bool:
    """
    静候广告播放完成并自动跳转回控制台。
    期间不再乱点任何按钮，防止打断广告播放。
    """
    safe_id = mask_server_id(identifier)
    log(f"广告正在播放，静候完成（最长 {timeout}s）: {safe_id}")
    start = time.time()

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        if _dismiss_alert_if_present(sb):
            log("检测到 Alert 弹窗并已关闭")

        try:
            current_url = sb.get_current_url()
            path = urlparse(current_url).path.lower()

            # 信号 1: URL 含 rewardDone=1
            if "rewarddone=1" in current_url.lower():
                log(f"✅ 广告完成 [rewardDone=1]: {safe_id}")
                return True

            # 信号 2: 已真正回到控制台路径
            if is_on_console_page(current_url, identifier):
                log(f"✅ 广告完成 [已自动回到控制台]: {safe_id}")
                return True

            # 信号 3: 检查 #embedStatus 完成提示词
            status_info = run_js(sb, '''
                var st = document.getElementById('embedStatus');
                if (!st) return {visible: false, text: ''};
                var display = window.getComputedStyle(st).display;
                return {
                    visible: display !== 'none',
                    text: (st.innerText || st.textContent || '').trim()
                };
            ''')
            st_text = ""
            if isinstance(status_info, dict) and status_info.get('visible'):
                st_text = status_info.get('text', '')
                lower_text = st_text.lower()
                if any(kw in lower_text for kw in ['starting', 'saving', 'returning', 'session', 'complete']):
                    log(f"✅ 广告完成 [状态='{st_text[:50]}']，等待页面跳转: {safe_id}")
                    time.sleep(6)
                    return True

            # 信号 4: 已经离开 venatus / reward 页面
            if not any(k in path for k in ['venatus', 'reward']) and elapsed > 5:
                log(f"✅ 广告完成 [页面已离开广告页 -> {path}]: {safe_id}")
                return True

            if elapsed % 15 == 0:
                log(f"等待广告播放完毕... [{elapsed}s] path={path} status='{st_text[:40]}'")

        except Exception as e:
            log(f"广告完成检测异常: {e}", "WARN")

        time.sleep(3)

    log(f"⚠️ 广告播放等待超时 ({timeout}s): {safe_id}", "WARN")
    return False


def handle_reward_ad_flow(sb, identifier: str, console_url: str) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"广告流程处理开始: {safe_id}")

    start = time.time()
    while time.time() - start < 15:
        if _dismiss_alert_if_present(sb):
            log("✅ 已处理 Alert 弹窗（无广告情况），继续后续流程")
            return True

        situation = _get_page_situation(sb, identifier)
        if situation == 'reward':
            log(f"✅ 已进入广告奖励页面: {urlparse(sb.get_current_url()).path}")
            if _wait_for_reward_btn_ready(sb, identifier, timeout=90):
                if _dismiss_alert_if_present(sb):
                    return True
                if _click_watch_ad_btn(sb):
                    _wait_for_ad_completion(sb, identifier, timeout=180)
                    _dismiss_alert_if_present(sb)
            return True

        elif situation == 'adblocker':
            _handle_adblocker_page(sb)
            continue

        time.sleep(1)

    _dismiss_alert_if_present(sb)
    log("未触发广告跳转或已在控制台，继续执行")
    return True


# ====================== 登录流程 ======================
def login(sb, email: str, password: str) -> bool:
    log("访问登录页...")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
    time.sleep(4)

    try:
        sb.wait_for_element_visible('input#email', timeout=15)
        log("✅ 找到登录表单")
    except TimeoutException:
        log("未找到登录表单，尝试重新连接...", "WARN")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
        time.sleep(5)
        try:
            sb.wait_for_element_visible('input#email', timeout=10)
        except TimeoutException:
            log("仍然未找到登录表单", "ERROR")
            return False

    log("填写登录信息...")
    sb.type('input#email', email)
    time.sleep(0.5)
    sb.type('input#password', password)
    time.sleep(0.5)

    if not wait_for_turnstile_success(sb, timeout=35):
        log("登录 Turnstile 未通过", "ERROR")
        return False

    log("提交登录...")
    try:
        sb.click('button.login-btn')
    except Exception:
        run_js(sb, 'var f = document.querySelector("form#login-form"); if (f) f.submit(); return true;')

    log("等待跳转到仪表盘...")
    for _ in range(20):
        cur = sb.get_current_url()
        if "/dashboard" in cur or "/client/dashboard" in cur:
            log("已跳转到仪表盘")
            break
        time.sleep(1)
    else:
        log("登录后未成功跳转到仪表盘", "ERROR")
        return False

    block_ads_modals(sb)
    time.sleep(2)
    log("✅ 登录成功并进入仪表盘")
    return True


# ====================== 获取服务器列表 ======================
def get_servers(sb) -> List[str]:
    log("通过 fetch 请求服务器列表...")
    try:
        result = sb.execute_async_script('''
            var callback = arguments[arguments.length - 1];
            fetch('/client/api/servers/status', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data && data.servers) {
                    callback(data.servers.map(function(s) { return s.identifier; }));
                } else {
                    callback([]);
                }
            })
            .catch(function(err) { callback([]); });
        ''')
        if result and isinstance(result, list):
            ids = [str(i) for i in result if i]
            if ids:
                masked = [mask_server_id(i) for i in ids]
                log(f"成功获取服务器列表，共 {len(ids)} 台: {masked}")
                return ids
    except Exception as e:
        log(f"fetch 请求失败: {e}", "ERROR")

    dom_ids = run_js(sb, '''
        var cards = document.querySelectorAll('[data-server-id], .server-card, .server-item');
        return Array.from(cards).map(function(el) {
            return el.getAttribute('data-server-id') || el.id;
        }).filter(Boolean);
    ''')
    if dom_ids and isinstance(dom_ids, list):
        masked = [mask_server_id(i) for i in dom_ids]
        log(f"从 DOM 提取到服务器，共 {len(dom_ids)} 台: {masked}")
        return list(dom_ids)

    log("未能获取任何服务器标识符", "ERROR")
    return []


# ====================== 重启服务器 ======================
def restart_server(sb, identifier: str) -> bool:
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"{'─'*40}")
    log(f"重启服务器: {safe_id}")
    log(f"{'─'*40}")

    log(f"导航到控制台: {safe_id}")
    sb.get(console_url)
    time.sleep(5)
    block_ads_modals(sb)

    start_btn = None
    for btn_sel, btn_name in [('button#start-btn', 'Start'), ('button#restart-btn', 'Restart')]:
        try:
            start_btn = sb.wait_for_element_visible(btn_sel, timeout=8)
            log(f"找到 {btn_name} 按钮")
            break
        except Exception:
            continue

    if not start_btn:
        log("未找到 Start/Restart 按钮", "ERROR")
        return False

    try:
        start_btn.click()
        log("✅ 已点击 Start/Restart 按钮")
    except Exception:
        clicked = run_js(sb, '''
            var b = document.querySelector('#start-btn, #restart-btn');
            if (b) { b.click(); return true; }
            return false;
        ''')
        if not clicked:
            log("点击 Start/Restart 失败", "ERROR")
            return False
        log("✅ 已通过 JS 点击 Start/Restart 按钮")

    time.sleep(3)

    log("=== 开始处理广告流程 ===")
    handle_reward_ad_flow(sb, identifier, console_url)
    log("=== 广告流程处理完毕 ===")

    time.sleep(2)
    cur_url = sb.get_current_url()
    if not is_on_console_page(cur_url, identifier):
        log(f"当前不在控制台（{urlparse(cur_url).path}），重新导航...")
        sb.get(console_url)
        time.sleep(5)
    else:
        log("当前已在控制台页面")

    block_ads_modals(sb)

    log("=== 开始处理 CF Turnstile 验证 ===")
    handle_restart_turnstile_modal(sb, timeout=60)
    log("=== CF Turnstile 验证完成 ===")

    block_ads_modals(sb)

    log(f"开始轮询服务器状态（最长 75 秒）: {safe_id}")
    poll_timeout = 75
    poll_interval = 5
    start_poll = time.time()

    status_script = f'''
        var callback = arguments[arguments.length - 1];
        var serverId = {json.dumps(identifier)};
        fetch('/client/api/servers/status', {{
            method: 'GET',
            headers: {{ 'Accept': 'application/json' }}
        }})
        .then(function(res) {{ return res.json(); }})
        .then(function(data) {{
            if (!data || !data.servers) return callback(null);
            var server = data.servers.find(function(s) {{
                return s.identifier === serverId;
            }});
            callback(server ? server.current_state : null);
        }})
        .catch(function() {{ callback(null); }});
    '''

    retried_start = False
    while time.time() - start_poll < poll_timeout:
        try:
            status = sb.execute_async_script(status_script)
            if status and any(s in str(status).lower() for s in ['running', 'starting']):
                log(f"✅ 服务器 {safe_id} 状态: {status}，重启成功！")
                return True
            log(f"当前状态: {status or '未知'}，{poll_interval}s 后重试...")

            # 如果看完广告回到控制台 15 秒后依然是 offline，尝试补点一次 Start 按钮
            if not retried_start and (time.time() - start_poll > 15) and str(status).lower() == 'offline':
                retried_start = True
                log("看完广告后仍为 offline，尝试在控制台补点一次 Start 按钮...")
                run_js(sb, '''
                    var b = document.querySelector('button#start-btn');
                    if (b && !b.disabled) { b.click(); return true; }
                    return false;
                ''')
                time.sleep(2)
                handle_restart_turnstile_modal(sb, timeout=25)
        except Exception as e:
            log(f"状态检查异常: {e}", "WARN")
        time.sleep(poll_interval)

    try:
        final_status = sb.execute_async_script(status_script)
        if final_status and any(s in str(final_status).lower() for s in ['running', 'starting']):
            log(f"✅ 最终检查成功: {safe_id} 状态: {final_status}")
            return True
        log(f"❌ 轮询超时，最终状态: {final_status or '未知'}", "ERROR")
        return False
    except Exception as e:
        log(f"最终状态检查失败: {e}", "ERROR")
        return False


# ====================== 账号处理 ======================
def process_account(idx: int, email: str, password: str, tg_token: str, tg_chat: str):
    log(f"{'='*50}")
    log(f"开始处理账号 {idx} | {mask_email(email)}")
    log(f"{'='*50}")

    user_data_dir = tempfile.mkdtemp(prefix=f"wisp_usr_{idx}_")
    with SB(uc=True, test=True, locale="en", headed=False,
            user_data_dir=user_data_dir,
            chromium_arg="--disable-blink-features=AutomationControlled") as sb:
        try:
            if not login(sb, email, password):
                screenshot = take_screenshot(sb, idx, "login-fail")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 登录失败\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            servers = get_servers(sb)
            if not servers:
                screenshot = take_screenshot(sb, idx, "no-server")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 未找到服务器\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            for si, server_id in enumerate(servers, start=1):
                success = restart_server(sb, server_id)
                suffix = f"done-{si}" if len(servers) > 1 else "done"
                screenshot = take_screenshot(sb, idx, suffix)
                status_icon = "✅" if success else "❌"
                status_text = "重启成功" if success else "重启失败"
                caption = (
                    f"{status_icon} {status_text}\n\n"
                    f"账号: {mask_email(email)}\n"
                    f"服务器: {server_id}\n\n"
                    f"Wispbyte Auto Restart"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)

        except Exception as e:
            log(f"账号 {idx} 处理异常: {e}", "ERROR")
            screenshot = take_screenshot(sb, idx, "exception")
            send_tg_photo(tg_token, tg_chat, screenshot,
                          f"❌ 脚本异常\n账号: {mask_email(email)}\n信息: {str(e)[:200]}\n\nWispbyte Auto Restart")


# ====================== 账号加载 ======================
def load_accounts() -> List[Tuple[str, str]]:
    accounts = []
    for i in range(1, 6):
        raw = os.environ.get(f"WISPBYTE_{i}")
        if not raw:
            continue
        parts = raw.split("-----")
        if len(parts) >= 2:
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                accounts.append((email, password))
                log(f"加载账号 WISPBYTE_{i}: {mask_email(email)}")
            else:
                log(f"WISPBYTE_{i} 格式不正确（邮箱或密码为空）", "WARN")
        else:
            log(f"WISPBYTE_{i} 格式错误，期望 '邮箱-----密码'", "WARN")
    return accounts


def parse_target_emails(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    seen = set()
    result = []
    for part in raw.split(","):
        email = part.strip().lower()
        if not email:
            continue
        if "@" not in email:
            log(f"无效的邮箱格式: '{email}'，已跳过", "WARN")
            continue
        if email in seen:
            log(f"重复邮箱: '{email}'，已跳过", "WARN")
            continue
        seen.add(email)
        result.append(email)
    return result


# ====================== 入口 ======================
def main():
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not tg_token or not tg_chat:
        log("缺少 TG_BOT_TOKEN 或 TG_CHAT_ID，通知功能将不可用", "WARN")

    all_accounts = load_accounts()
    if not all_accounts:
        log("未找到任何有效账号，请检查 Secrets 设置", "ERROR")
        sys.exit(1)

    target_raw = os.environ.get("INPUT_ACCOUNTS", "").strip()
    target_emails = parse_target_emails(target_raw)

    if target_emails:
        all_email_map = {
            email.lower(): (idx, email, password)
            for idx, (email, password) in enumerate(all_accounts, start=1)
        }
        selected = []
        for target in target_emails:
            if target in all_email_map:
                selected.append(all_email_map[target])
            else:
                log(f"邮箱 '{mask_email(target)}' 未在已配置账号中找到，已跳过", "WARN")
        if not selected:
            log("指定的邮箱全部无效，退出", "ERROR")
            sys.exit(1)
        log(f"指定运行账号: {[mask_email(e) for _, e, _ in selected]}")
    else:
        selected = [(idx, email, password)
                    for idx, (email, password) in enumerate(all_accounts, start=1)]
        log("未指定账号，运行全部账号")

    for run_order, (idx, email, password) in enumerate(selected):
        if run_order > 0:
            restart_warp()
        process_account(idx, email, password, tg_token, tg_chat)
        if run_order < len(selected) - 1:
            time.sleep(5)

    log("所有账号处理完毕")


if __name__ == "__main__":
    main()
