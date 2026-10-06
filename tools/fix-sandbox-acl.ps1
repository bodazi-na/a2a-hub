# 诊断（并在必要时修复）DSH 沙箱的 ACL 前提
#
# 背景
# ----
# DSH 启动子进程前要给工作区配置沙箱授权项（SetNamedSecurityInfoW），
# 这要求调用者对目标目录持有「修改权限 + 取得所有权」——
# 在 ACL 层面即 FileSystemRights 的 ChangePermissions + TakeOwnership
# （注意：不是 WriteDAC / WriteOwner，PowerShell 枚举里没有这两个名字）。
#
# 实测本机 D:\A2A_Engineering\a2a-hub：
#   ACL 层面 —— 当前用户是所有者，且已有 FullControl，**不缺权限**。
#   令牌层面 —— 在受限令牌（如从别的沙箱会话里派生）下，WRITE_DAC 位不可用，
#               于是仍然报 SetNamedSecurityInfoW failed (Win32 5)。
#
# 所以本脚本的第一职责是**先诊断**：
#   - ACL 已够  → 直接告诉你「不用改，去普通终端跑 DSH」
#   - ACL 不够  → 备份后补上 ChangePermissions + TakeOwnership，并给回滚命令
#
# 用法
# ----
# 在**普通终端**（不是任何沙箱会话）里运行：
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File fix-sandbox-acl.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File fix-sandbox-acl.ps1 -Path 'D:\other'
#   powershell -NoProfile -ExecutionPolicy Bypass -File fix-sandbox-acl.ps1 -DryRun

[CmdletBinding()]
param(
    [string]$Path = 'D:\A2A_Engineering\a2a-hub',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Write-Host "== 目标 ==" -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $Path)) {
    Write-Host "  目录不存在: $Path" -ForegroundColor Red
    exit 1
}
Write-Host "  $Path"

$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = Get-Acl -LiteralPath $Path
Write-Host "  当前用户 : $me"
Write-Host "  所有者   : $($acl.Owner)"
Write-Host "  受保护   : $($acl.AreAccessRulesProtected)"

# ---- 计算当前用户对该目录的有效权限 ----
$rights = [System.Security.AccessControl.FileSystemRights]0
foreach ($ace in $acl.Access) {
    if ($ace.AccessControlType -ne 'Allow') { continue }
    $id = $ace.IdentityReference.Value
    $isMe = ($id -eq $me) -or ($id -eq "$env:USERDOMAIN\$env:USERNAME") -or ($id -like "*\$env:USERNAME")
    if ($isMe) { $rights = $rights -bor $ace.FileSystemRights }
}

# 关键位：ChangePermissions(0x40000) / TakeOwnership(0x80000)
$hasChangePerm = [bool]($rights -band [System.Security.AccessControl.FileSystemRights]::ChangePermissions)
$hasTakeOwner  = [bool]($rights -band [System.Security.AccessControl.FileSystemRights]::TakeOwnership)

Write-Host ""
Write-Host "== 有效权限 ==" -ForegroundColor Cyan
Write-Host "  $rights"
Write-Host "  含 ChangePermissions : $hasChangePerm"
Write-Host "  含 TakeOwnership     : $hasTakeOwner"

if ($hasChangePerm -and $hasTakeOwner) {
    Write-Host ""
    Write-Host "== 结论：ACL 已足够，不需要改动 ==" -ForegroundColor Green
    Write-Host ""
    Write-Host "  目录权限没问题。DSH 仍报 SetNamedSecurityInfoW failed 的话，" -ForegroundColor Yellow
    Write-Host "  原因是**当前进程的令牌被降权**（例如从别的沙箱会话里派生），" -ForegroundColor Yellow
    Write-Host "  此时 ACL 给再多也用不上 —— 令牌决定有效访问权。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  处置：在普通终端里直接跑 DSH 验证 ——" -ForegroundColor Green
    Write-Host "    dsh --profile headless `"say hi`""
    Write-Host "    dsh --profile headless `"运行命令并原样返回: echo ok`""
    exit 0
}

Write-Host ""
Write-Host "== ACL 缺权限，准备补上 ==" -ForegroundColor Cyan
Write-Host "  将要添加: [Allow] $me : ChangePermissions, TakeOwnership (可继承)"

if ($DryRun) {
    Write-Host ""
    Write-Host "  (-DryRun 已指定，未做任何改动)" -ForegroundColor Yellow
    exit 0
}

# 备份原 SDDL
$backupDir = Join-Path (Split-Path -Parent $Path) 'acl-recovery'
if (-not (Test-Path -LiteralPath $backupDir)) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupFile = Join-Path $backupDir "acl-before-$stamp.txt"
$acl.Sddl | Set-Content -LiteralPath $backupFile -Encoding UTF8
Write-Host "  已备份原 SDDL: $backupFile" -ForegroundColor DarkGray

$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $me,
    [System.Security.AccessControl.FileSystemRights]'ChangePermissions, TakeOwnership',
    [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $Path -AclObject $acl

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
$after = Get-Acl -LiteralPath $Path
foreach ($ace in $after.Access) {
    if ($ace.IdentityReference.Value -like "*$env:USERNAME*") {
        Write-Host "  [Allow] $($ace.IdentityReference) : $($ace.FileSystemRights)"
    }
}
Write-Host ""
Write-Host "回滚："
Write-Host "  `$a = Get-Acl '$Path'"
Write-Host "  `$a.SetSecurityDescriptorSddlForm((Get-Content '$backupFile' -Raw).Trim())"
Write-Host "  Set-Acl -Path '$Path' -AclObject `$a"
