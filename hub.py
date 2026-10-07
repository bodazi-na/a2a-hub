#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""仓库根的开发入口 —— **不随包安装**，只是让 `python hub.py ...` 继续可用。

为什么要有它：文档里到处是 `python hub.py serve`，而真正的入口已经收进
`src/a2a_hub/cli.py`。直接删掉的话，所有示例和别人的肌肉记忆都会失效；
而这个文件**不在 `pyproject.toml` 的 packages 里**，所以它**不会污染
site-packages 的顶层命名空间** —— 这正是这次重构要解决的问题。

安装过包之后更推荐直接用 `a2a-hub ...` 或 `python -m a2a_hub ...`：
那两条路不依赖你在哪个目录。
"""

import sys
from pathlib import Path

# 源码仓库里 a2a_hub 在 src/ 下，没装包时要把 src/ 加进来才 import 得到。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from a2a_hub.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
