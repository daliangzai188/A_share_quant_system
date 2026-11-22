#!/bin/sh
/usr/bin/python3 -c "
import ctypes, ctypes.util, sys
cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreGraphics')); cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreFoundation'))
cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
d = cg.CGSessionCopyCurrentDictionary(); cf.CFCopyDescription.restype = ctypes.c_void_p; cf.CFCopyDescription.argtypes = [ctypes.c_void_p]
buf = ctypes.create_string_buffer(8192); cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
cf.CFStringGetCString(cf.CFCopyDescription(d), buf, 8192, 0x08000100)
sys.exit(1 if 'CGSSessionScreenIsLocked = 1' in buf.value.decode() else 0)
" || { echo LOCKED; exit 1; }
