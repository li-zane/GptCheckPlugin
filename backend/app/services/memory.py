from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from collections.abc import Iterable


class ProcessMemorySampler:
    """Sample RSS for this process tree without retaining per-sample history."""

    def __init__(self, root_pid: int | None = None, interval_seconds: float = 1.0) -> None:
        self.root_pid = root_pid or os.getpid()
        self.interval_seconds = max(0.2, interval_seconds)
        self.peak_rss_bytes: int | None = None
        self.sample_count = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> ProcessMemorySampler:
        self._sample_once()
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._sample_once()
        self._stop.set()
        if self._task:
            await self._task
        self._sample_once()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    def _sample_once(self) -> None:
        try:
            rss_bytes = process_tree_rss_bytes(self.root_pid)
        except Exception:
            return
        if rss_bytes <= 0:
            return
        self.sample_count += 1
        if self.peak_rss_bytes is None or rss_bytes > self.peak_rss_bytes:
            self.peak_rss_bytes = rss_bytes


def process_tree_rss_bytes(root_pid: int | None = None) -> int:
    pid = root_pid or os.getpid()
    psutil_value = _psutil_tree_rss_bytes(pid)
    if psutil_value is not None:
        return psutil_value
    if sys.platform.startswith("win"):
        return _windows_tree_rss_bytes(pid)
    return _procfs_tree_rss_bytes(pid)


_PSUTIL_IMPORT_ATTEMPTED = False
_PSUTIL = None


def _psutil_tree_rss_bytes(root_pid: int) -> int | None:
    global _PSUTIL_IMPORT_ATTEMPTED, _PSUTIL
    if not _PSUTIL_IMPORT_ATTEMPTED:
        _PSUTIL_IMPORT_ATTEMPTED = True
        try:
            import psutil  # type: ignore[import-not-found]

            _PSUTIL = psutil
        except Exception:
            _PSUTIL = None
    if _PSUTIL is None:
        return None

    try:
        root = _PSUTIL.Process(root_pid)
        processes = [root, *_safe_children(root)]
        total = 0
        for process in processes:
            try:
                total += int(process.memory_info().rss)
            except Exception:
                continue
        return total
    except Exception:
        return None


def _safe_children(process) -> list:
    try:
        return process.children(recursive=True)
    except Exception:
        return []


def _procfs_tree_rss_bytes(root_pid: int) -> int:
    proc_root = "/proc"
    if not os.path.isdir(proc_root):
        return 0

    page_size = _page_size()
    parent_by_pid: dict[int, int] = {}
    rss_by_pid: dict[int, int] = {}
    try:
        entries = list(os.scandir(proc_root))
    except OSError:
        return 0

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with open(os.path.join(proc_root, entry.name, "stat"), encoding="utf-8", errors="ignore") as handle:
                stat_parts = handle.read().split()
            with open(os.path.join(proc_root, entry.name, "statm"), encoding="utf-8", errors="ignore") as handle:
                statm_parts = handle.read().split()
            parent_by_pid[pid] = int(stat_parts[3])
            rss_by_pid[pid] = int(statm_parts[1]) * page_size
        except (OSError, IndexError, ValueError):
            continue

    return _sum_selected_rss(root_pid, parent_by_pid.items(), rss_by_pid)


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return 4096


def _sum_selected_rss(root_pid: int, parent_items: Iterable[tuple[int, int]], rss_by_pid: dict[int, int]) -> int:
    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for pid, parent_pid in parent_items:
        children_by_parent[parent_pid].append(pid)

    selected: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in selected:
            continue
        selected.add(pid)
        stack.extend(children_by_parent.get(pid, []))

    return sum(rss_by_pid.get(pid, 0) for pid in selected)


def _windows_tree_rss_bytes(root_pid: int) -> int:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return 0

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    parent_by_pid: dict[int, int] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return 0
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return 0
        while True:
            parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)

    selected = _selected_process_ids(root_pid, parent_by_pid.items())
    return sum(_windows_working_set_bytes(pid, kernel32, psapi, ctypes, wintypes) for pid in selected)


def _selected_process_ids(root_pid: int, parent_items: Iterable[tuple[int, int]]) -> set[int]:
    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for pid, parent_pid in parent_items:
        children_by_parent[parent_pid].append(pid)

    selected: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in selected:
            continue
        selected.add(pid)
        stack.extend(children_by_parent.get(pid, []))
    return selected


def _windows_working_set_bytes(pid: int, kernel32, psapi, ctypes, wintypes) -> int:
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    process_query_information = 0x0400
    process_vm_read = 0x0010
    process_query_limited_information = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_information | process_vm_read, False, int(pid))
    if not handle:
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)
