"""Read memory for this Windows process tree without WMI or helper processes."""
import ctypes
from ctypes import wintypes
import os


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
        ('parent_pid', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
        ('name', wintypes.WCHAR * 260),
    ]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('page_faults', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            'peak_working_set', 'working_set', 'peak_paged_pool', 'paged_pool',
            'peak_nonpaged_pool', 'nonpaged_pool', 'pagefile', 'peak_pagefile', 'private',
        )
    ]


class _PerformanceInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            'commit', 'commit_limit', 'commit_peak', 'physical', 'available', 'cache',
            'kernel', 'kernel_paged', 'kernel_nonpaged', 'page_size',
        )
    ] + [(name, wintypes.DWORD) for name in ('handles', 'processes', 'threads')]


class WindowsMemorySampler:
    """Each call returns bounded scalar metadata; every native handle is closed."""

    def __init__(self, pid=None):
        self.pid = pid or os.getpid()
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.psapi = ctypes.WinDLL('psapi', use_last_error=True)
        signatures = (
            (self.kernel.CreateToolhelp32Snapshot, [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel.Process32FirstW, [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            (self.kernel.Process32NextW, [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            (self.kernel.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel.GetProcessTimes, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            (self.kernel.GetProcessHandleCount, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.psapi.GetProcessMemoryInfo, [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD], wintypes.BOOL),
            (self.psapi.GetPerformanceInfo, [ctypes.POINTER(_PerformanceInfo), wintypes.DWORD], wintypes.BOOL),
        )
        for function, arguments, result in signatures:
            function.argtypes, function.restype = arguments, result

    def _processes(self):
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            if not self.kernel.Process32FirstW(handle, ctypes.byref(entry)):
                raise ctypes.WinError(ctypes.get_last_error())
            records = {}
            while True:
                records[entry.pid] = {
                    'pid': entry.pid, 'parent_pid': entry.parent_pid,
                    'name': entry.name, 'threads': entry.threads,
                }
                if not self.kernel.Process32NextW(handle, ctypes.byref(entry)):
                    code = ctypes.get_last_error()
                    if code != 18:  # ERROR_NO_MORE_FILES
                        raise ctypes.WinError(code)
                    return records
        finally:
            self.kernel.CloseHandle(handle)

    def _memory(self, record):
        # QUERY_INFORMATION | VM_READ, no write or terminate permissions.
        handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, record['pid'])
        if not handle:
            return {**record, 'read_error_code': ctypes.get_last_error()}
        try:
            memory = _MemoryCounters()
            memory.size = ctypes.sizeof(memory)
            if not self.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.size):
                return {**record, 'read_error_code': ctypes.get_last_error()}
            result = {
                **record, 'working_set_bytes': memory.working_set,
                'private_bytes': memory.private, 'peak_working_set_bytes': memory.peak_working_set,
                'peak_private_bytes': memory.peak_pagefile,
            }
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                result['created_filetime'] = (created.dwHighDateTime << 32) | created.dwLowDateTime
            handles = wintypes.DWORD()
            if self.kernel.GetProcessHandleCount(handle, ctypes.byref(handles)):
                result['handles'] = handles.value
            return result
        finally:
            self.kernel.CloseHandle(handle)

    def __call__(self):
        result = {}
        performance = _PerformanceInfo()
        performance.size = ctypes.sizeof(performance)
        if self.psapi.GetPerformanceInfo(ctypes.byref(performance), performance.size):
            result['system'] = {
                'physical_total_bytes': performance.physical * performance.page_size,
                'physical_available_bytes': performance.available * performance.page_size,
                'committed_bytes': performance.commit * performance.page_size,
                'commit_limit_bytes': performance.commit_limit * performance.page_size,
                'commit_peak_bytes': performance.commit_peak * performance.page_size,
            }
        else:
            result['system_read_error_code'] = ctypes.get_last_error()
        processes = self._processes()
        related = {self.pid}
        while True:
            added = {pid for pid, item in processes.items() if item['parent_pid'] in related} - related
            if not added:
                break
            related.update(added)
        ids = sorted(related & processes.keys())
        result['processes'] = [self._memory(processes[pid]) for pid in ids[:128]]
        result['omitted_processes'] = max(0, len(ids) - 128)
        return result
