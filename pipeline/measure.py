"""Run one wildcat stage in a fresh process and measure it: wall time and peak memory.

Each stage gets its own process so its peak memory is its own, not the running maximum of
one long-lived process. The child reports its peak before exiting, because Windows cannot
be asked for it afterwards. Used by run_fire.py (one fire) and split_fire.py (each piece).

The child installs the NumPy 2 shim first: pysheds 0.4 calls np.in1d, which NumPy 2
removed (see run_wildcat.py).
"""

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Per-stage limit. A whole-fire 1 m assess runs for hours, so allow hours; a Slurm job
# can raise it (split_prepare.slurm sets 11 h).
STAGE_TIMEOUT = int(os.environ.get("DFH_STAGE_TIMEOUT", os.environ.get("M3_STAGE_TIMEOUT", 4 * 3600)))


def peak_mb():
    """Peak memory of this process, in MB (peak working set on Windows, max RSS elsewhere)."""
    if sys.platform == "win32":
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [
                (n, ctypes.c_size_t) for n in
                ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                 "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                 "PagefileUsage", "PeakPagefileUsage")]
        # GetCurrentProcess returns a 64-bit pseudo-handle; without an explicit restype
        # ctypes truncates it to c_int and the call silently fails, reporting 0 MB.
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC),
                                               ctypes.c_uint32]
        c = PMC()
        c.cb = ctypes.sizeof(c)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return c.PeakWorkingSetSize / 1e6
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3


def run_stage(stage, project, timeout=STAGE_TIMEOUT):
    """wildcat.<stage>(project) in a fresh process: {"seconds", "peak_mb"}, or {"error"},
    or {"timeout": True}."""
    code = (
        "import time,json,sys,numpy as np\n"
        "np.in1d = getattr(np,'in1d',np.isin)\n"
        "import wildcat\n"
        f"sys.path.insert(0,{str(HERE)!r})\n"
        "from measure import peak_mb\n"
        "t=time.perf_counter()\n"
        f"wildcat.{stage}({str(project)!r})\n"
        "print('RESULT'+json.dumps({'seconds':round(time.perf_counter()-t,2),"
        "'peak_mb':round(peak_mb(),1)}))\n"
    )
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"seconds": float("nan"), "peak_mb": float("nan"), "timeout": True}
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT"):
            return json.loads(line[6:])
    return {"error": (proc.stdout + proc.stderr)[-800:]}
