"""Windows automation for opening Video Channels in the running WeChat client."""

from __future__ import annotations

import ctypes
import os
import statistics
import sys
import threading
import time
from ctypes import wintypes


_channels_environment_lock = threading.Lock()


# Binary outline extracted from the Video Channels icon supplied by the user.
# Matching the outline instead of fixed coordinates keeps the click safe when the
# sidebar shifts because of window size or display scaling.
VIDEO_CHANNELS_ICON_TEMPLATE = (
    ".####..........####.",
    "######........######",
    "##.###........##..##",
    "##..###......###..##",
    "##...###....###...##",
    "###...##....##...###",
    "###...###..###...###",
    "###....######....###",
    ".##.....####.....##.",
    ".##.....####.....##.",
    ".##.....####....###.",
    ".###....####....###.",
    "..##...######...##..",
    "..###.###..###.###..",
    "...#####....#####...",
    "...####......####...",
)


# Binary outline extracted from the Favorites icon supplied by the user.  In
# Weixin 4.x, Discover is the navigation item immediately below this stable
# cube icon even though avatars, badges, themes, and Discover artwork vary.
FAVORITES_ICON_TEMPLATE = (
    ".......####.......",
    ".....###..###.....",
    "...###.....###....",
    "..###........###..",
    ".##............##.",
    "###............###",
    "#####........#####",
    "#...###....###..##",
    "#....###.###....##",
    "#......####.....##",
    "#.......##......##",
    "#.......##......##",
    "#.......##......##",
    "##......##......##",
    "###.....##.....##.",
    ".###....##....###.",
    "...###..##..###...",
    ".....########.....",
    "......######......",
)


_ICON_SCALES = (0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


def _scaled_template(source, scale: float):
    source_height = len(source)
    source_width = len(source[0])
    width = max(1, round(source_width * scale))
    height = max(1, round(source_height * scale))
    rows = []
    for y in range(height):
        source_y = min(source_height - 1, int(y / scale))
        row = []
        for x in range(width):
            source_x = min(source_width - 1, int(x / scale))
            row.append(source[source_y][source_x] == "#")
        rows.append(row)
    return rows


def _find_icon(
    grayscale,
    source,
    *,
    min_y=0,
    max_y=None,
    min_x=8,
    max_x=None,
    minimum_score=0.52,
):
    if not grayscale or not grayscale[0]:
        return None

    image_height = len(grayscale)
    image_width = len(grayscale[0])
    search_top = max(0, min(int(min_y), image_height - 1))
    search_bottom = image_height if max_y is None else min(image_height, int(max_y))
    search_left = max(0, int(min_x))
    search_right = image_width if max_x is None else min(image_width, int(max_x))
    if search_top >= search_bottom or search_left >= search_right:
        return None
    best = None

    # The navigation/menu surfaces are effectively flat-colour regions. Build
    # one foreground mask and summed-area table so each candidate rectangle can
    # be rejected in O(1); only plausible rectangles need point-by-point shape
    # comparison. This keeps the launcher responsive without shipping OpenCV.
    sample_step = max(1, min(image_width, image_height) // 40)
    samples = [
        grayscale[y][x]
        for y in range(search_top, search_bottom, sample_step)
        for x in range(search_left, search_right, sample_step)
    ]
    background = statistics.median(samples)
    if background >= 128:
        foreground = [
            [value < background - 28 for value in row]
            for row in grayscale
        ]
    else:
        foreground = [
            [value > background + 28 for value in row]
            for row in grayscale
        ]

    integral = [[0] * (image_width + 1) for _ in range(image_height + 1)]
    for y, row in enumerate(foreground):
        running = 0
        previous = integral[y]
        current = integral[y + 1]
        for x, value in enumerate(row):
            running += value
            current[x + 1] = previous[x + 1] + running

    for scale in _ICON_SCALES:
        template = _scaled_template(source, scale)
        height = len(template)
        width = len(template[0])
        target_count = sum(sum(row) for row in template)
        target_points = [
            (x, y)
            for y, row in enumerate(template)
            for x, value in enumerate(row)
            if value
        ]
        if width > search_right - search_left or height > search_bottom - search_top:
            continue

        for top in range(search_top, search_bottom - height + 1):
            for left in range(search_left, search_right - width + 1):
                right = left + width
                bottom = top + height
                observed_count = (
                    integral[bottom][right]
                    - integral[top][right]
                    - integral[bottom][left]
                    + integral[top][left]
                )
                if observed_count < target_count * 0.35 or observed_count > target_count * 2.2:
                    continue

                intersection = sum(
                    foreground[top + y][left + x]
                    for x, y in target_points
                )
                score = (2.0 * intersection) / (target_count + observed_count)
                if best is None or score > best["score"]:
                    best = {
                        "x": left + width // 2,
                        "y": top + height // 2,
                        "score": score,
                        "left": left,
                        "top": top,
                        "width": width,
                        "height": height,
                    }

    if best is None or best["score"] < minimum_score:
        return None
    return best


def find_video_channels_icon(
    grayscale, min_y=150, max_y=None, min_x=8, max_x=None
):
    """Return the best Video Channels icon center in a grayscale image."""
    return _find_icon(
        grayscale,
        VIDEO_CHANNELS_ICON_TEMPLATE,
        min_y=min_y,
        max_y=max_y,
        min_x=min_x,
        max_x=max_x,
    )


def find_favorites_icon(grayscale, min_y=120, max_y=None):
    """Return the Favorites cube center used to anchor the Discover entry."""
    return _find_icon(
        grayscale,
        FAVORITES_ICON_TEMPLATE,
        min_y=min_y,
        max_y=max_y,
        min_x=8,
        max_x=None,
        minimum_score=0.56,
    )


def _is_direct_channels_entry(channels_match, favorites_match, scale):
    """Distinguish the old direct entry from the similarly shaped Discover icon."""
    if channels_match is None:
        return False
    if favorites_match is None:
        return True
    expected_y = favorites_match["y"] + round(50 * scale)
    return (
        channels_match["score"] >= 0.68
        and abs(channels_match["x"] - favorites_match["x"]) <= round(8 * scale)
        and abs(channels_match["y"] - expected_y) <= round(15 * scale)
    )


def _query_process_path(pid):
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(process)


def _window_rect(hwnd):
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("无法读取微信窗口位置")
    return rect


def _enumerate_wechat_windows():
    """Read native WeChat windows, including minimized ones, without activation."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    windows = []
    processes = {}
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in processes:
            processes[pid.value] = os.path.basename(_query_process_path(pid.value)).lower()
        executable = processes[pid.value]
        if executable not in {"weixin.exe", "wechat.exe", "wechatappex.exe"}:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))

        windows.append({
            "hwnd": hwnd, "pid": pid.value, "title": title_buffer.value,
            "class_name": class_buffer.value, "executable": executable,
            "minimized": bool(user32.IsIconic(hwnd)),
        })
        return True

    callback = callback_type(collect)
    user32.EnumWindows(callback, 0)
    return windows


def _find_wechat_window():
    windows = [window for window in _enumerate_wechat_windows()
               if window["executable"] in {"weixin.exe", "wechat.exe"}
               and window["class_name"] in {"Qt51514QWindowIcon", "WeChatMainWndForPC"}
               and window["title"].lower() in {"微信", "wechat", "weixin"}]
    if not windows:
        return None

    # Prefer a titled main window that is already restored.
    windows.sort(key=lambda item: item["minimized"])
    return windows[0]


def find_wechat_browser_windows():
    """Find the embedded browser separately from the WeChat chat/main window.

    Weixin 4.x labels its Channels browser simply '微信', even when minimized.
    Native presence is therefore separate from the injected page's readiness.
    """
    windows = [window for window in _enumerate_wechat_windows()
               if window["executable"] in {"weixin.exe", "wechat.exe", "wechatappex.exe"}
               and window["class_name"].startswith("Chrome_WidgetWin_")
               and (window["title"].lower() in {"微信", "wechat", "weixin"}
                    or "视频号" in window["title"])]
    windows.sort(key=lambda item: ("视频号" not in item["title"], item["minimized"]))
    return windows


def restore_wechat_browser_window(hwnd):
    """Restore the existing minimized browser; never navigate or click WeChat."""
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    if not user32.IsWindow(hwnd) or not user32.IsIconic(hwnd):
        return False
    return bool(user32.ShowWindowAsync(hwnd, 9))  # SW_RESTORE, same HWND/page


def inspect_wechat_channels_environment():
    """Read-only diagnostics: do not start the proxy, restore, click, or reload."""
    from backend.mitm_proxy import ProxyManager, wait_for_channels_page

    manager = ProxyManager.get_instance()
    browsers = find_wechat_browser_windows()
    page = wait_for_channels_page(time.time() - 20, timeout=0) if manager.running else None
    return {
        "proxy_running": manager.running,
        "browser_open": bool(browsers),
        "browser_windows": browsers,
        "monitoring_active": page is not None,
    }


def _restore_and_focus(hwnd):
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached_threads = []

    # Attach to the current foreground queue so Windows permits a background
    # Flask worker to activate WeChat when the user presses our button.
    for thread_id in {foreground_thread, target_thread}:
        if thread_id and thread_id != current_thread:
            if user32.AttachThreadInput(current_thread, thread_id, True):
                attached_threads.append(thread_id)
    try:
        user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        # The synthetic Alt press is a second activation path for newer Windows.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
    finally:
        for thread_id in attached_threads:
            user32.AttachThreadInput(current_thread, thread_id, False)

    deadline = time.time() + 3
    while time.time() < deadline:
        if not user32.IsIconic(hwnd) and user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.1)
        user32.ShowWindowAsync(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
    return not user32.IsIconic(hwnd) and user32.GetForegroundWindow() == hwnd


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def _capture_grayscale(left, top, width, height):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    handle = ctypes.c_void_p
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = handle
    user32.ReleaseDC.argtypes = [wintypes.HWND, handle]
    gdi32.CreateCompatibleDC.argtypes = [handle]
    gdi32.CreateCompatibleDC.restype = handle
    gdi32.CreateCompatibleBitmap.argtypes = [handle, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = handle
    gdi32.SelectObject.argtypes = [handle, handle]
    gdi32.SelectObject.restype = handle
    gdi32.BitBlt.argtypes = [
        handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        handle, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.GetDIBits.argtypes = [
        handle, handle, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(_BitmapInfo), wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteDC.argtypes = [handle]

    screen_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    bitmap_selected = True
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020):
            raise RuntimeError("无法截取微信导航栏")

        # GetDIBits requires the bitmap not to be selected into a device context.
        gdi32.SelectObject(memory_dc, previous)
        bitmap_selected = False

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down pixels
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(
            memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0
        ):
            raise RuntimeError("无法读取微信导航栏图像")

        raw = buffer.raw
        rows = []
        for y in range(height):
            row = []
            offset = y * width * 4
            for x in range(width):
                blue, green, red = raw[offset:offset + 3]
                row.append((red * 77 + green * 150 + blue * 29) >> 8)
                offset += 4
            rows.append(row)
        return rows
    finally:
        if bitmap_selected:
            gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def _click_wechat_client_point(hwnd, x, y):
    """Send a click directly to WeChat so another foreground app is never clicked."""
    user32 = ctypes.windll.user32
    packed_point = (int(y) << 16) | (int(x) & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0200, 0, packed_point)  # WM_MOUSEMOVE
    user32.PostMessageW(hwnd, 0x0201, 0x0001, packed_point)  # WM_LBUTTONDOWN
    time.sleep(0.08)
    user32.PostMessageW(hwnd, 0x0202, 0, packed_point)  # WM_LBUTTONUP


def _window_scale(hwnd):
    dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) or 96
    return dpi / 96.0


def open_wechat_video_channels():
    """Restore WeChat and open Video Channels through either supported entry."""
    if sys.platform != "win32":
        raise RuntimeError("当前自动打开流程仅支持 Windows 微信客户端")

    window = _find_wechat_window()
    if not window:
        raise RuntimeError("未找到已登录的微信主窗口，请先启动并登录电脑版微信")

    hwnd = window["hwnd"]
    focused = _restore_and_focus(hwnd)

    time.sleep(0.6)
    rect = _window_rect(hwnd)
    window_width = rect.right - rect.left
    window_height = rect.bottom - rect.top
    if window_width < 500 or window_height < 500:
        raise RuntimeError("微信主窗口尺寸异常，请先恢复窗口后重试")

    scale = _window_scale(hwnd)
    nav_width = min(window_width, max(64, round(76 * scale)))
    nav_height = min(window_height, max(420, round(560 * scale)))
    channels_match = None
    favorites_match = None
    if focused:
        try:
            grayscale = _capture_grayscale(rect.left, rect.top, nav_width, nav_height)
            favorites_match = find_favorites_icon(
                grayscale,
                min_y=round(110 * scale),
                max_y=min(nav_height, round(430 * scale)),
            )
            channels_match = find_video_channels_icon(
                grayscale,
                min_y=round(170 * scale),
                max_y=min(nav_height, round(430 * scale)),
            )
            if favorites_match is not None:
                # The cube resembles the butterfly at low resolution, so scan
                # its next slot explicitly instead of accepting an overlapping
                # false match on Favorites itself.
                channels_match = find_video_channels_icon(
                    grayscale,
                    min_y=favorites_match["y"] + round(20 * scale),
                    max_y=min(
                        nav_height,
                        favorites_match["y"] + round(80 * scale),
                    ),
                    min_x=max(8, favorites_match["x"] - round(25 * scale)),
                    max_x=min(
                        nav_width,
                        favorites_match["x"] + round(25 * scale),
                    ),
                )
                if not _is_direct_channels_entry(
                    channels_match, favorites_match, scale
                ):
                    channels_match = None
        except RuntimeError:
            channels_match = None
            favorites_match = None

    anchor_match_score = None
    if channels_match:
        click_x = channels_match["x"]
        click_y = channels_match["y"]
        click_method = "icon_match"
        match_score = round(channels_match["score"], 3)
    elif favorites_match:
        # The Discover icon itself is not stable across accounts/themes. Its
        # slot is stable relative to Favorites: 50 logical pixels below it.
        discover_x = favorites_match["x"]
        discover_y = favorites_match["y"] + round(50 * scale)
        if discover_y >= nav_height:
            raise RuntimeError("已识别收藏图标，但发现入口超出微信导航栏范围")
        _click_wechat_client_point(hwnd, discover_x, discover_y)

        # Discover opens inside the main window. Match the unchanged orange
        # Video Channels outline in its first few menu rows instead of relying
        # on account-specific artwork or fixed absolute coordinates.
        menu_width = min(window_width, max(round(320 * scale), nav_width + 140))
        menu_height = min(window_height, max(round(330 * scale), 280))
        menu_match = None
        for _ in range(3):
            time.sleep(0.3)
            try:
                menu_grayscale = _capture_grayscale(
                    rect.left, rect.top, menu_width, menu_height
                )
            except RuntimeError:
                continue
            menu_match = find_video_channels_icon(
                menu_grayscale,
                min_y=round(70 * scale),
                max_y=min(menu_height, round(235 * scale)),
                min_x=round(58 * scale),
                max_x=min(menu_width, round(165 * scale)),
            )
            if menu_match:
                break
        if menu_match is None:
            raise RuntimeError("已通过收藏图标打开发现，但未识别到视频号按钮")

        click_x = menu_match["x"]
        click_y = menu_match["y"]
        click_method = "favorites_anchor_discover_menu"
        match_score = round(menu_match["score"], 3)
        anchor_match_score = round(favorites_match["score"], 3)
    elif window["class_name"] == "Qt51514QWindowIcon":
        # Weixin 4.x exposes no accessibility tree. Its left navigation slots are
        # stable in older layouts. This remains the final compatibility fallback
        # only when neither the old Channels icon nor the new Favorites anchor
        # can be identified.
        click_x = round(38 * scale)
        click_y = round(307 * scale)
        click_method = "verified_weixin_sidebar_slot"
        match_score = None
    else:
        raise RuntimeError("已打开微信，但未识别到视频号图标；请确认微信左侧导航栏可见")

    _click_wechat_client_point(hwnd, click_x, click_y)
    _restore_and_focus(hwnd)

    return {
        "window_found": True,
        "window_title": window["title"],
        "icon_found": True,
        "icon_match_score": match_score,
        "anchor_match_score": anchor_match_score,
        "click_method": click_method,
        "clicked": True,
    }


def ensure_wechat_channels_available(
    detection_timeout=20.0, open_timeout=8.0
):
    """Reuse a connected Channels page before attempting any desktop action.

    The injected page's poll proves that the browser is on Channels and can
    accept collection commands. A WeChat process, window title, or a request for
    a static asset alone does not prove that the environment is usable.
    """
    if sys.platform != "win32":
        raise RuntimeError("当前自动打开流程仅支持 Windows 微信客户端")
    from backend.mitm_proxy import ProxyManager, wait_for_channels_page

    with _channels_environment_lock:
        checked_at = time.time()
        browsers = find_wechat_browser_windows()
        manager = ProxyManager.get_instance()
        proxy_started = False
        if not manager.running:
            proxy_started = bool(manager.start())
            if not proxy_started:
                raise RuntimeError("微信极速同步助手启动失败，请检查本地代理端口")

        # A minimized Chromium webview may suspend/throttle its injected poll.
        # Restore that exact browser, not the WeChat main window or sidebar.
        browser_restored = False
        if browsers and browsers[0].get("minimized"):
            browser_restored = restore_wechat_browser_window(browsers[0]["hwnd"])

        # Allow an already-open page to reconnect after a client/proxy restart.
        # Require a fresh response: old state can belong to a now-closed page.
        # Legacy pages report every 15 seconds while collecting, so allow 20.
        page = wait_for_channels_page(checked_at, timeout=detection_timeout)
        if page is not None:
            return {
                "proxy_running": manager.running,
                "proxy_started": proxy_started,
                "monitoring_active": True,
                "opened": False,
                "browser_open": bool(browsers),
                "browser_restored": browser_restored,
                "message": (
                    "已恢复现有视频号浏览器，采集接口已就绪，未重新打开页面"
                    if browser_restored else "检测到现有微信视频号页面可用，已跳过打开步骤"
                ),
            }

        # No heartbeat is not proof that the browser is absent. Recheck native
        # windows in case the user opened one while we waited, and never click
        # the sidebar again when an embedded browser already exists.
        browsers = find_wechat_browser_windows()
        if browsers:
            return {
                "proxy_running": manager.running,
                "proxy_started": proxy_started,
                "monitoring_active": False,
                "opened": False,
                "browser_open": True,
                "browser_restored": browser_restored,
                "message": "检测到微信内置浏览器已打开，未重复打开；视频号采集连接尚未就绪，请在现有窗口打开或刷新视频号页面后重试",
            }

        started_at = time.time()
        result = open_wechat_video_channels()
        page = wait_for_channels_page(started_at, timeout=open_timeout)
        available = page is not None
        return {
            **(result or {}),
            "proxy_running": manager.running,
            "proxy_started": proxy_started,
            "monitoring_active": available,
            "opened": True,
            "message": (
                "视频号已自动打开，采集接口已就绪"
                if available else
                "已尝试打开视频号，但采集页面尚未就绪，请检查微信中的视频号页面"
            ),
        }
