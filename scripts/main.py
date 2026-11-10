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
from urllib.parse import urlparse
from typing import List, Tuple, Optional

import requests
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
DASHBOARD_URL = "https://wispbyte.com/client/dashboard"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"
REWARD_VIDEO_URL = "https://wispbyte.com/client/reward-video"

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


def is_ad_or_reward_url(url: str) -> bool:
    """判断当前 URL 的主路径是否为广告/奖励视频页面（忽略 ?return= 参数）"""
    if not url:
        return False
    path = urlparse(url).path.lower()
    return any(kw in path for kw in ("reward-video", "reward_video", "venatus-reward", "reward"))


def is_console_url(url: str, identifier: str) -> bool:
    """判断当前 URL 的主路径是否真正回到了控制台页面（忽略 ?return= 参数）"""
    if not url:
        return False
    path = urlparse(url).path
    return f"/servers/{identifier}/console" in path


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


def _safe_warp_cmd(args: List[str], timeout: int = 12) -> bool:
    """带严格超时与非交互输入保护的 warp-cli 调用，防止进程卡死"""
    try:
        res = subprocess.run(
            ["sudo", "warp-cli", "--accept-tos"] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False
        )
        return res.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"warp-cli {' '.join(args)} 超时 ({timeout}s)，跳过...", "WARN")
        return False
    except Exception as e:
        log(f"warp-cli {' '.join(args)} 异常: {e}", "WARN")
        return False


def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"

    _safe_warp_cmd(["disconnect"], timeout=10)
    time.sleep(2)

    if not _safe_warp_cmd(["registration", "delete"], timeout=10):
        log("删除注册失败（可能未注册），继续...", "WARN")
    time.sleep(1)

    # 重启一次 warp-svc 服务有助于刷新 WireGuard 密钥与出口会话
    try:
        subprocess.run(["sudo", "systemctl", "restart", "warp-svc"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
        time.sleep(3)
    except Exception:
        pass

    if not _safe_warp_cmd(["registration", "new"], timeout=15):
        log("注册新 WARP 节点超时或失败，再次尝试...", "WARN")
        time.sleep(2)
        _safe_warp_cmd(["registration", "new"], timeout=12)

    time.sleep(2)
    _safe_warp_cmd(["connect"], timeout=12)
    time.sleep(6)

    try:
        new_ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 连接后网络不通 ({e})，断开 WARP 恢复默认网络继续执行...", "WARN")
        _safe_warp_cmd(["disconnect"], timeout=10)
        time.sleep(3)
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


# ====================== 广告弹窗与引导遮罩屏蔽 ======================
def block_ads_modals(sb):
    """屏蔽干扰性弹窗，并关闭新手引导与 Cookie 遮罩（仅在登录成功后调用）"""
    css = """
    .wisp-offer-modal, .instagram-modal, .qc-cmp2-summary-section,
    #qc-cmp2-container, .qc-cmp2-container {
        display: none !important;
    }
    """
    try:
        sb.execute_script(f'''
            var style = document.createElement('style');
            style.textContent = {json.dumps(css)};
            document.head.appendChild(style);
        ''')
        log("✅ 已注入广告屏蔽 CSS")
    except Exception as e:
        log(f"注入屏蔽 CSS 失败: {e}", "WARN")

    try:
        sb.execute_script('''
            var cmp = document.getElementById('qc-cmp2-container');
            if (cmp) cmp.remove();
            var btns = document.querySelectorAll('button, a, span');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').trim().toLowerCase();
                if (t === 'skip' || t === '跳过') {
                    btns[i].click();
                }
            }
        ''')
    except Exception:
        pass


# ====================== Turnstile 处理 ======================
def check_turnstile_solved(sb) -> bool:
    """检查当前页面/弹窗中的 Turnstile 是否已完成"""
    try:
        return bool(sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        '''))
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 30) -> bool:
    """等待并点击登录页 Turnstile"""
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


def handle_restart_turnstile_modal(sb, timeout: int = 90) -> bool:
    """
    处理点击 Start 后弹出的 CF Turnstile 验证弹窗。
    弹窗选择器: .wisp-start-captcha-modal
    """
    log("等待 CF Turnstile 重启验证弹窗...")
    start = time.time()

    modal_appeared = False
    for _ in range(15):
        try:
            exists = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if exists:
                modal_appeared = True
                log("CF Turnstile 弹窗已出现")
                break
        except Exception:
            pass
        time.sleep(1)

    if not modal_appeared:
        log("CF Turnstile 弹窗未出现，可能无需验证", "WARN")
        return True

    last_click = 0
    while time.time() - start < timeout:
        try:
            modal_visible = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if not modal_visible:
                log("✅ CF Turnstile 弹窗已关闭，验证完成")
                return True

            if check_turnstile_solved(sb):
                log("Turnstile 已解决，等待弹窗自动关闭...")
                for _ in range(15):
                    closed = sb.execute_script('''
                        var el = document.querySelector('.wisp-start-captcha-modal');
                        return !(el && getComputedStyle(el).display !== 'none');
                    ''')
                    if closed:
                        log("✅ 弹窗已自动关闭")
                        return True
                    time.sleep(1)
                try:
                    sb.execute_script('''
                        var btn = document.querySelector(
                            '.wisp-start-captcha-btn[data-action="cancel"],' +
                            '.wisp-start-captcha-modal button[type="submit"],' +
                            '.wisp-start-captcha-modal .submit-btn'
                        );
                        if (btn) btn.click();
                    ''')
                    time.sleep(2)
                    return True
                except Exception:
                    pass
                return True

            now = time.time()
            if now - last_click > 3:
                try:
                    sb.uc_gui_click_captcha()
                    last_click = now
                    log("CF弹窗内点击 Turnstile (uc_gui)")
                except Exception:
                    try:
                        sb.execute_script('''
                            var ts = document.querySelector(
                                '.wisp-start-captcha-modal .cf-turnstile,' +
                                '.wisp-start-captcha-modal iframe'
                            );
                            if (ts) ts.click();
                        ''')
                        last_click = now
                        log("CF弹窗内点击 Turnstile (JS)")
                    except Exception as e:
                        log(f"CF弹窗 Turnstile 点击失败: {e}", "WARN")

        except Exception as e:
            log(f"CF Turnstile 弹窗处理异常: {e}", "WARN")

        time.sleep(1)

    try:
        if sb.execute_script('return !document.querySelector(".wisp-start-captcha-modal") || getComputedStyle(document.querySelector(".wisp-start-captcha-modal")).display === "none";'):
            log("✅ 超时后弹窗已消失")
            return True
        if check_turnstile_solved(sb):
            log("✅ 超时后 Turnstile 已完成")
            return True
    except Exception:
        pass

    log("CF Turnstile 弹窗处理超时", "WARN")
    return False


# ====================== 广告页面处理 ======================
def _get_page_situation(sb) -> str:
    try:
        current_url = sb.get_current_url()
    except Exception:
        return 'unknown'

    if is_ad_or_reward_url(current_url):
        return 'reward'

    try:
        adblocker = sb.execute_script('''
            var box = document.querySelector('.check-box');
            var title = document.querySelector('.check-title');
            return !!(box || (title && title.textContent.toLowerCase().includes('adblocker')));
        ''')
        if adblocker:
            return 'adblocker'
    except Exception:
        pass

    try:
        has_embed_btn = sb.execute_script('''
            return !!(document.getElementById('embedWatchBtn') || 
                      document.getElementById('embedPlayBtn'));
        ''')
        if has_embed_btn:
            return 'reward'
    except Exception:
        pass

    return 'unknown'


def _dismiss_alert_if_present(sb) -> bool:
    try:
        alert = sb.driver.switch_to.alert
        alert_text = alert.text
        log(f"发现 Alert 弹窗: {alert_text[:100]}")
        alert.accept()
        log("✅ Alert 弹窗已关闭（点击确定）")
        time.sleep(1)
        return True
    except Exception:
        return False


def _handle_adblocker_page(sb) -> bool:
    log("检测到广告拦截器页面，尝试点击 'Check again'...")
    try:
        sb.execute_script('''
            var btn = document.getElementById('recheck-btn');
            if (btn) btn.click();
        ''')
        log("✅ 已点击 'Check again'")
        time.sleep(3)
        return True
    except Exception as e:
        log(f"点击 'Check again' 失败: {e}", "WARN")
        return False


def _wait_for_reward_btn_ready(sb, timeout: int = 90) -> bool:
    log(f"等待广告视频加载就绪（最长 {timeout}s）...")
    start = time.time()

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            result = sb.execute_script('''
                var btn = document.getElementById('embedWatchBtn');
                var panel = document.getElementById('embedPlayBtn');
                var status = document.getElementById('embedStatus');

                if (!btn || !panel) return {ready: false, reason: 'no_element'};

                var panelDisplay = window.getComputedStyle(panel).display;
                var btnDisplay = window.getComputedStyle(btn).display;
                var btnVis = window.getComputedStyle(btn).visibility;

                if (panelDisplay === 'none') {
                    var statusText = status ? status.textContent : '';
                    return {ready: false, reason: 'panel_hidden', statusText: statusText};
                }

                return {
                    ready: btnDisplay !== 'none' && btnVis !== 'hidden',
                    reason: 'ok'
                };
            ''')

            if result and result.get('ready'):
                log("✅ 广告已就绪，Watch ad 按钮可点击")
                return True
            else:
                reason = result.get('reason', '?') if result else '?'
                status_text = result.get('statusText', '') if result else ''
                if elapsed % 10 == 0:
                    log(f"广告加载中... [{elapsed}s] reason={reason} status='{status_text[:60]}'")

        except Exception as e:
            log(f"检查广告就绪状态异常: {e}", "WARN")

        time.sleep(2)

    log(f"⏰ 广告按钮等待超时 ({timeout}s)", "WARN")
    return False


def _click_watch_ad_btn(sb) -> bool:
    log("点击 'Watch ad to continue' 按钮...")
    methods = [
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.click();
            return true;
        '''),
        lambda: (sb.click('#embedWatchBtn') or True),
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        '''),
    ]

    for i, method in enumerate(methods, 1):
        try:
            result = method()
            if result:
                log(f"✅ 广告按钮点击成功（方式{i}）")
                time.sleep(1)
                return True
        except Exception as e:
            log(f"广告按钮点击方式{i}失败: {e}", "WARN")

    log("❌ 所有广告按钮点击方式均失败", "ERROR")
    return False


def _wait_for_ad_completion(sb, identifier: str, timeout: int = 300) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"广告开始播放，等待完成（最长 {timeout}s）: {safe_id}")
    start = time.time()

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            if _dismiss_alert_if_present(sb):
                log(f"✅ 广告播放期间处理了 Alert 弹窗: {safe_id}")
                time.sleep(3)

            current_url = sb.get_current_url()

            if "rewardDone=1" in current_url:
                log(f"✅ 广告完成 [rewardDone=1]: {safe_id}")
                time.sleep(3)
                return True

            if is_console_url(current_url, identifier):
                log(f"✅ 广告完成 [已跳转回控制台]: {safe_id}")
                time.sleep(3)
                return True

            try:
                status_info = sb.execute_script('''
                    var st = document.getElementById('embedStatus');
                    if (!st) return {visible: false, text: ''};
                    var display = window.getComputedStyle(st).display;
                    return {
                        visible: display !== 'none',
                        text: st.textContent || ''
                    };
                ''')
                if status_info and status_info.get('visible'):
                    text = status_info.get('text', '').lower()
                    if any(kw in text for kw in ['starting', 'saving', 'returning', 'session', 'complete', 'redirect']):
                        log(f"✅ 广告完成 [embedStatus='{text[:50]}']: {safe_id}")
                        for _ in range(20):
                            if is_console_url(sb.get_current_url(), identifier):
                                log("✅ 已自动跳转回控制台")
                                break
                            time.sleep(1)
                        return True
            except Exception:
                pass

            if not is_ad_or_reward_url(current_url) and elapsed > 10:
                situation = _get_page_situation(sb)
                if situation not in ('reward', 'adblocker', 'unknown'):
                    log(f"✅ 广告完成 [页面已跳转到: {current_url[:80]}]: {safe_id}")
                    return True

            if elapsed % 10 == 0:
                path_only = urlparse(current_url).path
                log(f"正在观看广告... [{elapsed}s] 当前路径={path_only}")

        except Exception as e:
            log(f"广告完成检测异常: {e}", "WARN")

        time.sleep(3)

    log(f"⚠️ 广告等待超时 ({timeout}s): {safe_id}", "WARN")
    return False


def handle_reward_ad_flow(sb, identifier: str, console_url: str) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"广告流程处理开始: {safe_id}")

    ad_flow_timeout = 15
    start = time.time()

    while time.time() - start < ad_flow_timeout:
        if _dismiss_alert_if_present(sb):
            log("✅ 已处理 Alert 弹窗（无广告情况），继续CF验证")
            return True

        situation = _get_page_situation(sb)
        log(f"当前页面情况: {situation}")

        if situation == 'reward':
            return _execute_reward_ad_watch(sb, identifier)

        elif situation == 'adblocker':
            log("检测到广告拦截器页面")
            _handle_adblocker_page(sb)
            time.sleep(3)
            continue

        elif situation == 'console':
            log("当前在控制台页面，无需处理广告")
            return True

        time.sleep(1)

    if _dismiss_alert_if_present(sb):
        log("✅ 超时后处理 Alert 弹窗")
        return True

    log("广告流程等待超时，继续执行CF验证", "WARN")
    return True


def _execute_reward_ad_watch(sb, identifier: str) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"进入广告观看流程: {safe_id}")

    btn_ready = _wait_for_reward_btn_ready(sb, timeout=90)

    if not btn_ready:
        if _dismiss_alert_if_present(sb):
            log("✅ 广告按钮未就绪但检测到 Alert（无广告），继续CF验证")
            return True

        current_url = sb.get_current_url()
        if not is_ad_or_reward_url(current_url):
            log(f"广告页面已自动跳转: {current_url[:80]}")
            return True

        log("广告按钮未就绪，继续CF验证", "WARN")
        return True

    if _dismiss_alert_if_present(sb):
        log("✅ 点击前检测到 Alert（无广告），继续CF验证")
        return True

    if not _click_watch_ad_btn(sb):
        log("广告按钮点击失败，继续CF验证", "WARN")
        return True

    _wait_for_ad_completion(sb, identifier, timeout=300)
    _dismiss_alert_if_present(sb)

    log(f"广告流程结束: {safe_id}")
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
        sb.execute_script('document.querySelector("form#login-form").submit()')

    log("等待跳转到仪表盘...")
    for _ in range(15):
        if "/dashboard" in sb.get_current_url() or "/client/dashboard" in sb.get_current_url():
            log("已跳转到仪表盘")
            break
        time.sleep(1)
    else:
        log("登录后未成功跳转到仪表盘", "ERROR")
        return False

    block_ads_modals(sb)

    DASHBOARD_SELECTORS = [
        'div.server-list', 'div.servers-container', 'div.card',
        'div.server-card', 'table', 'main', 'section', '#app',
    ]
    dashboard_ready = False
    for sel in DASHBOARD_SELECTORS:
        try:
            sb.wait_for_element_present(sel, timeout=3)
            dashboard_ready = True
            log(f"✅ 仪表盘已就绪 ({sel})")
            break
        except Exception:
            continue

    if not dashboard_ready:
        try:
            body_len = sb.execute_script("return document.body.innerText.length")
            if body_len and int(body_len) > 100:
                log("✅ 仪表盘页面有内容，继续执行")
                dashboard_ready = True
        except Exception:
            pass

    if not dashboard_ready:
        log("仪表盘结构未识别，但继续执行", "WARN")

    time.sleep(2)
    block_ads_modals(sb)

    log("✅ 登录成功并进入仪表盘")
    return True


# ====================== 获取服务器列表与状态 ======================
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
                if (data.servers) {
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

    try:
        dom_ids = sb.execute_script('''
            var cards = document.querySelectorAll(
                '[data-server-id], .server-card, .server-item'
            );
            return Array.from(cards).map(function(el) {
                return el.getAttribute('data-server-id') || el.id;
            }).filter(Boolean);
        ''')
        if dom_ids:
            masked = [mask_server_id(i) for i in dom_ids]
            log(f"从 DOM 提取到服务器，共 {len(dom_ids)} 台: {masked}")
            return list(dom_ids)
    except Exception as e:
        log(f"DOM 提取失败: {e}", "WARN")

    log("未能获取任何服务器标识符", "ERROR")
    return []


def get_server_status(sb, identifier: str) -> Optional[str]:
    """查询单台服务器当前状态 (running / starting / stopping / offline)"""
    status_script = f'''
        var callback = arguments[arguments.length - 1];
        var serverId = {json.dumps(identifier)};
        fetch('/client/api/servers/status', {{
            method: 'GET',
            headers: {{ 'Accept': 'application/json' }}
        }})
        .then(function(res) {{ return res.json(); }})
        .then(function(data) {{
            var server = (data.servers || []).find(function(s) {{
                return s.identifier === serverId;
            }});
            callback(server ? server.current_state : null);
        }})
        .catch(function() {{ callback(null); }});
    '''
    try:
        res = sb.execute_async_script(status_script)
        return str(res).lower().strip() if res else None
    except Exception:
        return None


# ====================== 强制终止 (Kill) 辅助函数 ======================
def force_kill_server(sb, identifier: str) -> bool:
    """
    当容器(如 hy2/xray 等不响应 SIGTERM 的进程)长时间卡在 stopping 状态时，
    尝试通过面板 API 和控制台 DOM 按钮（包括 '...' 更多菜单和确认弹窗）强制 Kill 进程。
    """
    safe_id = mask_server_id(identifier)
    log(f"⚠️ 服务器 {safe_id} 卡在 stopping，开始执行强制终止 (Kill)...")

    # 1. 尝试调用同源 API 发送 kill 电源信号（兼容多种常见路由格式）
    try:
        api_res = sb.execute_async_script(f'''
            var done = arguments[arguments.length - 1];
            var id = {json.dumps(identifier)};
            var csrfMeta = document.querySelector('meta[name="csrf-token"]');
            var csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
            var headers = {{
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }};
            if (csrf) headers['X-CSRF-TOKEN'] = csrf;

            var endpoints = [
                {{url: '/client/api/servers/' + id + '/power', body: {{signal: 'kill', action: 'kill'}}}},
                {{url: '/client/servers/' + id + '/power', body: {{signal: 'kill', action: 'kill'}}}},
                {{url: '/api/client/servers/' + id + '/power', body: {{signal: 'kill'}}}},
                {{url: '/client/api/servers/' + id + '/kill', body: {{}}}}
            ];

            (async function() {{
                for (var i = 0; i < endpoints.length; i++) {{
                    try {{
                        var r = await fetch(endpoints[i].url, {{
                            method: 'POST',
                            headers: headers,
                            body: JSON.stringify(endpoints[i].body)
                        }});
                        if (r.ok || r.status === 204) {{
                            done({{ok: true, url: endpoints[i].url, status: r.status}});
                            return;
                        }}
                    }} catch (e) {{}}
                }}
                done({{ok: false}});
            }})();
        ''')
        if api_res and api_res.get("ok"):
            log(f"✅ 已通过 API 发送 Kill 信号 ({api_res.get('url')})")
    except Exception as e:
        log(f"API 发送 Kill 异常: {e}", "WARN")

    # 2. 尝试通过页面 DOM 点击 Kill / Force Stop（含展开 '...' 更多菜单）
    try:
        dom_res = sb.execute_async_script('''
            var done = arguments[arguments.length - 1];

            function findAndClickKill() {
                var els = Array.from(document.querySelectorAll('button, a, li, div[role="menuitem"], [role="button"], span'));
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    var st = window.getComputedStyle(el);
                    if (el.disabled || st.display === 'none' || st.visibility === 'hidden') continue;

                    var id = (el.id || '').toLowerCase();
                    var cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
                    var act = (el.getAttribute('data-action') || el.getAttribute('title') || el.getAttribute('aria-label') || '').toLowerCase();
                    var txt = (el.innerText || el.textContent || '').trim().toLowerCase();

                    if (
                        id.includes('kill') || act.includes('kill') ||
                        txt === 'kill' || txt === 'force stop' || txt === 'force kill' ||
                        txt.includes('kill server') || txt.includes('force stop') ||
                        txt.includes('强制停止') || txt.includes('强制终止')
                    ) {
                        el.click();
                        return txt || act || id || 'kill-btn';
                    }
                }
                return null;
            }

            // 第一步：直接找页面上可见的 Kill 按钮
            var direct = findAndClickKill();
            if (direct) {
                setTimeout(function() {
                    // 检查是否有二次确认弹窗按钮
                    var confirmBtns = Array.from(document.querySelectorAll('.modal button, [role="dialog"] button, .swal2-confirm, button.btn-danger'));
                    for (var j = 0; j < confirmBtns.length; j++) {
                        var b = confirmBtns[j];
                        var bt = (b.innerText || '').trim().toLowerCase();
                        if (bt.includes('kill') || bt.includes('confirm') || bt.includes('yes') || bt.includes('force') || bt === '确定') {
                            b.click();
                            break;
                        }
                    }
                    done({clicked: true, target: direct});
                }, 800);
                return;
            }

            // 第二步：如果没找到，尝试点击 Stop 按钮右侧的 '...' 更多菜单按钮
            var moreBtns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            var menuOpened = false;
            for (var k = 0; k < moreBtns.length; k++) {
                var mb = moreBtns[k];
                var mst = window.getComputedStyle(mb);
                if (mb.disabled || mst.display === 'none' || mst.visibility === 'hidden') continue;
                var mtxt = (mb.innerText || mb.textContent || '').trim();
                var mhtml = (mb.innerHTML || '').toLowerCase();
                var mtitle = (mb.getAttribute('title') || mb.getAttribute('aria-label') || '').toLowerCase();

                if (
                    mtxt === '...' || mtxt === '⋯' || mtxt === '•••' ||
                    mhtml.includes('ellipsis') || mhtml.includes('more-horizontal') ||
                    mhtml.includes('dots') || mtitle.includes('more') || mtitle.includes('更多')
                ) {
                    mb.click();
                    menuOpened = true;
                    break;
                }
            }

            if (menuOpened) {
                setTimeout(function() {
                    var afterMenu = findAndClickKill();
                    setTimeout(function() {
                        var confirmBtns = Array.from(document.querySelectorAll('.modal button, [role="dialog"] button, .swal2-confirm, button.btn-danger'));
                        for (var j = 0; j < confirmBtns.length; j++) {
                            var b = confirmBtns[j];
                            var bt = (b.innerText || '').trim().toLowerCase();
                            if (bt.includes('kill') || bt.includes('confirm') || bt.includes('yes') || bt.includes('force') || bt === '确定') {
                                b.click();
                                break;
                            }
                        }
                        done({clicked: !!afterMenu, target: afterMenu || 'menu-opened-no-kill'});
                    }, 800);
                }, 600);
                return;
            }

            done({clicked: false, target: null});
        ''')
        if dom_res and dom_res.get("clicked"):
            log(f"✅ 已通过 DOM 点击强制停止按钮: {dom_res.get('target')}")
    except Exception as e:
        log(f"DOM 查找 Kill 按钮异常: {e}", "WARN")

    # 3. 等待最多 15 秒确认状态脱离 stopping
    for _ in range(5):
        time.sleep(3)
        st = get_server_status(sb, identifier)
        if st and 'stopping' not in st:
            log(f"✅ 服务器 {safe_id} 已脱离 stopping 状态，当前状态: {st}")
            return True

    return False


# ====================== 点击启动按钮辅助函数 ======================
def try_click_start_button(sb, prefer_only_start: bool = False) -> bool:
    selectors = [
        ('button#start-btn', 'Start(#start-btn)'),
        ('[data-action="start"]', 'Start(data-action)'),
    ]
    if not prefer_only_start:
        selectors.extend([
            ('button#restart-btn', 'Restart(#restart-btn)'),
            ('[data-action="restart"]', 'Restart(data-action)'),
        ])

    for btn_sel, btn_name in selectors:
        try:
            if sb.is_element_visible(btn_sel):
                sb.click(btn_sel)
                log(f"✅ 已点击 {btn_name} 按钮")
                return True
        except Exception:
            continue

    try:
        res = sb.execute_async_script(f'''
            var preferOnlyStart = {json.dumps(prefer_only_start)};
            var done = arguments[arguments.length - 1];
            var els = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            var found = null;
            var debugBtns = [];

            for (var i = 0; i < els.length; i++) {{
                var el = els[i];
                var st = window.getComputedStyle(el);
                if (el.disabled || st.display === 'none' || st.visibility === 'hidden') continue;

                var id = (el.id || '').toLowerCase();
                var cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
                var txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                var title = (el.getAttribute('title') || el.getAttribute('aria-label') || el.getAttribute('data-action') || '').toLowerCase();
                var html = (el.innerHTML || '').toLowerCase();

                if (el.tagName === 'BUTTON') {{
                    debugBtns.push(el.tagName + '#' + id + '.' + cls.split(' ')[0] + '(' + txt.slice(0, 12) + ')');
                }}

                if (txt === 'skip' || txt === '跳过' || txt.includes('stop') || txt.includes('kill')) continue;
                if (preferOnlyStart && (id.includes('restart') || title.includes('restart') || txt.includes('restart') || txt.includes('重启'))) continue;

                if (
                    id.includes('start') || id.includes('restart') ||
                    title.includes('start') || title.includes('restart') || title.includes('启动') || title.includes('重启') ||
                    txt === 'start' || txt === 'restart' || txt === '启动' || txt === '重启' ||
                    txt.includes('start server') || txt.includes('restart server') ||
                    html.includes('fa-play') || html.includes('bi-play') ||
                    (el.tagName === 'BUTTON' && html.includes('<polygon')) ||
                    (el.tagName === 'BUTTON' && html.includes('m8 5v14l11-7z')) ||
                    (el.tagName === 'BUTTON' && html.includes('5 3 19 12 5 21'))
                ) {{
                    el.click();
                    found = (txt || title || id || 'play-icon-btn');
                    break;
                }}
            }}
            done({{clicked: !!found, target: found, allButtons: debugBtns.slice(0, 25)}});
        ''')
        if res and res.get("clicked"):
            log(f"✅ 已通过 DOM 匹配点击启动按钮: {res.get('target')}")
            return True
        else:
            all_btns = res.get("allButtons", []) if isinstance(res, dict) else []
            log(f"当前页面可见按钮列表: {all_btns}", "INFO")
    except Exception as e:
        log(f"查找启动按钮异常: {e}", "WARN")

    return False


def _trigger_start_and_verify(sb, identifier: str, console_url: str, prefer_only_start: bool = False) -> bool:
    """点击启动/重启按钮，并处理可能随之触发的广告与 CF Turnstile 验证"""
    clicked = try_click_start_button(sb, prefer_only_start=prefer_only_start)

    if not clicked:
        log("等待 4 秒后重试查找控制台启动按钮...")
        time.sleep(4)
        block_ads_modals(sb)
        clicked = try_click_start_button(sb, prefer_only_start=prefer_only_start)

    if not clicked:
        log("控制台未找到启动按钮，返回仪表盘尝试点击 [▷] 快捷启动按钮...")
        sb.get(DASHBOARD_URL)
        time.sleep(5)
        block_ads_modals(sb)
        clicked = try_click_start_button(sb, prefer_only_start=prefer_only_start)

    if not clicked:
        log("未找到可用的 Start/Restart 按钮", "ERROR")
        return False

    time.sleep(3)

    # 处理广告流程
    log("=== 开始处理广告流程 ===")
    handle_reward_ad_flow(sb, identifier, console_url)
    log("=== 广告流程处理完毕 ===")

    # 确保回到控制台页面
    time.sleep(2)
    current_url = sb.get_current_url()
    if not is_console_url(current_url, identifier) or is_ad_or_reward_url(current_url):
        log(f"当前不在控制台页面（{current_url[:80]}），重新导航...")
        sb.get(console_url)
        time.sleep(5)
        block_ads_modals(sb)
    else:
        log("当前在控制台页面，无需重新导航")
        block_ads_modals(sb)

    # 处理 CF Turnstile 验证弹窗
    log("=== 开始处理 CF Turnstile 验证 ===")
    cf_result = handle_restart_turnstile_modal(sb, timeout=90)
    if not cf_result:
        log("CF Turnstile 验证失败", "WARN")
    else:
        log("=== CF Turnstile 验证完成 ===")

    block_ads_modals(sb)
    return True


# ====================== 重启服务器（完整流程）======================
def restart_server(sb, identifier: str) -> bool:
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"{'─'*40}")
    log(f"重启服务器: {safe_id}")
    log(f"{'─'*40}")

    # ── Step 1: 导航到控制台 ──
    log(f"导航到控制台: {safe_id}")
    sb.get(console_url)
    time.sleep(6)
    block_ads_modals(sb)

    # 检查初始状态是否就已经卡在 stopping
    init_status = get_server_status(sb, identifier)
    if init_status and 'stopping' in init_status:
        log(f"检测到服务器 {safe_id} 初始状态即为 stopping，先执行强制 Kill...")
        force_kill_server(sb, identifier)
        sb.get(console_url)
        time.sleep(5)
        block_ads_modals(sb)

    # ── Step 2~5: 点击 Start/Restart 并处理广告与 CF 验证 ──
    if not _trigger_start_and_verify(sb, identifier, console_url):
        return False

    # ── Step 6: 轮询服务器状态（带卡死 stopping 自动 Kill 恢复机制） ──
    poll_timeout = 120
    poll_interval = 5
    start_poll = time.time()
    stopping_count = 0
    offline_count = 0
    kill_attempted = False

    log(f"开始轮询服务器状态（最长 {poll_timeout} 秒）: {safe_id}")

    while time.time() - start_poll < poll_timeout:
        status = get_server_status(sb, identifier)

        if status and 'running' in status:
            log(f"✅ 服务器 {safe_id} 状态: {status}，重启成功！")
            return True

        if status and 'stopping' in status:
            stopping_count += 1
            offline_count = 0
            log(f"当前状态: {status} (连续第 {stopping_count} 次)，{poll_interval}s 后重试...")

            # 如果连续 5 次（约 25 秒）都卡在 stopping，执行强制 Kill 并重新点击 Start
            if stopping_count >= 5 and not kill_attempted:
                kill_attempted = True
                force_kill_server(sb, identifier)

                # 刷新控制台并重新点击 Start 按钮拉起服务器
                log(f"刷新控制台并尝试重新启动服务器: {safe_id}")
                sb.get(console_url)
                time.sleep(5)
                block_ads_modals(sb)

                current_st = get_server_status(sb, identifier)
                if not current_st or ('running' not in current_st and 'starting' not in current_st):
                    _trigger_start_and_verify(sb, identifier, console_url, prefer_only_start=True)
                    # 重置计时器，再给 60 秒等待启动
                    start_poll = time.time()
                    poll_timeout = 60
                continue

        elif status and ('offline' in status or 'stopped' in status):
            offline_count += 1
            stopping_count = 0
            log(f"当前状态: {status} (连续第 {offline_count} 次)，{poll_interval}s 后重试...")

            # 如果服务器已变为 offline 超过 2 次（10 秒）仍未自动进入 starting，手动补点一次 Start
            if offline_count >= 2:
                log(f"服务器 {safe_id} 处于离线状态未自动启动，尝试点击 Start 按钮...")
                offline_count = 0
                sb.get(console_url)
                time.sleep(5)
                block_ads_modals(sb)
                _trigger_start_and_verify(sb, identifier, console_url, prefer_only_start=True)
                continue
        else:
            stopping_count = 0
            offline_count = 0
            log(f"当前状态: {status or '未知'}，{poll_interval}s 后重试...")

        time.sleep(poll_interval)

    # 最终检查
    status = get_server_status(sb, identifier)
    if status and 'running' in status:
        log(f"✅ 最终检查成功: {safe_id} 状态: {status}")
        return True

    log(f"❌ 轮询超时，最终状态: {status or '未知'}", "ERROR")
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
