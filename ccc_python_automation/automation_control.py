#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
automation_control.py (v32)
github目前automation_control.py版本號v32

Summary:
- 互動式殼層（REPL），維持原 UI 風格（titles、boxes、help、progress）。
- 測試三大功能（供 JSON 腳本使用）：
  1) 螢幕辨識（以範本圖在螢幕上定位，支援 ROI 與信心度、多次重試）
  2) 滑鼠控制（移動、點擊、拖曳、滾動）
  3) 鍵盤控制（輸入文字、按鍵、組合鍵）
- 開啟 URL、重置瀏覽器縮放比例（Zoom Reset）。
- JSON 腳本（run <script.json>），可批次執行步驟。
- 流程控制：
  - until：在時限內重試直到條件（locate）成立
  - repeat：重複執行一組 steps 指定次數
  - if.locate：於短時限內檢查元素是否存在，存在則執行 then，不存在則（可選）執行 else
  - procs/call：在腳本根定義可重複使用的步驟區塊，於 steps 以 call 呼叫（可用 with 暫時覆蓋設定）
- v26 新增（終止腳本 JSON 功能）：
  - end：結束目前腳本（返回 REPL），可附帶訊息
    - 用法：{ "end": true }、{ "end": "message" }、{ "end": { "message": "..." } }
  - program.exit：結束整個程式（sys.exit），可附帶訊息與代碼
    - 用法：{ "program.exit": 0 }、{ "program.exit": { "code": 0, "message": "..." } }
    - alias：{ "exit": ... } 亦可
- v28 擴充（Debug 高亮框）：
  - 在 locate/until 步驟支援 "highlight" 參數（true 或物件），定位成功後以疊加矩形框標示目標。
  - Windows 使用 Win32 GDI 疊加框（不遮蔽視窗）；其他平台列印座標替代。
- v29 擴充（defaults 可套用 highlight）：
  - defaults 與 set 現在支援 "highlight" 參數。
  - 當步驟未指定 highlight 時，會自動使用 cfg.highlight（即 defaults 或 set 套用的值）。
- v32 改良：
  - 重構 try_levels 產生邏輯為可參數化的 _build_try_levels()，維持原「先嚴後鬆」行為但更易維護。

相依建議：
- pyautogui（必要）
- opencv-python（建議，才能使用 confidence 參數做影像比對）
- 可選：keyboard（提供 ESC failsafe）
"""

__version__ = "v32"  # github目前automation_control.py版本號v32

import argparse
import json
import platform
import re
import sys
import textwrap
import time
import webbrowser
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# -------------------------------
# Third-party availability checks
# -------------------------------

PYA_AVAILABLE = True
OPENCV_AVAILABLE = True
KEYBOARD_AVAILABLE = True

try:
    import pyautogui as pag  # type: ignore
except Exception:
    PYA_AVAILABLE = False

try:
    import cv2  # noqa: F401
except Exception:
    OPENCV_AVAILABLE = False

try:
    import keyboard as kb  # type: ignore
except Exception:
    KEYBOARD_AVAILABLE = False


# -------------------------------
# Console UI helpers (box style)
# -------------------------------

def _line(width: int = 70, char: str = "─") -> str:
    # github目前automation_control.py版本號v32
    return char * width

def _title(text: str, width: int = 84) -> str:
    # github目前automation_control.py版本號v32
    text = f" {text.strip()} "
    pad = max(0, width - len(text))
    left = pad // 2
    right = pad - left
    return f"{'─'*left}{text}{'─'*right}"

def _box(lines: List[str], width: int = 84) -> str:
    # github目前automation_control.py版本號v32
    top = f"┌{_line(width,'─')}┐"
    bottom = f"└{_line(width,'─')}┘"
    body = []
    for ln in lines:
        ln = ln.rstrip("\n")
        wrapped = textwrap.wrap(ln, width=width, break_long_words=True, break_on_hyphens=False) or [""]
        for w in wrapped:
            body.append(f"│{w:<{width}}│")
    return "\n".join([top, *body, bottom])

def _kv_table(pairs: List[Tuple[str, str]], key_w: int = 22, width: int = 84) -> str:
    # github目前automation_control.py版本號v32
    rows = []
    for k, v in pairs:
        rows.append(f"{k:>{key_w}} : {v}")
    return _box(rows, width=width)

def _print_banner():
    # github目前automation_control.py版本號v32
    print(_title(f"automation_control Interactive Shell ({__version__})", width=84))
    print(_box([
        "Usage:",
        "  run <script.json>                     Execute steps from a JSON script file",
        "  show                                  Show minimal current settings",
        "  quit / exit                           Exit",
    ], width=84))
    print()

def _print_show(cfg: Dict[str, Any]) -> None:
    # github目前automation_control.py版本號v32
    print(_title("Current Configuration", width=84))
    print(_kv_table([
        ("log_file", cfg.get("log_file","(none)")),
        ("pyautogui_ready", cfg.get("pyautogui_ready","No")),
        ("opencv_ready", cfg.get("opencv_ready","No")),
    ], width=84))
    print()


# -------------------------------
# Progress helpers
# -------------------------------

def _progress(pct: int, text: str) -> None:
    # github目前automation_control.py版本號v32
    pct = max(0, min(100, int(pct)))
    sys.stdout.write(f"\r[ {pct:>3}% ] {text.ljust(60)}")
    sys.stdout.flush()

def _progress_done():
    # github目前automation_control.py版本號v32
    sys.stdout.write("\n")
    sys.stdout.flush()


# -------------------------------
# Logging helpers
# -------------------------------

def _write_log(cfg: Dict[str, Any], msg: str) -> None:
    # github目前automation_control.py版本號v32
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_file = cfg.get("log_file", "")
    if isinstance(log_file, str) and log_file.strip():
        try:
            Path(log_file).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            with open(Path(log_file).expanduser().resolve(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # 忽略檔案寫入錯誤，以免干擾執行
            pass


# -------------------------------
# Config helpers
# -------------------------------

def default_config() -> Dict[str, Any]:
    # github目前automation_control.py版本號v32
    return {
        "template": "",
        "roi": "",  # "x1,y1,x2,y2"
        "confidence": 0.85,
        "find_timeout": 20.0,
        "retry_interval": 0.5,
        "type_interval": 0.03,
        "log_file": "",
        "platform": platform.system(),
        "opencv_ready": "Yes" if OPENCV_AVAILABLE else "No",
        "pyautogui_ready": "Yes" if PYA_AVAILABLE else "No",
        "highlight": None  # 新增：可在 defaults/set 指定，locate/until 未指定時套用
    }

def load_config(cfg_path: Path) -> Dict[str, Any]:
    # github目前automation_control.py版本號v32
    if not cfg_path.exists():
        return default_config()
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        base = default_config()
        base.update(data or {})
        return base
    except Exception:
        return default_config()

def save_config(cfg_path: Path, data: Dict[str, Any]) -> None:
    # github目前automation_control.py版本號v32
    try:
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] Config written to: {cfg_path}")
    except Exception as e:
        print(f"Warning: cannot save config {cfg_path}: {e}", file=sys.stderr)


# -------------------------------
# Utility parsers (v32)
# -------------------------------

def _parse_float(s: Union[str, float, int], default: float = 0.0) -> float:
    # github目前automation_control.py版本號v32
    try:
        return float(s)
    except Exception:
        return default

def _parse_int(s: Union[str, float, int], default: int = 0) -> int:
    # github目前automation_control.py版本號v32
    try:
        return int(float(s))
    except Exception:
        return default


# -------------------------------
# ROI parsers and converters (v12/v32: xyxy)
# -------------------------------

def _parse_roi_token(token: str) -> Optional[Tuple[int, int, int, int]]:
    # github目前automation_control.py版本號v32
    # Accept "roi=x1,y1,x2,y2" or "x1,y1,x2,y2"
    if "=" in token:
        k, v = token.split("=", 1)
        if k.strip().lower() != "roi":
            return None
    else:
        v = token
    m = re.match(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*$", v)
    if not m:
        return None
    x1, y1, x2, y2 = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return (x1, y1, x2, y2)

def _roi_str_to_tuple(s: str) -> Optional[Tuple[int, int, int, int]]:
    # github目前automation_control.py版本號v32
    if not s:
        return None
    return _parse_roi_token(s)  # same format "x1,y1,x2,y2"

def _roi_from_json(val: Any) -> Optional[Tuple[int, int, int, int]]:
    # github目前automation_control.py版本號v32
    if val is None:
        return None
    if isinstance(val, str):
        return _roi_str_to_tuple(val)
    if isinstance(val, (list, tuple)) and len(val) == 4:
        try:
            return (int(val[0]), int(val[1]), int(val[2]), int(val[3]))
        except Exception:
            return None
    if isinstance(val, dict):
        try:
            return (int(val["x1"]), int(val["y1"]), int(val["x2"]), int(val["y2"]))
        except Exception:
            return None
    return None

def _roi_xyxy_to_xywh(roi: Tuple[int, int, int, int]) -> Optional[Tuple[int, int, int, int]]:
    # github目前automation_control.py版本號v32
    # 僅在呼叫 pyautogui 的 region 參數時使用
    if not roi:
        return None
    x1, y1, x2, y2 = roi
    w = max(0, x2 - x1)
    h = max(0, y2 - y1)
    if w <= 0 or h <= 0:
        return None
    return (x1, y1, w, h)

def _roi_tuple_to_str(roi: Tuple[int, int, int, int]) -> str:
    # github目前automation_control.py版本號v32
    x1, y1, x2, y2 = roi
    return f"{x1},{y1},{x2},{y2}"

def _to_bool(x: Any) -> bool:
    # github目前automation_control.py版本號v32
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


# -------------------------------
# Custom ESC failsafe (v32)
# -------------------------------

FAILSAFE_TRIGGERED = False
FAILSAFE_THREAD: Optional[threading.Thread] = None
FAILSAFE_READY = False

def _trigger_failsafe():
    # github目前automation_control.py版本號v32
    global FAILSAFE_TRIGGERED
    FAILSAFE_TRIGGERED = True

def _failsafe_active() -> bool:
    # github目前automation_control.py版本號v32
    return bool(FAILSAFE_TRIGGERED)

def _start_keyboard_failsafe():
    # github目前automation_control.py版本號v32
    global FAILSAFE_READY
    try:
        kb.add_hotkey('esc', _trigger_failsafe, suppress=False, trigger_on_release=False)
        FAILSAFE_READY = True
    except Exception:
        FAILSAFE_READY = False

def install_esc_failsafe_once():
    # github目前automation_control.py版本號v32
    global FAILSAFE_READY
    if FAILSAFE_READY or _failsafe_active():
        return
    if KEYBOARD_AVAILABLE:
        _start_keyboard_failsafe()


# -------------------------------
# Debug highlight (Windows GDI overlay)
# -------------------------------

def _parse_color_to_colorref(color: Union[str, Tuple[int, int, int]]) -> int:
    # github目前automation_control.py版本號v32
    # Windows COLORREF = 0x00BBGGRR
    r, g, b = 255, 0, 0
    if isinstance(color, str):
        s = color.strip()
        if s.startswith("#") and len(s) == 7:
            r = int(s[1:3], 16); g = int(s[3:5], 16); b = int(s[5:7], 16)
        elif "," in s:
            try:
                parts = [int(x.strip()) for x in s.split(",")]
                if len(parts) >= 3:
                    r, g, b = parts[0], parts[1], parts[2]
            except Exception:
                pass
    elif isinstance(color, (list, tuple)) and len(color) >= 3:
        r, g, b = int(color[0]), int(color[1]), int(color[2])
    r = max(0, min(255, r)); g = max(0, min(255, g)); b = max(0, min(255, b))
    return (b << 16) | (g << 8) | r

def _highlight_rect(rect_xyxy: Tuple[int, int, int, int], duration: float = 0.6, color: Union[str, Tuple[int,int,int]] = "#ff0000", thickness: int = 3) -> None:
    # github目前automation_control.py版本號v32
    # Windows: draw overlay rectangle using GDI; Others: print bbox
    try:
        if "windows" in platform.system().strip().lower():
            import ctypes
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            hdc = user32.GetDC(0)
            PS_SOLID = 0
            NULL_BRUSH = 5
            colorref = _parse_color_to_colorref(color)
            pen = gdi32.CreatePen(PS_SOLID, int(max(1, thickness)), colorref)
            null_brush = gdi32.GetStockObject(NULL_BRUSH)
            old_pen = gdi32.SelectObject(hdc, pen)
            old_brush = gdi32.SelectObject(hdc, null_brush)
            x1, y1, x2, y2 = rect_xyxy
            gdi32.Rectangle(hdc, int(x1), int(y1), int(x2), int(y2))
            gdi32.SelectObject(hdc, old_pen)
            gdi32.SelectObject(hdc, old_brush)
            gdi32.DeleteObject(pen)
            user32.ReleaseDC(0, hdc)
            time.sleep(max(0.0, float(duration)))
        else:
            print(_box([f"[debug] highlight rect={rect_xyxy} color={color} thickness={thickness}"], width=84))
    except Exception as e:
        print(_box([f"[warn] highlight failed: {e}"], width=84))

def _highlight_async(rect_xyxy: Tuple[int, int, int, int], duration: float = 0.6, color: Union[str, Tuple[int,int,int]] = "#ff0000", thickness: int = 3) -> None:
    # github目前automation_control.py版本號v32
    try:
        threading.Thread(target=_highlight_rect, args=(rect_xyxy, duration, color, thickness), daemon=True).start()
    except Exception as e:
        print(_box([f"[warn] highlight spawn failed: {e}"], width=84))

def _extract_highlight_params(params: Any) -> Tuple[bool, float, Union[str, Tuple[int,int,int]], int]:
    # github目前automation_control.py版本號v32
    # returns (enabled, duration, color, thickness)
    enabled = False
    duration = 0.6
    color: Union[str, Tuple[int,int,int]] = "#ff0000"
    thickness = 3
    if params is None:
        return (False, duration, color, thickness)
    if isinstance(params, bool):
        enabled = params
    elif isinstance(params, dict):
        enabled = True
        if "duration" in params:
            try: duration = float(params.get("duration", duration))
            except Exception: pass
        if "color" in params:
            color = params.get("color", color)
        if "thickness" in params:
            try: thickness = int(params.get("thickness", thickness))
            except Exception: pass
    else:
        enabled = True
    return (enabled, duration, color, thickness)


# -------------------------------
# Screen, Mouse, Keyboard actions
# -------------------------------

def ensure_pyautogui_ready():
    # github目前automation_control.py版本號v32
    if not PYA_AVAILABLE:
        raise RuntimeError("pyautogui is not available. Please install pyautogui.")

    try:
        pag.FAILSAFE = False
    except Exception:
        pass
    pag.PAUSE = 0.05


# -------------------------------
# Try-levels builder (v32)
# -------------------------------

def _build_try_levels(base: float, step: float = 0.04, steps: int = 6, min_floor: float = 0.5, cap: float = 0.99) -> List[float]:
    """
    依據 base 產生一組由高到低的信心度門檻，維持原「先嚴後鬆」策略。
    - base：起始門檻（會先夾在 [min_floor, cap] 範圍內）
    - step：每階下降量
    - steps：總階數（包含 base 本身）
    - min_floor：最低不低於此值
    - cap：最高不高於此值
    github目前automation_control.py版本號v32
    """
    try:
        base = float(base)
    except Exception:
        base = cap
    base = max(min_floor, min(cap, base))
    seq = [round(max(min_floor, base - i * step), 2) for i in range(max(1, int(steps)))]
    # 去重並由高到低排序
    return sorted(set(seq), reverse=True)


# -------------------------------
# Screen, Mouse, Keyboard actions (locate + inputs)
# -------------------------------

def locate_template(
    template_path: Path,
    confidence: float,
    timeout: float,
    retry_interval: float,
    roi: Optional[Tuple[int, int, int, int]] = None,  # github目前automation_control.py版本號v32
    try_levels: Optional[List[float]] = None,
) -> Optional[Tuple[int, int, Tuple[int, int, int, int]]]:
    """
    在螢幕上定位範本，回傳 (cx, cy, rect_xyxy) 或 None
    rect_xyxy = (x1, y1, x2, y2) 為左上到右下座標。
    - 若 OPENCV 不可用，將忽略 confidence 參數。
    github目前automation_control.py版本號v32
    """
    ensure_pyautogui_ready()
    install_esc_failsafe_once()

    if not template_path.exists():
        print(f"[ERROR] Template not found: {template_path}")
        return None

    if try_levels is None:
        # v32: 使用參數化的建置器，維持原行為（base 至多降 0.20、每步 0.04、下限 0.50、上限 0.99）
        try_levels = _build_try_levels(confidence, step=0.04, steps=6, min_floor=0.5, cap=0.99)

    start = time.time()
    step_count = max(1, int(timeout / max(retry_interval, 0.1)))
    for _ in range(step_count + 1):
        if _failsafe_active():
            raise RuntimeError("Failsafe triggered (ESC)")
        elapsed = time.time() - start
        pct = int(min(100, (elapsed / max(timeout, 0.001)) * 100))
        _progress(pct, f"Locating {template_path.name} ...")

        for lv in try_levels:
            if _failsafe_active():
                _progress_done()
                raise RuntimeError("Failsafe triggered (ESC)")
            try:
                kwargs: Dict[str, Any] = {}
                if roi:
                    region = _roi_xyxy_to_xywh(roi)
                    if region:
                        kwargs["region"] = region
                if OPENCV_AVAILABLE:
                    kwargs["confidence"] = float(lv)
                box = pag.locateOnScreen(str(template_path), **kwargs)
                if box:
                    cx, cy = pag.center(box)
                    x1 = int(box.left)
                    y1 = int(box.top)
                    x2 = int(box.left + box.width)
                    y2 = int(box.top + box.height)
                    rect_xyxy = (x1, y1, x2, y2)
                    _progress_done()
                    print(f"[INFO] Found {template_path.name} at ({int(cx)},{int(cy)}) conf~{lv:.2f} rect={rect_xyxy}")
                    return (int(cx), int(cy), rect_xyxy)
            except Exception:
                pass

        if elapsed >= timeout:
            break
        time.sleep(retry_interval)

    _progress_done()
    print(f"[ERROR] Timeout locating: {template_path}")
    return None

def move_mouse(x: int, y: int, duration: float = 0.15) -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    pag.moveTo(int(x), int(y), duration=max(0.0, float(duration)))

def click_mouse(x: Optional[int] = None, y: Optional[int] = None, button: str = "left", clicks: int = 1, interval: float = 0.05) -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    if x is not None and y is not None:
        pag.click(int(x), int(y), clicks=max(1, int(clicks)), interval=max(0.0, float(interval)), button=button)
    else:
        pag.click(clicks=max(1, int(clicks)), interval=max(0.0, float(interval)), button=button)

def drag_mouse(x1: int, y1: int, x2: int, y2: int, duration: float = 0.3, button: str = "left") -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    pag.moveTo(int(x1), int(y1), duration=0.05)
    pag.dragTo(int(x2), int(y2), duration=max(0.0, float(duration)), button=button)

def scroll_mouse(amount: int) -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    pag.scroll(int(amount))

def type_text(text: str, type_interval: float = 0.03) -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    pag.typewrite(text, interval=max(0.0, float(type_interval)))

def press_key(key: str) -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    pag.press(key)

def hotkey_chord(keys: List[str]) -> None:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    keys = [k.strip() for k in keys if k.strip()]
    if not keys:
        return
    pag.hotkey(*keys)

def current_pos() -> Tuple[int, int]:
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    x, y = pag.position()
    return int(x), int(y)

def zoom_reset_hotkey():
    # github目前automation_control.py版本號v32
    ensure_pyautogui_ready()
    install_esc_failsafe_once()
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    sys_plat = platform.system().lower()
    combo = ["ctrl", "0"]
    if "darwin" in sys_plat or "mac" in sys_plat:
        combo = ["command", "0"]
    time.sleep(0.2)
    pag.hotkey(*combo)
    time.sleep(0.15)
    pag.hotkey(*combo)


# -------------------------------
# JSON script runner (with control flow & v11/v12/v32 features)
# -------------------------------

def _apply_set_cfg(cfg: Dict[str, Any], updates: Dict[str, Any]) -> None:
    # github目前automation_control.py版本號v32
    for k, v in (updates or {}).items():
        kl = str(k).lower()
        if kl == "template":
            cfg["template"] = str(v)
        elif kl == "roi":
            roi = _roi_from_json(v)
            if roi:
                cfg["roi"] = _roi_tuple_to_str(roi)
            else:
                cfg["roi"] = ""
        elif kl == "confidence":
            cfg["confidence"] = _parse_float(v, cfg.get("confidence", 0.85))
        elif kl == "find_timeout":
            cfg["find_timeout"] = _parse_float(v, cfg.get("find_timeout", 20.0))
        elif kl == "retry_interval":
            cfg["retry_interval"] = _parse_float(v, cfg.get("retry_interval", 0.5))
        elif kl == "type_interval":
            cfg["type_interval"] = _parse_float(v, cfg.get("type_interval", 0.03))
        elif kl == "log_file":
            cfg["log_file"] = str(v)
        elif kl == "highlight":  # 新增：允許 defaults/set 設定 highlight
            cfg["highlight"] = v

def _resolve_step_value(d: Dict[str, Any]) -> Tuple[str, Any]:
    # github目前automation_control.py版本號v32
    if not isinstance(d, dict) or not d:
        return "", None
    k = list(d.keys())[0]
    return k, d[k]

def _parse_hotkey(val: Any) -> List[str]:
    # github目前automation_control.py版本號v32
    if isinstance(val, list):
        return [str(x) for x in val if str(x)]
    if isinstance(val, str):
        return [x.strip() for x in val.split("+") if x.strip()]
    return []

def _click_with_relative(
    cx: int,
    cy: int,
    rect_xyxy: Optional[Tuple[int, int, int, int]],
    offset: Optional[Tuple[int, int]],
    percent: Optional[Tuple[float, float]],
    button: str = "left",
    clicks: int = 1
) -> None:
    # github目前automation_control.py版本號v32
    x, y = int(cx), int(cy)
    if percent and rect_xyxy:
        x1, y1, x2, y2 = rect_xyxy
        rw = max(0, x2 - x1)
        rh = max(0, y2 - y1)
        px = float(percent[0]); py = float(percent[1])
        px = max(0.0, min(1.0, px))
        py = max(0.0, min(1.0, py))
        x = x1 + int(rw * px)
        y = y1 + int(rh * py)
    if offset:
        dx, dy = int(offset[0]), int(offset[1])
        x += dx; y += dy
    click_mouse(x, y, button=button, clicks=max(1, int(clicks)))

def _step_locate(value: Any, cfg: Dict[str, Any]) -> None:
    # github目前automation_control.py版本號v32
    if _failsafe_active():
        raise RuntimeError("Failsafe triggered (ESC)")
    params: Dict[str, Any] = {}
    if isinstance(value, str):
        params["file"] = value
    elif isinstance(value, dict):
        params = dict(value)
    else:
        raise RuntimeError("locate step expects string or object")

    file_path = params.get("file") or cfg.get("template", "")
    if not file_path:
        raise RuntimeError("locate step requires 'file' or default template")

    roi = _roi_from_json(params.get("roi")) or _roi_str_to_tuple(cfg.get("roi",""))
    conf = _parse_float(params.get("confidence", cfg.get("confidence", 0.85)), cfg.get("confidence", 0.85))
    timeout = _parse_float(params.get("timeout", cfg.get("find_timeout", 20.0)), cfg.get("find_timeout", 20.0))
    retry = _parse_float(params.get("retry_interval", cfg.get("retry_interval", 0.5)), cfg.get("retry_interval", 0.5))

    res = locate_template(Path(file_path).expanduser().resolve(), conf, timeout, retry, roi=roi)
    if not res:
        if _to_bool(params.get("required", True)):
            raise RuntimeError(f"locate failed: {file_path}")
        else:
            print(_box([f"[skip] locate not found (optional): {file_path}"], width=84))
            return

    cx, cy, rect_xyxy = res

    # debug highlight: 使用步驟指定的 highlight，缺省則用 cfg.highlight
    hl_param = params.get("highlight", cfg.get("highlight", None))
    enabled, duration, color, thickness = _extract_highlight_params(hl_param)
    if enabled and rect_xyxy:
        _highlight_async(rect_xyxy, duration=duration, color=color, thickness=thickness)

    if _to_bool(params.get("click", False)):
        clicks = int(params.get("clicks", 1))
        button = str(params.get("button", "left")).lower()
        offset = None
        percent = None
        if "offset" in params:
            ov = params.get("offset")
            if isinstance(ov, (list, tuple)) and len(ov) == 2:
                offset = (int(ov[0]), int(ov[1]))
        if "percent" in params:
            pv = params.get("percent")
            if isinstance(pv, (list, tuple)) and len(pv) == 2:
                percent = (float(pv[0]), float(pv[1]))
        _click_with_relative(cx, cy, rect_xyxy, offset, percent, button=button, clicks=clicks)
        print(_box([f"Clicked at ({cx},{cy}) rect={rect_xyxy} offset={offset} percent={percent}"], width=84))

def _exec_repeat(block: Dict[str, Any], run_step) -> None:
    # github目前automation_control.py版本號v32
    times = int(block.get("times", 1))
    steps = block.get("steps", [])
    if not isinstance(steps, list):
        raise RuntimeError("repeat requires 'steps' as list")
    for _ in range(max(0, times)):
        for st in steps:
            if _failsafe_active():
                raise RuntimeError("Failsafe triggered (ESC)")
            nm, val = _resolve_step_value(st)
            run_step(nm, val)

def _exec_until(block: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    # github目前automation_control.py版本號v32
    # 只支援 locate
    timeout = _parse_float(block.get("timeout", cfg.get("find_timeout", 20.0)), cfg.get("find_timeout", 20.0))
    retry = _parse_float(block.get("retry_interval", cfg.get("retry_interval", 0.5)), cfg.get("retry_interval", 0.5))
    start = time.time()

    cond_loc = block.get("locate")
    if not cond_loc or not isinstance(cond_loc, dict):
        raise RuntimeError("until requires a 'locate' object")

    # 解析 locate 條件（與 _step_locate 相同的參數集合，但不點擊）
    file_path = cond_loc.get("file") or cfg.get("template", "")
    if not file_path:
        raise RuntimeError("until.locate requires 'file' or default template")

    roi = _roi_from_json(cond_loc.get("roi")) or _roi_str_to_tuple(cfg.get("roi", ""))
    conf = _parse_float(cond_loc.get("confidence", cfg.get("confidence", 0.85)), cfg.get("confidence", 0.85))

    # optional highlight: 使用 block 指定的 highlight，缺省則用 cfg.highlight
    hl_param = block.get("highlight", cfg.get("highlight", None))
    hl_enabled, hl_duration, hl_color, hl_thickness = _extract_highlight_params(hl_param)

    while True:
        if _failsafe_active():
            raise RuntimeError("Failsafe triggered (ESC)")
        if time.time() - start > timeout:
            raise RuntimeError("until timeout (locate)")
        res = locate_template(Path(file_path).expanduser().resolve(), conf, retry * 2, retry, roi=roi)
        if res:
            cx, cy, rect_xyxy = res
            if hl_enabled and rect_xyxy:
                _highlight_async(rect_xyxy, duration=hl_duration, color=hl_color, thickness=hl_thickness)
            return
        time.sleep(max(0.05, retry))

def _probe_locate(cond_loc: Dict[str, Any], cfg: Dict[str, Any], fallback_timeout: float = 1.5, fallback_retry: float = 0.4) -> bool:
    # github目前automation_control.py版本號v32
    # 在短時間內嘗試定位一次，不拋錯，僅回傳是否存在
    file_path = cond_loc.get("file") or cfg.get("template", "")
    if not file_path:
        return False
    roi = _roi_from_json(cond_loc.get("roi")) or _roi_str_to_tuple(cfg.get("roi", ""))
    conf = _parse_float(cond_loc.get("confidence", cfg.get("confidence", 0.85)), cfg.get("confidence", 0.85))
    timeout = _parse_float(cond_loc.get("timeout", fallback_timeout), fallback_timeout)
    retry = _parse_float(cond_loc.get("retry_interval", fallback_retry), fallback_retry)
    res = locate_template(Path(file_path).expanduser().resolve(), conf, timeout, retry, roi=roi)
    return bool(res)

def _exec_if(block: Dict[str, Any], cfg: Dict[str, Any], run_step) -> None:
    # github目前automation_control.py版本號v32
    # 支援格式：
    # { "if": {
    #     "locate": { "file": "...", "timeout": 1.0, "retry_interval": 0.25, "roi": [...] , "confidence": 0.85 },
    #     "then": [ {step...}, ... ],
    #     "else": [ {step...}, ... ]   // optional
    # } }
    if not isinstance(block, dict):
        raise RuntimeError("if step expects object")
    cond_loc = block.get("locate")
    if not isinstance(cond_loc, dict):
        raise RuntimeError("if requires a 'locate' object")
    found = _probe_locate(cond_loc, cfg)
    seq = block.get("then" if found else "else", [])
    if not seq:
        return
    if not isinstance(seq, list):
        raise RuntimeError("if.then/if.else must be a list of steps")
    for st in seq:
        if _failsafe_active():
            raise RuntimeError("Failsafe triggered (ESC)")
        nm, val = _resolve_step_value(st)
        if not nm:
            continue
        run_step(nm, val)


# -------------------------------
# End / Exit steps (v32)
# -------------------------------

class ScriptEnd(Exception):
    # github目前automation_control.py版本號v32
    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

def _exec_end(value: Any) -> None:
    # github目前automation_control.py版本號v32
    msg = ""
    if isinstance(value, str):
        msg = value
    elif isinstance(value, dict):
        msg = str(value.get("message", "") or "")
    elif _to_bool(value):
        msg = ""
    # 印出訊息方框（若有）
    lines = ["Script ended (end)"] if not msg else [msg]
    print(_box(lines, width=84))
    raise ScriptEnd(msg)

def _exec_program_exit(value: Any) -> None:
    # github目前automation_control.py版本號v32
    code = 0
    msg = ""
    if isinstance(value, int):
        code = int(value)
    elif isinstance(value, str):
        # 若給字串就當作訊息
        msg = value
    elif isinstance(value, dict):
        code = int(value.get("code", 0) or 0)
        if "message" in value:
            msg = str(value.get("message") or "")
    if msg:
        print(_box([msg], width=84))
    sys.exit(code)


# -------------------------------
# Call / Procs (v32)
# -------------------------------

def _exec_call(block: Any, procs: Dict[str, List[Any]], cfg: Dict[str, Any], run_step) -> None:
    # github目前automation_control.py版本號v32
    # 支援格式：
    # { "call": "procName" }
    # { "call": { "name": "procName", "with": { ...臨時覆蓋設定... } } }
    if isinstance(block, str):
        name = block
        with_cfg = None
    elif isinstance(block, dict):
        name = str(block.get("name", "")).strip()
        with_cfg = block.get("with", None)
    else:
        raise RuntimeError("call step expects string or object")

    if not name:
        raise RuntimeError("call requires a procedure name")

    steps = procs.get(name)
    if not isinstance(steps, list):
        raise RuntimeError(f"proc not found: {name}")

    # 儲存/覆蓋 cfg（區域性）
    original = dict(cfg)
    try:
        if isinstance(with_cfg, dict):
            _apply_set_cfg(cfg, with_cfg)
        for st in steps:
            if _failsafe_active():
                raise RuntimeError("Failsafe triggered (ESC)")
            nm, val = _resolve_step_value(st)
            if not nm:
                continue
            run_step(nm, val)
    finally:
        # 還原 cfg
        for k in list(cfg.keys()):
            if k not in original:
                continue
            cfg[k] = original[k]

def run_json_script(script_path: Path, cfg: Dict[str, Any]) -> None:
    # github目前automation_control.py版本號v32
    install_esc_failsafe_once()
    if not script_path.exists():
        raise RuntimeError(f"Script not found: {script_path}")
    try:
        data = json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Invalid JSON: {e}")

    if not isinstance(data, dict):
        raise RuntimeError("Script root must be an object")

    defaults = data.get("defaults", {})
    if isinstance(defaults, dict):
        _apply_set_cfg(cfg, defaults)

    procs: Dict[str, List[Any]] = {}
    raw_procs = data.get("procs", {})
    if isinstance(raw_procs, dict):
        for k, v in raw_procs.items():
            if isinstance(v, list):
                procs[str(k)] = v

    steps = data.get("steps", [])
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("Script requires a non-empty 'steps' array")

    print(_title(f"Run Script: {script_path.name}", width=84))
    print(_kv_table([
        ("steps", str(len(steps))),
        ("procs", str(len(procs))),
        ("confidence", str(cfg.get("confidence"))),
        ("roi", cfg.get("roi","(none)")),
        ("timeout", str(cfg.get("find_timeout"))),
        ("retry_interval", str(cfg.get("retry_interval"))),
        ("log_file", cfg.get("log_file","(none)")),
    ], width=84))

    total = len(steps)
    start = time.time()

    def run_step(name: str, value: Any) -> None:
        # github目前automation_control.py版本號v32
        if _failsafe_active():
            raise RuntimeError("Failsafe triggered (ESC)")
        name_l = str(name).strip().lower()
        if not name_l:
            return
        t0 = time.time()
        _write_log(cfg, f"START step '{name_l}'")
        try:
            if name_l in ("open_url", "open-url"):
                url = str(value)
                webbrowser.open(url)
            elif name_l in ("zoom_reset", "zoom-reset"):
                zoom_reset_hotkey()
            elif name_l == "delay":
                time.sleep(_parse_float(value, 0.0))
            elif name_l == "set":
                if not isinstance(value, dict):
                    raise RuntimeError("set step must be an object")
                _apply_set_cfg(cfg, value)
            elif name_l == "locate":
                _step_locate(value, cfg)
            elif name_l == "key.type":
                type_text(str(value), type_interval=_parse_float(cfg.get("type_interval", 0.03), 0.03))
            elif name_l == "key.press":
                press_key(str(value))
            elif name_l == "key.hotkey":
                hotkey_chord(_parse_hotkey(value))
            elif name_l == "mouse.move":
                if not isinstance(value, dict):
                    raise RuntimeError("mouse.move expects object")
                move_mouse(
                    _parse_int(value.get("x",0), 0),
                    _parse_int(value.get("y",0), 0),
                    _parse_float(value.get("duration",0.15), 0.15)
                )
            elif name_l == "mouse.click":
                if not isinstance(value, dict):
                    raise RuntimeError("mouse.click expects object")
                x = value.get("x", None)
                y = value.get("y", None)
                bx = str(value.get("button", "left"))
                clicks = int(value.get("clicks", 1))
                if x is None or y is None:
                    click_mouse(None, None, button=bx, clicks=max(1, clicks))
                else:
                    click_mouse(_parse_int(x, 0), _parse_int(y, 0), button=bx, clicks=max(1, clicks))
            elif name_l == "mouse.drag":
                if not isinstance(value, dict):
                    raise RuntimeError("mouse.drag expects object")
                drag_mouse(
                    _parse_int(value.get("x1",0), 0), _parse_int(value.get("y1",0), 0),
                    _parse_int(value.get("x2",0), 0), _parse_int(value.get("y2",0), 0),
                    _parse_float(value.get("duration",0.3), 0.3),
                    str(value.get("button","left"))
                )
            elif name_l == "mouse.scroll":
                amt = _parse_int(value, 0) if not isinstance(value, dict) else _parse_int(value.get("amount",0), 0)
                scroll_mouse(amt)
            elif name_l == "repeat":
                if not isinstance(value, dict):
                    raise RuntimeError("repeat expects object")
                _exec_repeat(value, lambda n,v: run_step(n,v))
            elif name_l == "until":
                if not isinstance(value, dict):
                    raise RuntimeError("until expects object")
                _exec_until(value, cfg)
            elif name_l == "if":
                if not isinstance(value, dict):
                    raise RuntimeError("if expects object")
                _exec_if(value, cfg, lambda n,v: run_step(n,v))
            elif name_l == "call":
                _exec_call(value, procs, cfg, lambda n,v: run_step(n,v))
            elif name_l == "end":
                _exec_end(value)
            elif name_l in ("program.exit", "exit"):
                _exec_program_exit(value)
            else:
                raise RuntimeError(f"Unknown step: {name}")
        except ScriptEnd:
            # 直接往外拋，讓腳本優雅結束
            raise
        except SystemExit:
            # 讓程式結束
            raise
        except Exception as e:
            _write_log(cfg, f"ERROR step '{name_l}' -> {e}")
            raise
        finally:
            _write_log(cfg, f"END step '{name_l}' in {time.time()-t0:.3f}s")

    try:
        for idx, raw_step in enumerate(steps, start=1):
            if _failsafe_active():
                raise RuntimeError("Failsafe triggered (ESC)")
            _progress(int((idx-1)*100/total), f"Step {idx}/{total}")
            name, value = _resolve_step_value(raw_step)
            run_step(name, value)
        _progress(100, "Done")
        _progress_done()
        print(_box([f"Script finished in {time.time()-start:.2f}s"], width=84))
    except ScriptEnd as se:
        _progress_done()
        # 已在 _exec_end 印過訊息；這裡補充收尾資訊
        msg = se.message or "Script ended by 'end'."
        print(_box([msg, f"Stopped at {time.time()-start:.2f}s"], width=84))
        return
    except Exception:
        _progress_done()
        raise


# -------------------------------
# Interactive shell (REPL)
# -------------------------------

def interactive_shell(cfg: Dict[str, Any], cfg_path: Path) -> None:
    # github目前automation_control.py版本號v32
    install_esc_failsafe_once()

    _print_banner()
    _print_show(cfg)  # 啟動時顯示目前最小設定

    while True:
        try:
            if _failsafe_active():
                print(_box(["Failsafe triggered (ESC). Exiting."], width=84))
                sys.exit(1)
            line = input("automation> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExit.")
            sys.exit(0)

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("quit", "exit"):
            print("Exit.")
            sys.exit(0)

        if cmd == "show":
            _print_show(cfg)
            continue

        if cmd == "run":
            if len(parts) < 2:
                print(_box(["Usage: run <script.json>"], width=84))
                continue
            script_path = Path(parts[1]).expanduser().resolve()
            try:
                run_json_script(script_path, cfg)
            except SystemExit as e:
                # program.exit 觸發，將程式結束碼往外傳
                raise
            except Exception as e:
                print(_box([f"Failed: {e}"], width=84))
            continue

        print(_box(["Unknown command. Use: run <script.json> or show"], width=84))


# -------------------------------
# Main
# -------------------------------

def main():
    """
    程式進入點
    github目前automation_control.py版本號v32
    """
    parser = argparse.ArgumentParser(add_help=False)
    _ = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    cfg_path = script_dir / "automation_control_config.json"
    cfg = load_config(cfg_path)

    # Initialize pyautogui base settings safely
    if PYA_AVAILABLE:
        try:
            # v32: disable pyautogui corner failsafe, use custom ESC failsafe
            pag.FAILSAFE = False
            pag.PAUSE = 0.05
        except Exception:
            pass

    install_esc_failsafe_once()
    interactive_shell(cfg, cfg_path)


if __name__ == "__main__":
    main()
