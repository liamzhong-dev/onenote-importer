# -*- coding: utf-8 -*-
"""把「OneNote 日记工具」打包成免安装的 exe。

    python pack_exe.py              # 打包 + 自检
    python pack_exe.py --clean      # 先清掉 build/dist 再打
    python pack_exe.py --no-verify  # 只打包，不启动验证

产物：`dist/OneNote日记工具/OneNote日记工具.exe`（双击即用）

三个刻意的选择：
  · `--noconsole`  —— 双击**不弹控制台黑窗**（静默启动）
  · onedir 而不是 onefile —— onefile 每次启动都要把整包解压到临时目录，
    冷启动要好几秒；onedir 是秒开。分发时整个文件夹一起拷即可。
  · `bridge/*.ps1` 打进包内（见 core/paths.py 的定位逻辑）

打包后会自动跑一遍验证：
  1. exe 在不在、桥脚本有没有进包
  2. 真的启动一次，确认进程活着、窗口正常
  3. **枚举该进程的窗口，断言里面没有控制台（黑窗）**
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "OneNote日记工具"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# 打包必须用「带 tkinter 的解释器」—— 本机托管的 3.13 没装 tkinter。
VENV_PY = ROOT.parent / ".venv-pack" / "Scripts" / "python.exe"


def find_python() -> Path:
    """挑一个能用来打包的解释器：要有 tkinter，还要有 PyInstaller。"""
    candidates = [VENV_PY, Path(sys.executable)]
    for name in ("Python313", "Python312", "Python311", "Python310"):
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / name / "python.exe")

    probe = ("import tkinter, PyInstaller; "
             "print(tkinter.TkVersion, PyInstaller.__version__)")
    for c in candidates:
        if not c or not c.exists():
            continue
        try:
            r = subprocess.run([str(c), "-c", probe], capture_output=True, timeout=90)
        except Exception:
            continue
        if r.returncode == 0:
            print(f"用这个解释器打包：{c}  (tk {r.stdout.decode().split()[0]}, "
                  f"PyInstaller {r.stdout.decode().split()[1]})")
            return c
    raise SystemExit(
        "没找到「带 tkinter 且装了 PyInstaller」的 Python。\n"
        f"先跑：\n  \"{sys.executable}\" -m pip install pyinstaller\n"
        f"（注意要用带 tkinter 的那个解释器，本机是 "
        f"%LOCALAPPDATA%\\Programs\\Python\\Python310\\python.exe）")


def build(py: Path, clean: bool) -> Path:
    if clean:
        for d in (DIST, BUILD):
            shutil.rmtree(d, ignore_errors=True)
            print(f"已清理 {d.name}/")

    cmd = [
        str(py), "-m", "PyInstaller",
        "--noconfirm", "--noconsole",
        "--name", APP_NAME,
        # 桥脚本是运行时按路径读的，不是 import 进来的 —— 必须显式带进包。
        # 必须给**绝对路径**：--specpath 把 spec 生成在 build/ 里，
        # PyInstaller 会按 spec 所在目录去解析相对路径，写成 "bridge;bridge"
        # 它会去找 build\bridge，直接报 "Unable to find ... build\bridge"。
        "--add-data", f"{ROOT / 'bridge'};bridge",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        str(ROOT / "app.py"),
    ]
    print("\n打包中（大约 1 分钟）…")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"打包失败，退出码 {r.returncode}")

    exe = DIST / APP_NAME / f"{APP_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"打包命令成功了，但没找到 {exe}")
    size = sum(f.stat().st_size for f in (DIST / APP_NAME).rglob("*") if f.is_file())
    print(f"\n产物：{exe}")
    print(f"整个目录 {size / 1024 / 1024:.1f} MB")
    return exe


def windows_of(pid: int) -> list[str]:
    """列出该进程拥有的顶层窗口的类名 —— 用来揪出控制台黑窗。"""
    import ctypes
    from ctypes import wintypes
    out: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        p = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
            out.append(buf.value)
        return True

    ctypes.windll.user32.EnumWindows(cb, 0)
    return out


def verify(exe: Path) -> int:
    print("\n--- 验证 ---")
    ok = True

    # 1) 桥脚本进包了没有
    internal = exe.parent / "_internal" / "bridge"
    if not internal.is_dir():
        internal = exe.parent / "bridge"
    if internal.is_dir():
        ps = sorted(p.name for p in internal.glob("*.ps1"))
        print(f"  OK  桥脚本已打进包：{ps}")
    else:
        print("  NG  包里找不到 bridge/ —— OneNote 通道会直接不可用")
        ok = False

    # 2) 真启动一次（隔离配置目录，别碰用户真实配置）
    env = dict(os.environ)
    env["APPDATA"] = tempfile.mkdtemp(prefix="execheck-")
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(9)
    if proc.poll() is not None:
        print(f"  NG  进程已经退出了（退出码 {proc.returncode}）—— 界面没起来")
        return 1
    print(f"  OK  进程活着（PID {proc.pid}）")

    # 3) 没有控制台黑窗
    classes = windows_of(proc.pid)
    consoles = [c for c in classes if c in ("ConsoleWindowClass", "PseudoConsoleWindow")]
    if consoles:
        print(f"  NG  检测到控制台窗口 {consoles} —— 双击会弹黑窗")
        ok = False
    else:
        print(f"  OK  没有控制台窗口（该进程的窗口类：{classes or '（还没建出来）'}）")

    proc.terminate()
    time.sleep(1.5)
    if proc.poll() is None:
        proc.kill()
    return 0 if ok else 1


def main() -> int:
    args = set(sys.argv[1:])
    py = find_python()
    exe = build(py, clean="--clean" in args)
    if "--no-verify" in args:
        return 0
    code = verify(exe)
    print("\n" + ("全部通过。" if code == 0 else "验证有问题，见上面的 NG。"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
