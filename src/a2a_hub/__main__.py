# -*- coding: utf-8 -*-
"""`python -m a2a_hub` 入口 —— 等价于 `a2a-hub` 命令。

比 `python hub.py` 更可靠：不依赖仓库根目录里那个 shim，
装完包之后在任何目录都能跑。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
