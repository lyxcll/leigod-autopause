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
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$PfxPath = $env:CODE_SIGN_PFX,
    [string]$PfxPassword = $env:CODE_SIGN_PFX_PASSWORD,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Path)) { throw "找不到要签名的文件：$Path" }

$tempPfx = $false
# CI 里用 base64 传证书，避免把 .pfx 提交进仓库
if (-not $PfxPath -and $env:CODE_SIGN_PFX_BASE64) {
    $PfxPath = Join-Path ([IO.Path]::GetTempPath()) "leigod-sign.pfx"
    [IO.File]::WriteAllBytes($PfxPath, [Convert]::FromBase64String($env:CODE_SIGN_PFX_BASE64))
    $tempPfx = $true
}

if (-not $PfxPath) {
    Write-Host "ℹ️  没配置签名证书（CODE_SIGN_PFX_BASE64 / -PfxPath），跳过签名。"
    Write-Host "   发布流程会生成 SHA256SUMS.txt，用户可据此校验下载。"
    exit 0
}
if (-not (Test-Path $PfxPath)) { throw "证书文件不存在：$PfxPath" }

$flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable -bor
         [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::PersistKeySet
try {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($PfxPath, $PfxPassword, $flags)
} catch {
    throw "读取 .pfx 失败（密码不对？）：$($_.Exception.Message)"
}
Write-Host ("🔏 签名证书：{0}  指纹 {1}" -f $cert.Subject, $cert.Thumbprint)

$sig = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert `
    -TimestampServer $TimestampUrl -HashAlgorithm SHA256
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

if ($tempPfx) { Remove-Item $PfxPath -Force -ErrorAction SilentlyContinue }
