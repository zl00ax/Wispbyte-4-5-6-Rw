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


# ====================== 广告弹窗 CSS 屏蔽 ======================
def block_ads_modals(sb):
    """屏蔽干扰性广告弹窗（不屏蔽广告本身，只屏蔽无关弹窗）"""
    css = """
    .wisp-offer-modal, .instagram-modal, .qc-cmp2-summary-section {
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

    # 先等弹窗出现（最多 20 秒）
    modal_appeared = False
    for _ in range(20):
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
        # 弹窗从未出现，可能服务器已启动或不需要验证
        log("CF Turnstile 弹窗未出现，可能无需验证", "WARN")
        return True

    last_click = 0
    while time.time() - start < timeout:
        try:
            # 检查弹窗是否还存在
            modal_visible = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if not modal_visible:
                log("✅ CF Turnstile 弹窗已关闭，验证完成")
                return True

            # 检查是否已解决
            if check_turnstile_solved(sb):
                log("Turnstile 已解决，等待弹窗自动关闭...")
                # 等待弹窗自动关闭（最多 15 秒）
                for _ in range(15):
                    closed = sb.execute_script('''
                        var el = document.querySelector('.wisp-start-captcha-modal');
                        return !(el && getComputedStyle(el).display !== 'none');
                    ''')
                    if closed:
                        log("✅ 弹窗已自动关闭")
                        return True
                    time.sleep(1)
                # 尝试手动关闭
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
                return True  # 即使关闭失败，Turnstile已完成视为成功

            # 尝试点击 Turnstile
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

    # 超时最终检查
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
    """
    检测当前页面/弹出情况，返回:
      'adblocker'  - 检测到广告拦截器页面
      'reward'     - 在广告奖励页面（reward-video page）
      'alert'      - 有 JS alert 弹窗（No ad available）
      'console'    - 在控制台页面（正常）
      'unknown'    - 未知
    """
    try:
        current_url = sb.get_current_url()
    except Exception:
        return 'unknown'

    # 检查是否在 reward video 页面
    if "reward-video" in current_url or "reward_video" in current_url:
        return 'reward'

    # 检查 AdBlocker 拦截页
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

    # 检查广告按钮是否在当前页面出现（内嵌情况）
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
    """
    处理 JS alert 弹窗（如 'No ad available right now...'）
    返回 True 如果处理了弹窗
    """
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
    """
    处理广告拦截器检测页面。
    点击 'Check again' 按钮。
    """
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
    """
    等待广告奖励页面的 embedWatchBtn 变为可点击状态。
    就绪条件：
      - embedPlayBtn 显示 (display: flex)
      - embedStatus 已隐藏（说明广告加载完成）
    """
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

                // panel 必须是 flex（showEmbedPlayButton 设置的）
                if (panelDisplay === 'none') {
                    // 检查 embedStatus 当前内容
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
    """
    点击 embedWatchBtn 按钮。
    使用多种方式确保点击成功。
    """
    log("点击 'Watch ad to continue' 按钮...")
    methods = [
        # 方式1: JS click（最可靠，绕过遮挡）
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.click();
            return true;
        '''),
        # 方式2: SeleniumBase click
        lambda: (sb.click('#embedWatchBtn') or True),
        # 方式3: JS dispatchEvent
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
    """
    等待广告观看完成。
    完成信号（任意一个触发即可）：
      1. URL 回到控制台页面（含 identifier）
      2. URL 含 rewardDone=1
      3. embedStatus 显示 "starting/saving/returning/session"
      4. 页面离开 reward-video URL 且非 adblocker 页
    """
    safe_id = mask_server_id(identifier)
    log(f"广告开始播放，等待完成（最长 {timeout}s）: {safe_id}")
    start = time.time()
    console_path = f"/servers/{identifier}/console"

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            current_url = sb.get_current_url()

            # 信号1: URL 含 rewardDone=1
            if "rewardDone=1" in current_url:
                log(f"✅ 广告完成 [rewardDone]: {safe_id}")
                return True

            # 信号2: 回到控制台
            if console_path in current_url:
                log(f"✅ 广告完成 [回到控制台]: {safe_id}")
                return True

            # 信号3: embedStatus 显示完成文字
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
                    if any(kw in text for kw in ['starting', 'saving', 'returning', 'session']):
                        log(f"✅ 广告完成 [embedStatus='{text[:50]}']: {safe_id}")
                        # 等待实际跳转
                        time.sleep(8)
                        return True
            except Exception:
                pass

            # 信号4: 离开 reward 页面
            if "reward-video" not in current_url and elapsed > 10:
                situation = _get_page_situation(sb)
                if situation not in ('reward', 'adblocker', 'unknown'):
                    log(f"✅ 广告完成 [页面已跳转到: {current_url[:80]}]: {safe_id}")
                    return True

            if elapsed % 15 == 0:
                log(f"等待广告完成... [{elapsed}s] URL={current_url[:60]}")

        except Exception as e:
            log(f"广告完成检测异常: {e}", "WARN")

        time.sleep(3)

    log(f"⚠️ 广告等待超时 ({timeout}s): {safe_id}", "WARN")
    return False


def handle_reward_ad_flow(sb, identifier: str, console_url: str) -> bool:
    """
    完整广告流程处理器。
    在点击 Start 按钮后调用。

    处理以下所有情况：
      A. 跳转到 reward-video 页面 → 等待按钮 → 点击 → 等待完成
      B. Alert 弹窗 "No ad available" → 关闭弹窗 → 继续（走CF验证）
      C. AdBlocker 检测页 → 点击 Check again → 重新检测
      D. 没有广告页面 → 直接返回 True（继续CF验证）

    返回 True 表示可以继续执行 CF 验证流程
    """
    safe_id = mask_server_id(identifier)
    log(f"广告流程处理开始: {safe_id}")

    # 等待页面响应（最多 15 秒）
    ad_flow_timeout = 15
    start = time.time()

    while time.time() - start < ad_flow_timeout:
        # 优先处理 Alert 弹窗（因为 alert 会阻塞其他操作）
        if _dismiss_alert_if_present(sb):
            log("✅ 已处理 Alert 弹窗（无广告情况），继续CF验证")
            return True

        situation = _get_page_situation(sb)
        log(f"当前页面情况: {situation}")

        if situation == 'reward':
            # 进入广告观看流程
            return _execute_reward_ad_watch(sb, identifier)

        elif situation == 'adblocker':
            # 处理广告拦截器检测页
            log("检测到广告拦截器页面")
            _handle_adblocker_page(sb)
            time.sleep(3)
            # 再次检查（可能变成 reward 或 alert）
            continue

        elif situation == 'console':
            log("当前在控制台页面，无需处理广告")
            return True

        time.sleep(1)

    # 超时：再检查一次 alert
    if _dismiss_alert_if_present(sb):
        log("✅ 超时后处理 Alert 弹窗")
        return True

    log("广告流程等待超时，继续执行CF验证", "WARN")
    return True


def _execute_reward_ad_watch(sb, identifier: str) -> bool:
    """
    在 reward-video 页面执行完整广告观看流程。
    返回 True 表示广告流程结束（无论成功与否都应继续CF验证）
    """
    safe_id = mask_server_id(identifier)
    log(f"进入广告观看流程: {safe_id}")

    # 等待 Watch ad 按钮就绪
    btn_ready = _wait_for_reward_btn_ready(sb, timeout=90)

    if not btn_ready:
        # 按钮未就绪，检查是否有 alert（No ad available）
        if _dismiss_alert_if_present(sb):
            log("✅ 广告按钮未就绪但检测到 Alert（无广告），继续CF验证")
            return True

        # 检查是否 failRewardReturn 已经把页面跳回
        current_url = sb.get_current_url()
        if "reward-video" not in current_url:
            log(f"广告页面已自动跳转: {current_url[:80]}")
            return True

        log("广告按钮未就绪，继续CF验证", "WARN")
        return True

    # 在点击前再检查一次 alert
    if _dismiss_alert_if_present(sb):
        log("✅ 点击前检测到 Alert（无广告），继续CF验证")
        return True

    # 点击 Watch ad 按钮
    if not _click_watch_ad_btn(sb):
        log("广告按钮点击失败，继续CF验证", "WARN")
        return True

    # 等待广告完成
    _wait_for_ad_completion(sb, identifier, timeout=300)

    # 广告完成后处理可能的 alert
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

    # 备用 DOM 提取
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


# ====================== 重启服务器（完整流程）======================
def restart_server(sb, identifier: str) -> bool:
    """
    完整重启流程：
    1. 导航到控制台
    2. 点击 Start/Restart 按钮
    3. 处理广告流程（reward video / alert / adblocker）
    4. 确保回到控制台页面
    5. 处理 CF Turnstile 验证弹窗
    6. 轮询服务器状态
    """
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"{'─'*40}")
    log(f"重启服务器: {safe_id}")
    log(f"{'─'*40}")

    # ── Step 1: 导航到控制台 ──
    log(f"导航到控制台: {safe_id}")
    sb.get(console_url)
    time.sleep(5)
    block_ads_modals(sb)

    # ── Step 2: 点击 Start / Restart ──
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
        try:
            sb.execute_script("document.querySelector('#start-btn, #restart-btn').click()")
            log("✅ 已通过 JS 点击 Start/Restart 按钮")
        except Exception as e:
            log(f"点击 Start/Restart 失败: {e}", "ERROR")
            return False

    # 等待页面响应
    time.sleep(3)

    # ── Step 3: 处理广告流程 ──
    log("=== 开始处理广告流程 ===")
    handle_reward_ad_flow(sb, identifier, console_url)
    log("=== 广告流程处理完毕 ===")

    # ── Step 4: 确保回到控制台页面 ──
    time.sleep(2)
    current_url = sb.get_current_url()
    if identifier not in current_url or "reward" in current_url:
        log(f"当前不在控制台页面（{current_url[:80]}），重新导航...")
        sb.get(console_url)
        time.sleep(5)
        block_ads_modals(sb)
    else:
        log(f"当前在控制台页面，无需重新导航")
        block_ads_modals(sb)

    # ── Step 5: 处理 CF Turnstile 验证弹窗 ──
    log("=== 开始处理 CF Turnstile 验证 ===")
    cf_result = handle_restart_turnstile_modal(sb, timeout=90)
    if not cf_result:
        log("CF Turnstile 验证失败", "WARN")
        # 不直接返回 False，继续尝试轮询
    else:
        log("=== CF Turnstile 验证完成 ===")

    block_ads_modals(sb)

    # ── Step 6: 轮询服务器状态 ──
    log(f"开始轮询服务器状态（最长 60 秒）: {safe_id}")
    poll_timeout = 60
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
            var server = data.servers.find(function(s) {{
                return s.identifier === serverId;
            }});
            callback(server ? server.current_state : null);
        }})
        .catch(function() {{ callback(null); }});
    '''

    while time.time() - start_poll < poll_timeout:
        try:
            status = sb.execute_async_script(status_script)
            if status and 'running' in str(status).lower():
                log(f"✅ 服务器 {safe_id} 状态: {status}，重启成功！")
                return True
            log(f"当前状态: {status or '未知'}，{poll_interval}s 后重试...")
        except Exception as e:
            log(f"状态检查异常: {e}", "WARN")
        time.sleep(poll_interval)

    # 最终检查
    try:
        status = sb.execute_async_script(status_script)
        if status and 'running' in str(status).lower():
            log(f"✅ 最终检查成功: {safe_id} 状态: {status}")
            return True
        log(f"❌ 轮询超时，最终状态: {status or '未知'}", "ERROR")
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
