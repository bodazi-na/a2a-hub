# DSH 沙箱 ACL 问题：诊断与处置

> 状态：**根因已定位并实测确认 —— 不是 ACL 问题，是令牌问题。**
> 记录时间：2026-10-06 13:45

## 症状

在 WorkBuddy 会话里调用 DSH 时，任何**需要执行命令**的任务都失败：

```
Error: SetNamedSecurityInfoW failed (Win32 5): grantWrite(<工作区>)
```

**特征**：纯对话**完全正常**（`dsh --profile headless "say hi"` 有正常回复）。
「能聊天但不能跑命令」是这个问题的典型表现 —— 不要误判成 DSH 坏了。

## 根因：令牌降权，不是 ACL

第一版判断（"ACL 缺 WRITE_DAC"）**只对了一半**。实测后修正：

```
$ powershell -File tools\fix-sandbox-acl.ps1 -DryRun
当前用户 : <机器名>\<用户名>
所有者   : <机器名>\<用户名>
受保护   : False

有效权限 : FullControl
含 ChangePermissions : True     ← FullControl 已隐含
含 TakeOwnership     : True
```

**ACL 完全够用**。真正的瓶颈在**进程令牌**：

| 层面 | 状态 |
| --- | --- |
| **ACL**（目录上写了什么） | ✅ 当前用户是所有者 + FullControl，含 ChangePermissions / TakeOwnership |
| **令牌**（进程实际拿到什么） | ❌ 从 WorkBuddy 沙箱派生的受限令牌里，`WRITE_DAC` 位不可用 |

**令牌决定有效访问权** —— ACL 给再多，受限令牌下也用不上。

DSH 自己的诊断也得出同一结论（`D:\A2A_Engineering-acl-reports\` 里的 JSONL）：

```json
{"operation":"classify","status":"PRECONDITION",
 "reason":"The caller cannot open the requested object with both WRITE_DAC and WRITE_OWNER."}
{"operation":"caller",
 "reason":"The caller token determines effective access; unconfined execution does not imply elevation."}
```

## 为什么 DSH 自己修不了

**修复动作本身就需要 `WRITE_DAC`** —— 它拿不到，所以在沙箱里无法自我修复。
不是「它不会做」，是「它够不着」：典型的「需要提权才能修好提权问题」。

## 处置

### 方案 A（推荐）：在普通终端跑 DSH

**ACL 不用动。** 只要 DSH 不是从别的沙箱会话里派生出来的，它自己的沙箱配置就能成功。

```powershell
# 普通终端（不是 WorkBuddy / 任何沙箱会话）
dsh --profile headless "say hi"                    # 纯对话，本来就通
dsh --profile headless "运行命令并原样返回: echo ok"  # 关键：不再报 SetNamedSecurityInfoW
```

跑通后，`~/.dsh/skills/a2a-hub/SKILL.md` 那份 skill 即可生效，DSH 就能指挥 hub。

### 方案 B：诊断（万一 ACL 真的缺）

```powershell
# 只诊断，不改动
powershell -NoProfile -ExecutionPolicy Bypass -File `
  D:\A2A_Engineering\a2a-hub\tools\fix-sandbox-acl.ps1 -DryRun

# ACL 确实缺权限时才去掉 -DryRun 执行修复（会先备份 SDDL 并给回滚命令）
```

脚本的**第一职责是诊断**：ACL 够就直说「不用改」，不够才补
`ChangePermissions + TakeOwnership`（注意 PowerShell 枚举里**没有** `WriteDAC` / `WriteOwner`
这两个名字，写错会报「无法将标识符名称与有效的枚举器名称相匹配」）。

### 方案 C：先不动

DSH 作为**执行体**（被 hub 调用）**完全可用** —— CLI 适配器跑在普通进程里，不受这个问题影响。
受影响的只有「DSH 主动去执行 shell 命令」这条路。近期没这个需求就可以不处理。

## 已备好的东西

| 文件 | 作用 |
| --- | --- |
| `tools/fix-sandbox-acl.ps1` | **先诊断**：ACL 够就说不用改；不够才补权限（带备份与回滚） |
| `~/.dsh/skills/a2a-hub/SKILL.md` | DSH 侧的 hub skill（文首写明本症状与处置：别重试，直接报告用户） |
| `D:\A2A_Engineering-acl-reports\` | DSH 自己产出的诊断报告（JSONL）与 ACL 备份 |

> ⚠️ 脚本必须以 **UTF-8 with BOM** 保存。PowerShell 5.1 在没有 BOM 时按 ANSI(GBK) 解码，
> 含中文注释的脚本会报「无法加载文件 …」的语法错误 —— 这个坑在准备脚本时踩过一次。

## 一句话总结

**这不是 DSH 的能力问题，也不是目录权限问题，是令牌问题。**
在普通终端里跑一次 DSH 即可验证；ACL 不需要任何改动。
