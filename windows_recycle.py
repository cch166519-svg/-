"""Windows 8+ recycle-only IFileOperation; no permanent-delete fallback."""
import ctypes as C
from ctypes import wintypes as W
import uuid

def recycle_file(path):
    ole=C.OleDLL('ole32');shell=C.OleDLL('shell32')
    ptr=C.c_void_p
    def guid(s): return (C.c_ubyte*16).from_buffer_copy(uuid.UUID(s).bytes_le)
    def checked(hr):
        if hr<0: raise OSError('Windows recycle failed: 0x%08X'%(hr & 0xffffffff))
    def call(obj,index,types,*args):
        table=C.cast(obj,C.POINTER(C.POINTER(ptr))).contents
        return C.WINFUNCTYPE(C.c_long,ptr,*types)(table[index])(obj,*args)
    hr=ole.CoInitializeEx(None,2);checked(hr)
    operation=ptr();item=ptr()
    try:
        cls=guid('3ad05575-8857-4850-9277-11b85bdb8e09');iid=guid('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')
        ole.CoCreateInstance.argtypes=[ptr,ptr,W.DWORD,ptr,C.POINTER(ptr)]
        checked(ole.CoCreateInstance(C.byref(cls),None,1,C.byref(iid),C.byref(operation)))
        # Recycle only, stop at first error, no shell confirmation/progress/error UI.
        checked(call(operation,5,[W.DWORD],0x00080000|0x00100000|0x20000000|0x400|0x10|0x4))
        si=guid('43826d1e-e718-42ee-bc55-a1e261c37bfe')
        shell.SHCreateItemFromParsingName.argtypes=[W.LPCWSTR,ptr,ptr,C.POINTER(ptr)]
        checked(shell.SHCreateItemFromParsingName(str(path),None,C.byref(si),C.byref(item)))
        checked(call(operation,18,[ptr,ptr],item,None))
        checked(call(operation,21,[]))
        aborted=W.BOOL()
        checked(call(operation,22,[C.POINTER(W.BOOL)],C.byref(aborted)))
        if aborted.value: raise OSError('Windows recycle operation aborted')
    finally:
        if item: call(item,2,[])
        if operation: call(operation,2,[])
        ole.CoUninitialize()
