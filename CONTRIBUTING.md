# 贡献指南

## 环境要求

| 项 | 要求 |
| --- | --- |
| Python | **3.10+**（CI 跑 3.10 与 3.13） |
| 操作系统 | **Windows**。接口是平台无关的，但子进程管理（Job Object、`cmd.exe` 包裹、`taskkill` 兜底）只在 Windows 上验证过 |
| 依赖 | 只要 starlette / uvicorn / httpx；SQLite 走标准库 |

```bash
git clone <repo> && cd a2a-hub
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,mcp]"      # mcp 是可选的，只在改 facade 时需要
```

先跑一遍快速开始（见 README），确认环境是通的 —— 它不需要任何真实 AI CLI。

## 三档测试

```bash
python tests/run_all.py          # 单元级 + 平台级（CI 跑这个）
python tests/run_all.py --all    # 再加集成级
python tests/run_all.py --list   # 只看分组
```

| 档 | 验证什么 | 依赖 | CI |
| --- | --- | --- | --- |
| **单元级** | core 的逻辑（store / router / orchestrator / hub_app） | 内存 Store + 假适配器，**零外部依赖** | ✅ |
| **平台级** | Windows 独有机制（Job Object 收进程树） | 会起真实进程，但只用 `sys.executable`；非 Windows 自动跳过 | ✅（windows-latest） |
| **集成级** | 适配器与本机工具的对接 | 真实 CLI / Windows 进程命令 / hub 在跑 | ❌ 仅开发机 |

**这条分界线就是架构边界**：

```
单元级全绿  ⟺  core 确实与平台无关
平台级 + 集成级  ⟺  适配器与本机工具的对接
```

所以 CI 同时是**架构约束的自动验证器**，而不只是防回归。

### 写测试的几条约定

- **断言别写「或」**。`state in {A, B}` 这种宽松断言正是 bug 溜过去的通道 ——
  历史上就有一条测试因为写了 `in {"CANCELED", "COMPLETED"}`，
  让一个状态回退的 bug 潜伏了很久
- **优先结构性断言，计时只做兜底**。计时测试在 CI 上不可靠（本机实测过同一段
  代码全量跑 170ms、单独跑 2.8ms）。结构性断言不受机器快慢影响
- **要测「不该发生的事」**。比如「已取消的任务不该被派往下游」、
  「超长行的尾巴不该被当成新行」—— 这些比正向路径更容易漏
- **能确定性复现就别靠并发碰运气**。例：撕裂读的复现方法是
  「开第二个连接，在读序列中间插一笔写」，而不是「起两个线程跑很多次」

## 写一个新适配器

`core` 一行都不用改。三步：

**① 加一个 `Adapter` 子类。** 下游是 CLI 就继承 `CLIAdapter`，
实现三个方法：

```python
class MyToolCLI(CLIAdapter):
    def build_argv(self, session_id): ...   # 拼参数（prompt 不进 argv，走 stdin）
    def parse_line(self, obj, state): ...   # 一行 JSON → 事件列表
    def finalize(self, state, returncode): ...  # 收尾：文本 / session / usage
```

下游是 HTTP 就继承 `Adapter`，实现 `call()` 与 `probe()`。

**② 在 `adapters/__init__.py` 导出它。**

**③ 写一份配置样例到 `examples/agents/`**（**用占位符，不要写本机真实路径**）。

动手前**务必读** [`docs/architecture.md`](docs/architecture.md#写-cli-适配器必踩的坑全部实测过)
的五个坑 —— 每一个都实测踩过，重踩一遍是纯粹的浪费。

## 代码约定

**注释写「为什么」，不写「是什么」。** 代码本身能说清「是什么」；
注释的价值在于记下「为什么不能那样写」。反例与正例：

```python
# 差：重复了代码
# 这里把 timeout 设为 600

# 好：记下了不明显的约束
# 未显式指定时用适配器自己的配置值 —— 不能替调用方填死一个数，
# 否则 agents/*.json 里配的 timeout 永远不生效（A2A-13）
```

**引用问题编号。** 项目里的注释常带 `（A2A-07）`、`（M2）`、`（P1-8）` 这类标记，
指向 [`docs/drill-2026-10-06-real-load.md`](docs/drill-2026-10-06-real-load.md)
里的具体条目。**改代码前先看那个编号讲了什么** —— 那些都是踩过的坑。

**改数据库 schema 必须同时加迁移。** `SCHEMA_VERSION` 加一，
在 `_migrate()` 里用 `PRAGMA table_info` 判断列是否存在再 `ALTER TABLE`。
索引要等列补好之后再建（老库上 `executescript` 阶段还没有那些列）。

**改架构决策请写 ADR。** 见 [`docs/adr/README.md`](docs/adr/README.md)。
尤其是**否决某个方案时，把否决理由写下来** —— 否则下一个人会重新提一遍。

## 提交

Commit message 用中文，格式：

```
<类型>: <一句话说明>

<为什么这么改 —— 问题是什么、根因是什么、为什么选这个方案>
<如果修的是清单里的条目，带上编号>
```

类型：`fix` / `feat` / `perf` / `refactor` / `docs` / `test` / `chore`。

**提交前必须做的**：

```bash
python tests/run_all.py          # 单元 + 平台，必须全绿
git status --short               # 确认没有把 data/ workspace/ agents/ 带进来
```

**修 bug 必须带回归测试。** 一个能复现原问题的测试，比一句「已修复」有价值得多 ——
它同时锁住了「这个问题不会再回来」。

## 不要提交的东西

`.gitignore` 已经排除了 `data/` `workspace/` `agents/` `__pycache__/`，
以及凭据文件（`.env`、`*.pat`）。

**注意**：这些目录模式在 `.gitignore` 里**必须写成锚定形式**（`/agents/`
而不是 `agents/`）。写 `agents/` 会匹配任意层级下名叫 `agents` 的目录，
把 `examples/agents/` 一起忽略掉 —— 陌生人 clone 下来会一个示例都拿不到。
