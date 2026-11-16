# 可选：给 LeigodAutoPause.exe 做 Authenticode 签名。
#
# 为什么是“可选”：
#   微软 SmartScreen 只认「正规 CA 签发 + 有下载信誉」的证书，自签名证书签了也照样弹
#   「已保护你的电脑」。所以本项目默认发未签名 exe，改为提供 SHA256 让用户自行校验。
#   如果你自己买了代码签名证书（OV/EV），或者通过 SignPath 拿到了开源项目签名，
#   把证书丢给 CI 跑这个脚本即可，其余流程不用改。
#
# 实现只用 PowerShell 自带的 Set-AuthenticodeSignature，不需要 Windows SDK / signtool。
#
# CI 用法（配在仓库 Secrets，不落盘）：
#   CODE_SIGN_PFX_BASE64   .pfx 的 base64：[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))
#   CODE_SIGN_PFX_PASSWORD .pfx 密码
#   不配这两个 Secret，脚本直接跳过，构建照常出未签名 exe。
#
# 本地用法：
#   pwsh scripts/sign_exe.ps1 -Path dist\LeigodAutoPause.exe -PfxPath D:\cert.pfx -PfxPassword '***'
#
# 安全约定（审计后加）：
#   - 私钥用 EphemeralKeySet 加载，不写进用户证书库、也不允许再次导出；
#   - CI 解码出来的临时 .pfx 放在 try/finally 里，成功失败都会被删除；
#   - 时间戳服务默认走 https。
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$PfxPath = $env:CODE_SIGN_PFX,
    [string]$PfxPassword = $env:CODE_SIGN_PFX_PASSWORD,
    [string]$TimestampUrl = "https://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Path)) { throw "找不到要签名的文件：$Path" }

$tempPfx = $false
if (-not $PfxPath -and $env:CODE_SIGN_PFX_BASE64) {
    # CI 里用 base64 传证书，避免把 .pfx 提交进仓库
    $PfxPath = Join-Path ([IO.Path]::GetTempPath()) ("leigod-sign-{0}.pfx" -f [guid]::NewGuid().ToString("N"))
    [IO.File]::WriteAllBytes($PfxPath, [Convert]::FromBase64String($env:CODE_SIGN_PFX_BASE64))
    $tempPfx = $true
    # 临时证书文件只留给自己
    try {
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $PfxPath /inheritance:r /grant:r "${me}:F" | Out-Null
    } catch {
        Write-Host "⚠️  收紧临时证书 ACL 失败（继续，但请注意 %TEMP% 的可读范围）"
    }
}

if (-not $PfxPath) {
    Write-Host "ℹ️  没配置签名证书（CODE_SIGN_PFX_BASE64 / -PfxPath），跳过签名。"
    Write-Host "   发布流程会生成 SHA256SUMS.txt，用户可据此校验下载。"
    exit 0
}

$cert = $null
try {
    if (-not (Test-Path $PfxPath)) { throw "证书文件不存在：$PfxPath" }

    # EphemeralKeySet：私钥只在内存里，不落证书库，也不能被再次导出
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
    try {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($PfxPath, $PfxPassword, $flags)
    } catch {
        throw "读取 .pfx 失败（密码不对？）：$($_.Exception.Message)"
    }
    Write-Host ("🔏 签名证书：{0}  指纹 {1}" -f $cert.Subject, $cert.Thumbprint)

    $sig = $null
    try {
        $sig = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert `
            -TimestampServer $TimestampUrl -HashAlgorithm SHA256
    } catch {
        Write-Host "⚠️  https 时间戳失败（$($_.Exception.Message)），改试不带时间戳签名"
        $sig = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -HashAlgorithm SHA256
    }
    if ($sig.Status -ne 'Valid' -and $sig.Status -ne 'UnknownError') {
        throw "签名失败：$($sig.Status) $($sig.StatusMessage)"
    }

    $check = Get-AuthenticodeSignature -FilePath $Path
    if ($check.SignerCertificate -and $check.SignerCertificate.Thumbprint -eq $cert.Thumbprint) {
        if ($check.Status -eq 'Valid') {
            Write-Host "✅ 已签名且证书链可信（SmartScreen 会逐步积累信誉）"
        } else {
            Write-Host "⚠️  已签名，但证书链不受信任（$($check.Status)）。自签名证书属正常现象，不会改善 SmartScreen 提示。"
        }
    } else {
        throw "签名后校验失败：没读到签名证书"
    }
} finally {
    if ($cert) { $cert.Dispose() }
    if ($tempPfx -and (Test-Path $PfxPath)) {
        Remove-Item $PfxPath -Force -ErrorAction SilentlyContinue
        Write-Host "🧹 已删除临时证书文件"
    }
}
