#!/usr/bin/env python3
"""
SpaceMouse for Miro - pan and zoom Miro boards with a 3Dconnexion SpaceMouse.
Windows 10/11, Python 3.8+, no extra packages.

Runs in the system tray. Right-click the tray icon for pause, precision mode,
settings, start-with-Windows and quit. Double-click the icon to open settings.

Run:        pythonw SpaceMouseMiro.pyw        (or just double-click the file)
Diagnose:   python SpaceMouseMiro.pyw --list   list detected 3Dconnexion devices
            python SpaceMouseMiro.pyw --debug  show raw puck/button values

Keep Miro's navigation mode on "Mouse" (in both the browser and the desktop app).
Settings are stored in %APPDATA%\\SpaceMouseMiro\\settings.json, log in log.txt there.
"""

import argparse
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
from ctypes import wintypes

if os.name != "nt":
    sys.exit("SpaceMouse for Miro runs on Windows only.")

import winreg  # noqa: E402  (Windows-only stdlib module)

APP_NAME = "SpaceMouse for Miro"
APP_ID = "SpaceMouseMiro"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_ID)
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "log.txt")

log = logging.getLogger(APP_ID)

# ============================== settings ==============================

DEFAULTS = {
    "nav_method": "touch",          # "touch" (recommended) or "wheel" (trackpad-style fallback)
    "pan_speed": 3200,              # pixels/second at full push
    "zoom_speed": 2.6,              # zoom rate at full push
    "curve": 1.5,                   # response curve; higher = finer control of small pushes
    "smoothing": 0.035,             # seconds; higher = silkier but laggier
    "precision_factor": 0.3,        # speed multiplier in precision mode
    "invert_x": False,
    "invert_y": False,
    "invert_zoom": False,
    "pan_source": "slide",          # "slide" the puck or "tilt" it
    "zoom_source": "push",          # "push"/pull or "twist"
    "pan_deadzone": 20,
    "zoom_deadzone": 50,
    "crosstalk": 0.15,
    "require_pointer_over_window": True,
    "target_processes": "miro.exe",
    "title_keywords": "miro",
    "buttons": {"1": "ALT+1", "12": "ALT+2", "13": "CTRL+0",
                "14": "CTRL+Z", "15": "CTRL+SHIFT+Z"},
}

# Fixed tuning (not exposed in the UI)
FULL_SCALE = 350            # approx. max raw deflection
TICK_HZ = 120
WHEEL_EVENT_HZ = 60
STALE_AFTER = 1.0           # s without a device report -> treat puck as released
EDGE_MARGIN = 60            # px kept clear of the window edge by the virtual fingers
TOUCH_SPREAD = 220          # px between virtual fingers when they touch down
TOUCH_MIN_SPREAD = 60
TOUCH_MAX_FRACTION = 0.7
TOUCH_RELEASE_DELAY = 0.12
WHEEL_UNITS_PER_PX = 0.75   # wheel-mode pan conversion
WHEEL_ZOOM_UNITS = 270      # wheel-mode zoom conversion


class Settings:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULTS))
        self.load()

    def __getitem__(self, key):
        return self.data[key]

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("Couldn't read settings (%s) - using defaults", e)
            return
        for key, default in DEFAULTS.items():
            if key not in saved:
                continue
            val = saved[key]
            if isinstance(default, bool):
                ok = isinstance(val, bool)
            elif isinstance(default, (int, float)):
                ok = isinstance(val, (int, float)) and not isinstance(val, bool)
            elif isinstance(default, dict):
                ok = isinstance(val, dict) and all(isinstance(v, str) for v in val.values())
            else:
                ok = isinstance(val, type(default))
            if ok:
                self.data[key] = val

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


# ============================== Win32 plumbing ==============================

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
VK_CONTROL = 0x11
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

KEYS = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "WIN": 0x5B, "ESC": 0x1B, "SPACE": 0x20,
        "ENTER": 0x0D, "TAB": 0x09, "BACKSPACE": 0x08, "DEL": 0x2E, "INS": 0x2D,
        "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22,
        "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
        "PLUS": 0xBB, "MINUS": 0xBD}
KEYS.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(d): ord(str(d)) for d in range(10)})
KEYS.update({f"F{n}": 0x70 + n - 1 for n in range(1, 13)})
KEY_ALIASES = {"CONTROL": "CTRL", "ESCAPE": "ESC", "DELETE": "DEL", "RETURN": "ENTER",
               "PAGEUP": "PGUP", "PAGEDOWN": "PGDN", "=": "PLUS", "-": "MINUS"}
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}


def parse_combo(text):
    """'ctrl+shift+z' -> [vk, vk, vk]; raises ValueError on unknown keys."""
    vks = []
    for tok in text.replace(" ", "").upper().split("+"):
        if not tok:
            continue
        tok = KEY_ALIASES.get(tok, tok)
        if tok not in KEYS:
            raise ValueError(f"Unknown key '{tok}'")
        vks.append(KEYS[tok])
    if not vks:
        raise ValueError("No keys given")
    return vks


def make_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def _mouse(flags, data=0):
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def _key(vk, up=False):
    i = INPUT(type=INPUT_KEYBOARD)
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0)
    i.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return i


def send(events):
    if events:
        arr = (INPUT * len(events))(*events)
        user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def send_combo(vks):
    send([_key(v) for v in vks] + [_key(v, True) for v in reversed(vks)])


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _csv(text):
    return [t.strip().lower() for t in text.split(",") if t.strip()]


def miro_window(S, held=False):
    """Foreground Miro window handle, or None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    title = buf.value.lower()

    exe = ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        size = wintypes.DWORD(260)
        pbuf = ctypes.create_unicode_buffer(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
            exe = os.path.basename(pbuf.value).lower()
        kernel32.CloseHandle(h)

    if exe not in _csv(S["target_processes"]) and not any(k in title for k in _csv(S["title_keywords"])):
        return None
    if S["require_pointer_over_window"] and not held:
        under = user32.WindowFromPoint(wintypes.POINT(*cursor_pos()))
        if not under or user32.GetAncestor(under, GA_ROOT) != hwnd:
            return None
    return hwnd


# ============================== touch injection ==============================

PT_TOUCH = 2
POINTER_FLAG_INRANGE, POINTER_FLAG_INCONTACT = 0x2, 0x4
POINTER_FLAG_DOWN, POINTER_FLAG_UPDATE, POINTER_FLAG_UP = 0x10000, 0x20000, 0x40000
TOUCH_MASK_ALL = 0x1 | 0x2 | 0x4        # contact area, orientation, pressure
TOUCH_FEEDBACK_NONE = 0x3
CONTACT = POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT


class POINTER_INFO(ctypes.Structure):
    _fields_ = [("pointerType", wintypes.DWORD), ("pointerId", ctypes.c_uint32),
                ("frameId", ctypes.c_uint32), ("pointerFlags", ctypes.c_uint32),
                ("sourceDevice", wintypes.HANDLE), ("hwndTarget", wintypes.HWND),
                ("ptPixelLocation", wintypes.POINT), ("ptHimetricLocation", wintypes.POINT),
                ("ptPixelLocationRaw", wintypes.POINT), ("ptHimetricLocationRaw", wintypes.POINT),
                ("dwTime", wintypes.DWORD), ("historyCount", ctypes.c_uint32),
                ("InputData", ctypes.c_int32), ("dwKeyStates", wintypes.DWORD),
                ("PerformanceCount", ctypes.c_uint64), ("ButtonChangeType", ctypes.c_int)]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchFlags", ctypes.c_uint32),
                ("touchMask", ctypes.c_uint32), ("rcContact", wintypes.RECT),
                ("rcContactRaw", wintypes.RECT), ("orientation", ctypes.c_uint32),
                ("pressure", ctypes.c_uint32)]


user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, wintypes.DWORD]
user32.InjectTouchInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
_touch_ready = False


class TouchNav:
    """Two virtual fingers either side of the pointer: moving them pans,
    spreading/pinching them zooms about their midpoint."""

    def __init__(self):
        global _touch_ready
        if not _touch_ready:
            if not user32.InitializeTouchInjection(2, TOUCH_FEEDBACK_NONE):
                raise OSError(f"touch injection unavailable (Windows error {ctypes.get_last_error()})")
            _touch_ready = True
        self.active = False
        self.cx = self.cy = self.spread = 0.0
        self.bounds = self.home = self.saved_cursor = None
        self.warned = False

    def _max_spread(self):
        l, t, r, b = self.bounds
        return TOUCH_MAX_FRACTION * min(r - l, b - t)

    def _inject(self, flags):
        arr = (POINTER_TOUCH_INFO * 2)()
        half = self.spread / 2
        for i, x in enumerate((self.cx - half, self.cx + half)):
            px, py = int(round(x)), int(round(self.cy))
            c = arr[i]
            c.pointerInfo.pointerType = PT_TOUCH
            c.pointerInfo.pointerId = i
            c.pointerInfo.pointerFlags = flags
            c.pointerInfo.ptPixelLocation = wintypes.POINT(px, py)
            c.touchMask = TOUCH_MASK_ALL
            c.rcContact = wintypes.RECT(px - 2, py - 2, px + 2, py + 2)
            c.orientation = 90
            c.pressure = 32000
        if not user32.InjectTouchInput(2, arr) and not self.warned:
            self.warned = True
            log.warning("Touch injection failed (Windows error %s)", ctypes.get_last_error())

    def _valid(self):
        l, t, r, b = self.bounds
        half = self.spread / 2
        return (TOUCH_MIN_SPREAD <= self.spread <= self._max_spread() and
                l <= self.cx - half and self.cx + half <= r and t <= self.cy <= b)

    def start(self, hwnd, focal):
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        self.bounds = (rect.left + EDGE_MARGIN, rect.top + EDGE_MARGIN,
                       rect.right - EDGE_MARGIN, rect.bottom - EDGE_MARGIN)
        l, t, r, b = self.bounds
        self.spread = min(TOUCH_SPREAD, self._max_spread())
        half = self.spread / 2
        self.cx = min(max(focal[0], l + half), r - half)
        self.cy = min(max(focal[1], t), b)
        self.home = (self.cx, self.cy)
        self.saved_cursor = cursor_pos()
        self.active = True
        self._inject(POINTER_FLAG_DOWN | CONTACT)

    def step(self, view_dx, view_dy, zoom_log):
        prev = (self.cx, self.cy, self.spread)
        self.cx -= view_dx          # slide the board the opposite way to the view
        self.cy -= view_dy
        self.spread *= 2.718281828 ** zoom_log
        if not self._valid():       # hit the window edge or pinch limit: lift and re-place
            self.cx, self.cy, self.spread = prev
            self._inject(POINTER_FLAG_UP)
            self.cx, self.cy = self.home
            self.spread = min(TOUCH_SPREAD, self._max_spread())
            self._inject(POINTER_FLAG_DOWN | CONTACT)
            return
        self._inject(POINTER_FLAG_UPDATE | CONTACT)

    def stop(self):
        if not self.active:
            return
        self._inject(POINTER_FLAG_UP)
        self.active = False
        if self.saved_cursor and cursor_pos() != self.saved_cursor:
            user32.SetCursorPos(*self.saved_cursor)


# ============================== HID (SpaceMouse) ==============================

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hiddll = ctypes.WinDLL("hid", use_last_error=True)

VENDORS = {0x046D, 0x256F}
KNOWN_046D = {0xC603, 0xC605, 0xC606, 0xC621, 0xC623, 0xC625, 0xC626,
              0xC627, 0xC628, 0xC629, 0xC62B}
AXES = ("x", "y", "z", "rx", "ry", "rz")
GENERIC_READ = 0x80000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
HIDP_STATUS_SUCCESS = 0x00110000
INVALID_HANDLE = ctypes.c_void_p(-1).value
USHORT = wintypes.USHORT


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ULONG_PTR)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wintypes.ULONG), ("VendorID", USHORT),
                ("ProductID", USHORT), ("VersionNumber", USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", USHORT), ("UsagePage", USHORT),
                ("InputReportByteLength", USHORT), ("OutputReportByteLength", USHORT),
                ("FeatureReportByteLength", USHORT), ("Reserved", USHORT * 17),
                ("NumberLinkCollectionNodes", USHORT),
                ("NumberInputButtonCaps", USHORT), ("NumberInputValueCaps", USHORT),
                ("NumberInputDataIndices", USHORT),
                ("NumberOutputButtonCaps", USHORT), ("NumberOutputValueCaps", USHORT),
                ("NumberOutputDataIndices", USHORT),
                ("NumberFeatureButtonCaps", USHORT), ("NumberFeatureValueCaps", USHORT),
                ("NumberFeatureDataIndices", USHORT)]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                          wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
hiddll.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hiddll.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hiddll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hiddll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hiddll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hiddll.HidP_GetCaps.restype = ctypes.c_long
hiddll.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]


def _open(path, access):
    h = kernel32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             None, OPEN_EXISTING, 0, None)
    return None if h in (None, 0, INVALID_HANDLE) else h


def _hid_paths():
    guid = GUID()
    hiddll.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev in (None, INVALID_HANDLE):
        return []
    paths = []
    try:
        i = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(ifd)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid),
                                                        i, ctypes.byref(ifd)):
                break
            i += 1
            need = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), None, 0,
                                                      ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value + 2)
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), buf,
                                                         need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def find_devices():
    found = []
    for path in _hid_paths():
        h = _open(path, 0)          # query-only access works even for devices in use
        if not h:
            continue
        try:
            attr = HIDD_ATTRIBUTES()
            attr.Size = ctypes.sizeof(attr)
            if not hiddll.HidD_GetAttributes(h, ctypes.byref(attr)):
                continue
            vid, pid = attr.VendorID, attr.ProductID
            if vid not in VENDORS or (vid == 0x046D and pid not in KNOWN_046D):
                continue
            pp = ctypes.c_void_p()
            if not hiddll.HidD_GetPreparsedData(h, ctypes.byref(pp)):
                continue
            caps = HIDP_CAPS()
            ok = hiddll.HidP_GetCaps(pp, ctypes.byref(caps)) == HIDP_STATUS_SUCCESS
            hiddll.HidD_FreePreparsedData(pp)
            if not ok or not (caps.UsagePage == 0x01 and caps.Usage == 0x08):
                continue            # only the multi-axis controller collection
            name = ctypes.create_unicode_buffer(128)
            hiddll.HidD_GetProductString(h, name, ctypes.sizeof(name))
            found.append({"path": path, "vendor_id": vid, "product_id": pid,
                          "name": name.value or f"{vid:04x}:{pid:04x}",
                          "report_len": caps.InputReportByteLength})
        finally:
            kernel32.CloseHandle(h)
    return found


def s16(lo, hi):
    v = lo | (hi << 8)
    return v - 65536 if v >= 32768 else v


class DeviceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.axes = dict.fromkeys(AXES, 0)
        self.buttons = 0
        self.last = 0.0
        self.pressed = []


class Reader(threading.Thread):
    _warned = set()

    def __init__(self, info, state, stop):
        super().__init__(daemon=True, name="hid-reader")
        self.info, self.state, self.stop_evt = info, state, stop
        self.alive = True
        self.opened = False
        self.handle = None
        self.six_axis = info["report_len"] >= 13

    def run(self):
        h = _open(self.info["path"], GENERIC_READ)
        if not h:
            if self.info["path"] not in Reader._warned:
                Reader._warned.add(self.info["path"])
                log.warning("Couldn't open %s (Windows error %s) - another program may be "
                            "holding it exclusively", self.info["name"], ctypes.get_last_error())
            self.alive = False
            return
        self.handle, self.opened = h, True
        n = self.info["report_len"]
        buf = (ctypes.c_ubyte * n)()
        got = wintypes.DWORD()
        log.info("Connected: %s", self.info["name"])
        try:
            while not self.stop_evt.is_set():
                if not kernel32.ReadFile(h, buf, n, ctypes.byref(got), None):
                    if not self.stop_evt.is_set():
                        log.info("Disconnected: %s", self.info["name"])
                    break
                if got.value:
                    self.parse(buf[:got.value])
        finally:
            self.alive = False
            kernel32.CloseHandle(h)

    def cancel(self):
        if self.handle and self.alive:
            kernel32.CancelIoEx(self.handle, None)

    def parse(self, d):
        st = self.state
        with st.lock:
            rid = d[0]
            if rid == 1 and self.six_axis:
                for i, a in enumerate(AXES):
                    st.axes[a] = s16(d[1 + 2 * i], d[2 + 2 * i])
            elif rid == 1 and len(d) >= 7:
                for i, a in enumerate(AXES[:3]):
                    st.axes[a] = s16(d[1 + 2 * i], d[2 + 2 * i])
            elif rid == 2 and len(d) >= 7:
                for i, a in enumerate(AXES[3:]):
                    st.axes[a] = s16(d[1 + 2 * i], d[2 + 2 * i])
            elif rid == 3:
                new = int.from_bytes(bytes(d[1:]), "little")
                down = new & ~st.buttons
                st.pressed += [b for b in range(new.bit_length()) if down >> b & 1]
                st.buttons = new
                return
            else:
                return
            st.last = time.perf_counter()


# ============================== engine ==============================

def shape(mag, deadzone, curve):
    deadzone = min(deadzone, FULL_SCALE * 0.8)
    if mag <= deadzone:
        return 0.0
    n = min(1.0, (mag - deadzone) / (FULL_SCALE - deadzone))
    return n ** curve


def precise_sleep_until(t):
    while True:
        rem = t - time.perf_counter()
        if rem <= 0:
            return
        time.sleep(rem - 0.0015 if rem > 0.002 else 0)


class Engine:
    """Reads the SpaceMouse and drives Miro. Runs on its own thread; settings are
    read live each tick, except the navigation method (which restarts the engine)."""

    def __init__(self, settings):
        self.settings = settings
        self.paused = False
        self.precision = False
        self.devices = []
        self.error = None
        self.live_axes = dict.fromkeys(AXES, 0)
        self.last_button = None
        self._thread = None
        self._stop = None

    def start(self):
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="engine")
        self._thread.start()

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=3)
            self._thread = None

    def restart(self):
        self.stop()
        self.start()

    def status_text(self):
        if self.error:
            return f"Error: {self.error}"
        if self.paused:
            return "Paused"
        if not self.devices:
            return "Waiting for SpaceMouse"
        extra = " (precision)" if self.precision else ""
        return f"Active{extra}: {', '.join(self.devices)}"

    def _run(self):
        try:
            self._loop(self._stop)
        except Exception as e:
            log.exception("Engine stopped with an error")
            self.error = str(e)

    def _loop(self, stop):
        S = self.settings
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
        state = DeviceState()
        readers = {}
        touch = TouchNav() if S["nav_method"] == "touch" else None
        vel = {"pan_x": 0.0, "pan_y": 0.0, "zoom": 0.0}
        acc = {"pan_x": 0.0, "pan_y": 0.0, "zoom": 0.0}
        tick = 1.0 / TICK_HZ
        last_scan, last_moving, last_wheel = -10.0, 0.0, 0.0
        prev = time.perf_counter()
        next_tick = prev
        log.info("Engine started (%s)", S["nav_method"])

        def release_all():
            if touch:
                touch.stop()
            for k in vel:
                vel[k] = acc[k] = 0.0

        try:
            while not stop.is_set():
                next_tick += tick
                now = time.perf_counter()
                dt = min(0.05, now - prev)
                prev = now

                if now - last_scan > 2.0:     # hot-plug / wireless wake-up
                    last_scan = now
                    for p in [p for p, r in readers.items() if not r.alive]:
                        del readers[p]
                    for info in find_devices():
                        if info["path"] not in readers:
                            r = Reader(info, state, stop)
                            readers[info["path"]] = r
                            r.start()
                    self.devices = [r.info["name"] for r in readers.values() if r.alive and r.opened]

                with state.lock:
                    axes = dict(state.axes)
                    stale = now - state.last > STALE_AFTER
                    pressed, state.pressed = state.pressed, []
                self.live_axes = axes

                combos = []
                for b in pressed:
                    self.last_button = b
                    action = S["buttons"].get(str(b), "")
                    if action == "@pause":
                        self.paused = not self.paused
                        log.info("Paused" if self.paused else "Resumed")
                    elif action == "@precision":
                        self.precision = not self.precision
                    elif action:
                        try:
                            combos.append(parse_combo(action))
                        except ValueError:
                            pass

                hwnd = None
                if not self.paused:
                    hwnd = miro_window(S, held=bool(touch and touch.active))
                if not hwnd:
                    release_all()
                    precise_sleep_until(next_tick)
                    continue
                for c in combos:
                    send_combo(c)
                if stale:
                    axes = dict.fromkeys(AXES, 0)

                # ---- puck -> target velocities ----
                if S["pan_source"] == "tilt":
                    px, py = axes["ry"], axes["rx"]
                else:
                    px, py = axes["x"], axes["y"]
                pz = axes["rz"] if S["zoom_source"] == "twist" else axes["z"]
                if S["invert_x"]:
                    px = -px
                if S["invert_y"]:
                    py = -py
                if S["invert_zoom"]:
                    pz = -pz
                pan_mag = (px * px + py * py) ** 0.5
                zoom_mag = abs(pz)
                curve = S["curve"]
                pan_s = shape(pan_mag, S["pan_deadzone"] + S["crosstalk"] * zoom_mag, curve)
                zoom_s = shape(zoom_mag, S["zoom_deadzone"] + S["crosstalk"] * pan_mag, curve)
                scale = S["precision_factor"] if self.precision else 1.0
                target = {"pan_x": 0.0, "pan_y": 0.0,
                          "zoom": zoom_s * scale * (1 if pz > 0 else -1)}   # zoom: -1..1
                if pan_s:
                    target["pan_x"] = px / pan_mag * pan_s * S["pan_speed"] * scale
                    target["pan_y"] = py / pan_mag * pan_s * S["pan_speed"] * scale

                # ---- smoothing ----
                sm = S["smoothing"]
                alpha = 1.0 if sm <= 0 else 1.0 - 2.718281828 ** (-dt / sm)
                for k in vel:
                    vel[k] += alpha * (target[k] - vel[k])
                    floor = 0.01 if k == "zoom" else 5.0
                    if target[k] == 0 and abs(vel[k]) < floor:
                        vel[k] = 0.0
                moving = any(vel.values())
                if moving:
                    last_moving = now

                if touch:
                    # one two-finger gesture does pan and zoom together
                    dx, dy = vel["pan_x"] * dt, vel["pan_y"] * dt
                    zlog = vel["zoom"] * S["zoom_speed"] * dt
                    if touch.active:
                        if moving or now - last_moving <= TOUCH_RELEASE_DELAY:
                            touch.step(dx, dy, zlog)
                        else:
                            touch.stop()
                    elif moving:
                        touch.start(hwnd, cursor_pos())
                        touch.step(dx, dy, zlog)
                else:
                    # trackpad-style scroll fallback
                    acc["pan_x"] += vel["pan_x"] * WHEEL_UNITS_PER_PX * dt
                    acc["pan_y"] += vel["pan_y"] * WHEEL_UNITS_PER_PX * dt
                    acc["zoom"] += vel["zoom"] * S["zoom_speed"] * WHEEL_ZOOM_UNITS * dt
                    for k in acc:
                        if not vel[k]:
                            acc[k] = 0.0
                    if now - last_wheel >= 1.0 / WHEEL_EVENT_HZ - 0.001:
                        events = []
                        hx, vy, zm = int(acc["pan_x"]), int(acc["pan_y"]), int(acc["zoom"])
                        if hx:
                            events.append(_mouse(MOUSEEVENTF_HWHEEL, hx))
                        if vy:
                            events.append(_mouse(MOUSEEVENTF_WHEEL, -vy))
                        if zm:
                            events += [_key(VK_CONTROL), _mouse(MOUSEEVENTF_WHEEL, zm),
                                       _key(VK_CONTROL, True)]
                        if events:
                            send(events)
                            last_wheel = now
                            acc["pan_x"] -= hx
                            acc["pan_y"] -= vy
                            acc["zoom"] -= zm

                if now - next_tick > 0.1:
                    next_tick = time.perf_counter()
                precise_sleep_until(next_tick)
        finally:
            release_all()
            send([_key(VK_CONTROL, True)])
            for r in readers.values():
                r.cancel()
            winmm.timeEndPeriod(1)
            self.devices = []
            log.info("Engine stopped")


# ============================== start with Windows ==============================

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_ID)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass


# ============================== tray icon ==============================

WM_DESTROY, WM_CLOSE, WM_TIMER, WM_NULL = 0x0002, 0x0010, 0x0113, 0x0000
WM_CONTEXTMENU, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x007B, 0x0203, 0x0205
WM_TRAY = 0x8000 + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_WARNING = 0x2
MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ULONG_PTR, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetTimer.argtypes = [wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreateIcon.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_ubyte,
                              ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_void_p]
user32.CreateIcon.restype = wintypes.HICON
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

CMD_PAUSE, CMD_PRECISION, CMD_SETTINGS, CMD_LOG, CMD_AUTOSTART, CMD_QUIT = range(1, 7)


def make_icon(hinst, fill, ring):
    """32x32 'puck' icon drawn in code: a coloured cap with a dark rim."""
    size = 32
    xor = bytearray(size * size * 4)
    c = (size - 1) / 2
    for y in range(size):
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            a = max(0.0, min(1.0, 14.5 - d))           # anti-aliased outer edge
            if a <= 0:
                continue
            col = ring if d > 11.5 or 5.0 < d < 6.5 else fill
            i = (y * size + x) * 4
            xor[i:i + 4] = bytes((col[2], col[1], col[0], int(255 * a)))
    and_mask = bytes(size * size // 8)
    return user32.CreateIcon(hinst, size, size, 1, 32, and_mask, bytes(xor))


class Tray:
    def __init__(self, engine, ui_queue):
        self.engine, self.ui = engine, ui_queue
        self.hwnd = None
        self._proc = WNDPROC(self._wndproc)    # keep a reference so it isn't collected
        self._last_tip = None
        self._error_shown = None
        self.ready = threading.Event()

    def run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self.icon_on = make_icon(hinst, (255, 208, 47), (45, 45, 55))
        self.icon_off = make_icon(hinst, (175, 175, 180), (90, 90, 95))
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = APP_ID + "Tray"
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, APP_NAME, 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._notify(NIM_ADD)
        user32.SetTimer(self.hwnd, 1, 500, None)
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        return nid

    def _notify(self, action):
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        active = not self.engine.paused and not self.engine.error
        nid.hIcon = self.icon_on if active else self.icon_off
        tip = f"{APP_NAME}\n{self.engine.status_text()}"
        nid.szTip = tip[:127]
        self._last_tip = tip
        shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    def balloon(self, title, text):
        nid = self._nid()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_WARNING
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _refresh(self):
        tip = f"{APP_NAME}\n{self.engine.status_text()}"
        if tip != self._last_tip:
            self._notify(NIM_MODIFY)
        err = self.engine.error
        if err and err != self._error_shown:
            self._error_shown = err
            self.balloon(APP_NAME, f"Stopped: {err}. See the log for details.")

    def _menu(self):
        e = self.engine
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, e.status_text()[:80])
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if e.paused else 0), CMD_PAUSE, "Pause")
        user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if e.precision else 0), CMD_PRECISION,
                           "Precision mode (slower)")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, CMD_SETTINGS, "Settings…")
        user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if autostart_enabled() else 0),
                           CMD_AUTOSTART, "Start with Windows")
        user32.AppendMenuW(m, MF_STRING, CMD_LOG, "Open log")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, CMD_QUIT, "Quit")
        x, y = cursor_pos()
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                                    x, y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(m)

        if cmd == CMD_PAUSE:
            e.paused = not e.paused
            self._notify(NIM_MODIFY)
        elif cmd == CMD_PRECISION:
            e.precision = not e.precision
            self._notify(NIM_MODIFY)
        elif cmd == CMD_SETTINGS:
            self.ui.put("settings")
        elif cmd == CMD_AUTOSTART:
            try:
                set_autostart(not autostart_enabled())
            except OSError as ex:
                self.balloon(APP_NAME, f"Couldn't change start-up setting: {ex}")
        elif cmd == CMD_LOG:
            if os.path.exists(LOG_PATH):
                os.startfile(LOG_PATH)
        elif cmd == CMD_QUIT:
            self.ui.put("quit")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                elif lparam == WM_LBUTTONDBLCLK:
                    self.ui.put("settings")
                return 0
            if msg == WM_TIMER:
                self._refresh()
                return 0
            if msg == self.taskbar_created:      # Explorer restarted: re-add the icon
                self._notify(NIM_ADD)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("Tray error")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ============================== settings window ==============================

ACTIONS = [  # (label shown in the list, key combo or special action)
    ("Nothing", ""),
    ("Zoom to fit", "ALT+1"),
    ("Zoom to selected item", "ALT+2"),
    ("Zoom to 100%", "CTRL+0"),
    ("Undo", "CTRL+Z"),
    ("Redo", "CTRL+SHIFT+Z"),
    ("Select tool", "V"),
    ("Hand tool", "H"),
    ("Copy", "CTRL+C"),
    ("Paste", "CTRL+V"),
    ("Delete", "DEL"),
    ("Escape", "ESC"),
    ("Pause SpaceMouse", "@pause"),
    ("Toggle precision mode", "@precision"),
]
CUSTOM = "Custom keys…"


def _norm(combo):
    return combo.replace(" ", "").upper()


class SettingsWindow:
    def __init__(self, app):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.app = tk, ttk, app
        S = app.settings
        self.vars = {}
        self.button_rows = {}

        top = self.top = tk.Toplevel(app.root)
        top.title(f"{APP_NAME} settings")
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.close)

        nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        # --- Speed ---
        f = self._tab(nb, "Speed")
        self._slider(f, "pan_speed", "Pan speed", 500, 10000, 100, int,
                     "How fast the board moves at a full push.")
        self._slider(f, "zoom_speed", "Zoom speed", 0.5, 6.0, 0.1, float,
                     "How fast it zooms at a full push.")
        self._slider(f, "curve", "Response curve", 1.0, 3.0, 0.1, float,
                     "Higher values give finer control of gentle pushes.")
        self._slider(f, "smoothing", "Smoothing", 0.0, 0.2, 0.005, float,
                     "Higher feels silkier but responds a little later. 0 turns it off.")
        self._slider(f, "precision_factor", "Precision mode speed", 0.1, 1.0, 0.05, float,
                     "Speed multiplier while precision mode is on.")

        # --- Axes ---
        f = self._tab(nb, "Axes")
        self._radio(f, "pan_source", "Pan by", [("slide", "Sliding the puck"),
                                                ("tilt", "Tilting the puck")])
        self._radio(f, "zoom_source", "Zoom by", [("push", "Pushing / pulling the puck"),
                                                  ("twist", "Twisting the puck")])
        self._check(f, "invert_x", "Reverse left / right")
        self._check(f, "invert_y", "Reverse forward / back")
        self._check(f, "invert_zoom", "Reverse zoom direction")
        self._slider(f, "pan_deadzone", "Pan dead zone", 0, 150, 1, int,
                     "Ignore pan movements smaller than this.")
        self._slider(f, "zoom_deadzone", "Zoom dead zone", 0, 200, 1, int,
                     "Ignore zoom movements smaller than this.")
        self._slider(f, "crosstalk", "Accidental-move filter", 0.0, 0.8, 0.05, float,
                     "Raise if zooming makes the board drift sideways, or panning zooms.")

        # --- Buttons ---
        f = self._tab(nb, "Buttons")
        ttk.Label(f, text="Press a button on your SpaceMouse to add it, then choose what it does.",
                  wraplength=460).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(f, text="SpaceMouse Pro: Fit is button 1, buttons 1–4 are 12–15.",
                  foreground="#666").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 8))
        self.rows_frame = ttk.Frame(f)
        self.rows_frame.grid(row=2, column=0, columnspan=4, sticky="we")
        add = ttk.Frame(f)
        add.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.last_btn_label = ttk.Label(add, text="Last button pressed: none yet")
        self.last_btn_label.pack(side="left")
        self.add_btn = ttk.Button(add, text="Add this button", command=self._add_last, state="disabled")
        self.add_btn.pack(side="left", padx=8)
        self._build_button_rows(S["buttons"])

        # --- Advanced ---
        f = self._tab(nb, "Advanced")
        self._radio(f, "nav_method", "Control Miro with", [
            ("touch", "Two-finger touch gesture (recommended)"),
            ("wheel", "Scroll wheel, trackpad-style (fallback)")])
        self._check(f, "require_pointer_over_window", "Only when the pointer is over the Miro window")
        self._entry(f, "target_processes", "Apps (program names)", "Comma separated, e.g. miro.exe")
        self._entry(f, "title_keywords", "Browser tabs (title contains)", "Comma separated, e.g. miro")
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        r = self._next_row(f)
        ttk.Checkbutton(f, text="Start with Windows", variable=self.autostart_var).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # --- footer ---
        foot = ttk.Frame(top, padding=(12, 8, 12, 12))
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="", foreground="#555")
        self.status.pack(side="top", anchor="w", pady=(0, 8))
        ttk.Button(foot, text="Restore defaults", command=self.restore_defaults).pack(side="left")
        ttk.Button(foot, text="Save changes", command=self.save).pack(side="right")
        ttk.Button(foot, text="Cancel", command=self.close).pack(side="right", padx=6)

        self._tick()
        top.lift()
        top.focus_force()

    # ---- layout helpers ----
    def _tab(self, nb, title):
        f = self.ttk.Frame(nb, padding=14)
        f.columnconfigure(1, weight=1)
        f._row = 0
        nb.add(f, text=title)
        return f

    def _next_row(self, f):
        r = f._row
        f._row += 1
        return r

    def _slider(self, f, key, label, lo, hi, res, cast, hint):
        tk, ttk = self.tk, self.ttk
        var = tk.DoubleVar(value=self.app.settings[key])
        self.vars[key] = (var, cast)
        r = self._next_row(f)
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10))
        tk.Scale(f, variable=var, from_=lo, to=hi, resolution=res, orient="horizontal",
                 length=280, showvalue=True, highlightthickness=0).grid(row=r, column=1, sticky="we")
        r = self._next_row(f)
        ttk.Label(f, text=hint, foreground="#666", wraplength=380).grid(
            row=r, column=1, sticky="w", pady=(0, 8))

    def _check(self, f, key, text):
        var = self.tk.BooleanVar(value=self.app.settings[key])
        self.vars[key] = (var, bool)
        r = self._next_row(f)
        self.ttk.Checkbutton(f, text=text, variable=var).grid(row=r, column=0, columnspan=2,
                                                             sticky="w", pady=2)

    def _radio(self, f, key, label, options):
        var = self.tk.StringVar(value=self.app.settings[key])
        self.vars[key] = (var, str)
        r = self._next_row(f)
        self.ttk.Label(f, text=label).grid(row=r, column=0, sticky="nw", padx=(0, 10))
        box = self.ttk.Frame(f)
        box.grid(row=r, column=1, sticky="w", pady=(0, 8))
        for val, text in options:
            self.ttk.Radiobutton(box, text=text, value=val, variable=var).pack(anchor="w")

    def _entry(self, f, key, label, hint):
        var = self.tk.StringVar(value=self.app.settings[key])
        self.vars[key] = (var, str)
        r = self._next_row(f)
        self.ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        self.ttk.Entry(f, textvariable=var, width=36).grid(row=r, column=1, sticky="w", pady=(6, 0))
        r = self._next_row(f)
        self.ttk.Label(f, text=hint, foreground="#666").grid(row=r, column=1, sticky="w")

    # ---- buttons tab ----
    def _build_button_rows(self, mapping):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self.button_rows = {}
        for idx in sorted(mapping, key=lambda k: int(k)):
            self._add_row(idx, mapping[idx])

    def _add_row(self, idx, combo):
        tk, ttk = self.tk, self.ttk
        idx = str(idx)
        if idx in self.button_rows:
            return
        r = len(self.rows_frame.winfo_children()) // 4
        names = [a[0] for a in ACTIONS] + [CUSTOM]
        preset = next((n for n, c in ACTIONS if _norm(c) == _norm(combo)), None)
        choice = tk.StringVar(value=preset or CUSTOM)
        keys = tk.StringVar(value=combo if not preset else "")
        ttk.Label(self.rows_frame, text=f"Button {idx}", width=10).grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(self.rows_frame, textvariable=choice, values=names, state="readonly", width=24)
        cb.grid(row=r, column=1, padx=4)
        ent = ttk.Entry(self.rows_frame, textvariable=keys, width=18)
        ent.grid(row=r, column=2, padx=4)

        def sync(*_):
            ent.configure(state="normal" if choice.get() == CUSTOM else "disabled")
        cb.bind("<<ComboboxSelected>>", sync)
        sync()

        def remove():
            for w in self.rows_frame.grid_slaves(row=r):
                w.grid_remove()
            self.button_rows.pop(idx, None)
        ttk.Button(self.rows_frame, text="Remove", command=remove).grid(row=r, column=3, padx=4)
        self.button_rows[idx] = (choice, keys)

    def _add_last(self):
        b = self.app.engine.last_button
        if b is not None:
            self._add_row(str(b), "")

    # ---- live status ----
    def _tick(self):
        try:
            if not self.top.winfo_exists():
                return
        except self.tk.TclError:
            return
        e = self.app.engine
        a = e.live_axes
        axes = "   ".join(f"{k} {a[k]:+4d}" for k in AXES)
        self.status.configure(text=f"{e.status_text()}\nLive input:  {axes}")
        if e.last_button is not None:
            self.last_btn_label.configure(text=f"Last button pressed: {e.last_button}")
            self.add_btn.configure(state="normal")
        self.top.after(100, self._tick)

    # ---- actions ----
    def restore_defaults(self):
        for key, (var, cast) in self.vars.items():
            var.set(DEFAULTS[key])
        self._build_button_rows(DEFAULTS["buttons"])

    def save(self):
        from tkinter import messagebox
        new = {}
        for key, (var, cast) in self.vars.items():
            val = var.get()
            new[key] = round(val) if cast is int else cast(val)
        buttons = {}
        for idx, (choice, keys) in self.button_rows.items():
            name = choice.get()
            combo = keys.get().strip() if name == CUSTOM else dict(ACTIONS).get(name, "")
            if not combo:
                continue
            if not combo.startswith("@"):
                try:
                    parse_combo(combo)
                except ValueError as ex:
                    messagebox.showerror(APP_NAME, f"Button {idx}: {ex}.\n\nUse key names joined "
                                         "with +, for example CTRL+SHIFT+Z or ALT+1.", parent=self.top)
                    return
                combo = _norm(combo)
            buttons[idx] = combo
        new["buttons"] = buttons

        S = self.app.settings
        method_changed = new["nav_method"] != S["nav_method"]
        S.data.update(new)
        try:
            S.save()
        except OSError as ex:
            messagebox.showerror(APP_NAME, f"Couldn't save settings: {ex}", parent=self.top)
            return
        try:
            if self.autostart_var.get() != autostart_enabled():
                set_autostart(self.autostart_var.get())
        except OSError as ex:
            messagebox.showwarning(APP_NAME, f"Couldn't change the start-up setting: {ex}", parent=self.top)
        if method_changed:
            self.app.engine.restart()
        log.info("Settings saved")
        self.close()

    def close(self):
        self.top.destroy()
        self.app.settings_window = None


class App:
    def __init__(self, settings, engine):
        import tkinter as tk
        self.settings, self.engine = settings, engine
        self.root = tk.Tk()
        self.root.withdraw()
        self.queue = queue.Queue()
        self.settings_window = None
        self.root.after(100, self._poll)

    def _poll(self):
        while not self.queue.empty():
            cmd = self.queue.get()
            if cmd == "settings":
                if self.settings_window and self.settings_window.top.winfo_exists():
                    self.settings_window.top.deiconify()
                    self.settings_window.top.lift()
                    self.settings_window.top.focus_force()
                else:
                    self.settings_window = SettingsWindow(self)
            elif cmd == "quit":
                self.root.quit()
                return
        self.root.after(100, self._poll)

    def run(self):
        self.root.mainloop()


# ============================== entry points ==============================

def setup_logging():
    os.makedirs(APP_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
        os.replace(LOG_PATH, LOG_PATH + ".old")
    handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s  %(levelname)s  %(message)s")


def cmd_list():
    devs = find_devices()
    for d in devs:
        print(f"{d['vendor_id']:04x}:{d['product_id']:04x}  {d['name']}  (report length {d['report_len']})")
    print(f"{len(devs)} device(s) found")


def cmd_debug():
    state, stop, readers = DeviceState(), threading.Event(), []
    for info in find_devices():
        r = Reader(info, state, stop)
        r.start()
        readers.append(r)
    if not readers:
        print("No SpaceMouse found.")
        return
    print("Move the puck and press buttons. Ctrl+C to stop.")
    try:
        while True:
            with state.lock:
                axes = dict(state.axes)
                pressed, state.pressed = state.pressed, []
                buttons = state.buttons
            vals = "  ".join(f"{a}:{axes[a]:5d}" for a in AXES)
            print(f"\r{vals}  buttons:{buttons:#06x}   ", end="", flush=True)
            for b in pressed:
                print(f"\nButton {b} pressed")
            time.sleep(0.05)
    except KeyboardInterrupt:
        stop.set()
        for r in readers:
            r.cancel()
        print()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--list", action="store_true", help="list detected 3Dconnexion devices")
    ap.add_argument("--debug", action="store_true", help="show raw puck and button values")
    args = ap.parse_args()
    if args.list:
        return cmd_list()
    if args.debug:
        return cmd_debug()

    setup_logging()
    mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}")  # noqa: F841 (held for app lifetime)
    if ctypes.get_last_error() == 183:    # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, f"{APP_NAME} is already running - look for its icon in the "
                           "system tray (you may need to click ^ to see it).", APP_NAME, 0x40)
        return

    make_dpi_aware()
    settings = Settings(SETTINGS_PATH)
    engine = Engine(settings)
    engine.start()
    try:
        app = App(settings, engine)
    except Exception as e:
        log.exception("Couldn't start the settings UI")
        user32.MessageBoxW(None, f"Couldn't start: {e}\n\nIs Python installed with tcl/tk?",
                           APP_NAME, 0x10)
        engine.stop()
        return
    tray = Tray(engine, app.queue)
    threading.Thread(target=tray.run, daemon=True, name="tray").start()
    log.info("%s started", APP_NAME)
    try:
        app.run()
    finally:
        tray.close()
        engine.stop()
        log.info("%s stopped", APP_NAME)


if __name__ == "__main__":
    main()
