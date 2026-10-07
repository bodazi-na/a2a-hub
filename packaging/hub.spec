# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 把 hub 打成免安装的 Windows exe。

为什么需要显式列 hiddenimports
------------------------------
uvicorn 的 loop / protocol 实现是**字符串动态导入**的：`uvicorn.loops.auto`
会在运行时去 import `uvicorn.loops.asyncio`，`uvicorn.protocols.http.auto` 去
import `uvicorn.protocols.http.h11_impl`。PyInstaller 的静态分析**看不到**这些。

漏掉的后果很坑：打包能成功、exe 能启动、然后在**真正开始服务时**才炸，报
`ModuleNotFoundError: No module named 'uvicorn.loops.asyncio'` —— 这个信息和
「打包」这件事看不出任何关系。

所以这里用 `collect_submodules("uvicorn")` **一次收全**，而不是逐个列举 ——
逐个列就是在赌自己没漏，而漏了只有运行时才知道。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# 注意：spec 是被 PyInstaller `exec` 执行的，**没有 `__file__`**。
# 它注入的是 `SPECPATH`（本 spec 所在目录）。
HERE = SPECPATH                        # noqa: F821  （由 PyInstaller 注入）
ROOT = os.path.dirname(HERE)           # 仓库根

hidden = collect_submodules("uvicorn") + [
    # anyio 的后端也是运行时按名字选的
    "anyio._backends._asyncio",
    # starlette 内部有少量延迟导入
    "starlette.middleware",
    "starlette.middleware.base",
]

a = Analysis(
    [os.path.join(ROOT, "hub.py")],
    pathex=[ROOT],                    # 让 core / adapters / probes 能被解析到
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除用不到的重包：能把体积砍下来一大截，也让构建更快
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest",
        "numpy", "pandas", "matplotlib", "PIL",
        "PyQt5", "PySide2", "IPython", "jupyter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# 形态由环境变量切：默认 onedir（目录版）。
# 目录版是**推荐形态** —— 启动快、依赖收集问题好排查；
# 单文件版更方便拷走，但每次启动都要把 27MB 解压到临时目录。
ONEFILE = os.environ.get("A2A_HUB_ONEFILE") == "1"

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="a2a-hub",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,                    # 不压缩：UPX 极易被杀软误报
        console=True,                 # 它是个 CLI，必须有控制台
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,        # onedir：二进制放到 _internal/
        name="a2a-hub",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="a2a-hub",
    )
