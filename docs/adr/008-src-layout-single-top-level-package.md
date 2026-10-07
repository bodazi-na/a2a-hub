# ADR-008：src 布局 + 单一顶层包 `a2a_hub`

## 状态

已采纳（2026-10-07）

## 背景

`pyproject.toml` 里原本是：

```toml
py-modules = ["hub"]
packages   = ["core", "adapters", "probes", "mcp_facade"]
```

也就是装一次 `a2a-hub`，会往 site-packages 的**顶层**塞进五个名字。

其中 `core` 和 `adapters` 是**通用名** —— 任何项目都可能有一个叫 `core` 的包，
撞车是迟早的事。而撞车的表现很难查：`import core` 到底拿到谁的，取决于
`sys.path` 顺序，不会报错，只会行为诡异。

当时的处理方式是**在 README 里写明「建议装进独立虚拟环境」**。

## 决策

1. 代码移进 **`src/a2a_hub/`**，顶层只占 `a2a_hub` 一个名字
2. 子模块保持原名，收在包内：`a2a_hub.core` / `a2a_hub.adapters` /
   `a2a_hub.probes` / `a2a_hub.mcp_facade`
3. 入口从 `hub.py` 改为 `src/a2a_hub/cli.py`，控制台脚本
   `a2a-hub = "a2a_hub.cli:main"`
4. 仓库根保留一个 `hub.py`，**不进安装包**，只为让文档里已有的
   `python hub.py serve` 继续可用

## 理由

**为什么不能靠「建议装 venv」解决。** 那是把一个设计缺陷转嫁成用户的负担：
用户装个库还得先想想会不会撞名，撞了还要自己排查。**能被修掉的设计问题
不该写成使用注意事项。**

**为什么是 `a2a_hub` 而不是别的。** 它是发行名 `a2a-hub` 的自然 Python 化，
且足够具体 —— `a2a_hub` 撞车的概率比 `core` 低好几个数量级。

**为什么要 `src/` 布局。** 不只是整洁。没有 `src/` 时，在仓库根跑测试会
**优先 import 到工作目录里的包**，于是「测试通过」和「装出去能用」是两件事 ——
打包配置写错了测试也发现不了。加了 `src/` 之后这两者才对齐。

**为什么保留 `hub.py` 这个 shim。** 文档、脚本、别人的肌肉记忆里到处是
`python hub.py serve`。删掉它换来的「干净」不值当，而它**不在 packages 里**，
所以不带来命名空间问题。shim 的存在本身要写清楚，否则下一个人会以为它是入口。

## 后果

**好处**

- `pip install a2a-hub` 只多出 `a2a_hub` 一个名字
- 装完之后**从任何目录**都能用（`a2a-hub` / `python -m a2a_hub`），不再依赖 cwd
- 测试验证的一定是会被装出去的那份代码
- 可以用 `pip install -e .` 做开发安装，同时保留源码目录的完整结构

**代价**

- **可导入路径是破坏性变更**：`from core.store import Store` →
  `from a2a_hub.core.store import Store`。只有当过库用的人需要改
- 仓库根多一个「只给源码用户用」的 `hub.py`，需要在文档里解释它是什么
- MCP 接入配置要改：`-m mcp_facade` → `-m a2a_hub.mcp_facade`，
  且 `PYTHONPATH` 要指向 `<仓库根>/src`

**验证方式（实测，不是推断）**

- 构建 wheel，检查顶层条目 → 只有 `a2a_hub`
- 临时 venv 真实 `pip install` → site-packages 顶层只有 `a2a_hub` + 第三方依赖
- 三个入口全通，包括**从无关目录** import
- 打包 exe 照常构建启动；测试 13/13（含集成 19/19）

## 相关

- ADR-002（薄核心 + 厚适配器）：分层方向不变，只是换了物理位置
- `docs/architecture.md` 的「目录分层」一节
