#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = $(if ($env:ICA_ROUTER_HOME) { $env:ICA_ROUTER_HOME } else { Join-Path $env:LOCALAPPDATA "IcaLiteLLMKeyRouter" }),
    [string]$SourceDirectory = $env:ICA_ROUTER_SOURCE_DIR,
    [string]$KeyRotatorPath = $env:ICA_ROUTER_KEY_ROTATOR,
    [switch]$NonInteractive,
    [switch]$PiModels,
    [switch]$PrimeModels,
    [string[]]$ModelsJson = @(),
    [switch]$ReplaceKeys,
    [switch]$ForceInstall
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$AppName = "ica-litellm-key-router"
$RepoSlug = "sehoon123/ica-litellm-key-router"
$SourceRef = if ($env:ICA_ROUTER_REF) { $env:ICA_ROUTER_REF } else { "v0.2.1" }
$LiteLLMVersion = "1.98.0"
$PythonVersion = "3.12.13"
$UvVersion = "0.12.2"
$UvInstallerSha256 = "ed83c6ca35c40979ccbab45d4877f47dd54243d163b8af3729fe281b8bd43d46"
$StateDir = Join-Path $InstallRoot "state"
$ReleasesDir = Join-Path $InstallRoot "releases"
$CurrentFile = Join-Path $InstallRoot "current"
$ToolsDir = Join-Path $InstallRoot ("tools\uv-" + $UvVersion)
$UvBin = Join-Path $ToolsDir "uv.exe"
$TempSource = $null
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

if ($SourceRef -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') {
    throw "ICA_ROUTER_REF must be an exact vMAJOR.MINOR.PATCH tag"
}

function Write-Utf8File([string]$LiteralPath, [string]$Text) {
    [System.IO.File]::WriteAllText($LiteralPath, $Text, $script:Utf8NoBom)
}

function Replace-FileAtomic([string]$TemporaryPath, [string]$TargetPath) {
    $temporaryDirectory = [System.IO.Path]::GetFullPath([System.IO.Path]::GetDirectoryName($TemporaryPath))
    $targetDirectory = [System.IO.Path]::GetFullPath([System.IO.Path]::GetDirectoryName($TargetPath))
    if ($temporaryDirectory -ne $targetDirectory) { throw "atomic replacement requires the same directory" }
    if (Test-Path -LiteralPath $TargetPath) {
        Assert-NotReparse $TargetPath 'replacement target'
        [System.IO.File]::Replace($TemporaryPath, $TargetPath, $null)
    } else {
        [System.IO.File]::Move($TemporaryPath, $TargetPath)
    }
}

function Assert-NotReparse([string]$LiteralPath, [string]$Label) {
    if (Test-Path -LiteralPath $LiteralPath) {
        $item = Get-Item -Force -LiteralPath $LiteralPath
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label must not be a reparse point: $LiteralPath"
        }
    }
}

function Set-PrivateAcl([string]$LiteralPath) {
    if (-not (Test-Path -LiteralPath $LiteralPath)) { return }
    Assert-NotReparse $LiteralPath "private path"
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $item = Get-Item -Force -LiteralPath $LiteralPath
    if ($item.PSIsContainer) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($sid)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
        [System.IO.Directory]::SetAccessControl($LiteralPath, $acl)
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($sid)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
        [System.IO.File]::SetAccessControl($LiteralPath, $acl)
    }
    $check = Get-Acl -LiteralPath $LiteralPath
    $ownerSid = ([System.Security.Principal.NTAccount]$check.Owner).Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -ne $sid.Value) { throw "private ACL owner is not the current user: $LiteralPath" }
    $allowed = @($check.Access | Where-Object { $_.AccessControlType -eq 'Allow' })
    if ($allowed.Count -eq 0) { throw "private ACL has no current-user allow rule: $LiteralPath" }
    foreach ($entry in $allowed) {
        $entrySid = $entry.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        if ($entrySid -ne $sid.Value) { throw "private ACL still allows another principal: $LiteralPath" }
    }
}

function Protect-PrivateTree([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $items = @(Get-ChildItem -Force -Recurse -LiteralPath $Root | Sort-Object { $_.FullName.Length } -Descending)
    foreach ($item in $items) { Set-PrivateAcl $item.FullName }
    Set-PrivateAcl $Root
}

function Download-Https([string]$Uri, [string]$OutFile) {
    $curlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (-not (Test-Path -LiteralPath $curlPath)) { throw "System32 curl.exe is required" }
    & $curlPath --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
        --connect-timeout 15 --max-time 300 --max-redirs 5 --retry 3 --output $OutFile $Uri
    if ($LASTEXITCODE -ne 0) { throw "download failed: $Uri" }
}

function Invoke-Uv([string[]]$UvArgs) {
    $saved = @{}
    $projectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
    $names = @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(UV_|PIP_|PYTHON)' } | ForEach-Object { $_.Name })
    try {
        foreach ($name in $names) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
        $env:UV_PYTHON_PREFERENCE = 'only-managed'
        $env:UV_DEFAULT_INDEX = 'https://pypi.org/simple'
        $env:UV_CACHE_DIR = Join-Path $script:InstallRoot 'cache\uv'
        if ($projectEnvironment) { $env:UV_PROJECT_ENVIRONMENT = $projectEnvironment }
        & $script:UvBin --no-config @UvArgs
        if ($LASTEXITCODE -ne 0) { throw "uv command failed: $($UvArgs -join ' ')" }
    }
    finally {
        $currentNames = @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(UV_|PIP_|PYTHON)' } | ForEach-Object { $_.Name })
        foreach ($name in $currentNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    }
}

function Test-ExactUvVersion {
    if (-not (Test-Path -LiteralPath $script:UvBin -PathType Leaf)) { return $false }
    Assert-NotReparse $script:UvBin
    $versionText = ((& $script:UvBin --version 2>$null) -join '').Trim()
    if ($LASTEXITCODE -ne 0) { return $false }
    $parts = $versionText -split '\s+'
    return ($parts.Count -ge 2 -and $parts[0] -eq 'uv' -and $parts[1] -eq $script:UvVersion)
}

function Install-VerifiedUv {
    if (Test-ExactUvVersion) { return }
    if (Test-Path -LiteralPath $script:UvBin) { Assert-NotReparse $script:UvBin }
    $installer = Join-Path $script:InstallRoot (".uv-install-" + $script:UvVersion + ".ps1")
    Write-Host "Installing verified uv $script:UvVersion ..."
    Download-Https "https://astral.sh/uv/$script:UvVersion/install.ps1" $installer
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    if ($actual -ne $script:UvInstallerSha256) { throw "uv installer SHA-256 mismatch" }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $psi.UseShellExecute = $false
        $installerCommand = '& $env:ICA_VERIFIED_UV_INSTALLER; exit $LASTEXITCODE'
        $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($installerCommand))
        $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $encodedCommand
        $psi.EnvironmentVariables.Clear()
        $psi.EnvironmentVariables['SystemRoot'] = $env:SystemRoot
        $psi.EnvironmentVariables['WINDIR'] = $env:WINDIR
        $psi.EnvironmentVariables['PATH'] = (Join-Path $env:SystemRoot 'System32')
        $psi.EnvironmentVariables['TEMP'] = [System.IO.Path]::GetTempPath()
        $psi.EnvironmentVariables['TMP'] = [System.IO.Path]::GetTempPath()
        $psi.EnvironmentVariables['USERPROFILE'] = $env:USERPROFILE
        $psi.EnvironmentVariables['ICA_VERIFIED_UV_INSTALLER'] = $installer
        $psi.EnvironmentVariables['UV_UNMANAGED_INSTALL'] = $script:ToolsDir
        $psi.EnvironmentVariables['UV_NO_MODIFY_PATH'] = '1'
        $psi.EnvironmentVariables['UV_DISABLE_UPDATE'] = '1'
        $process = [System.Diagnostics.Process]::Start($psi)
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "verified uv installer failed" }
    }
    finally { Remove-Item -Force -LiteralPath $installer -ErrorAction SilentlyContinue }
    if (-not (Test-Path -LiteralPath $script:UvBin -PathType Leaf)) { throw "uv.exe was not installed" }
    Assert-NotReparse $script:UvBin
    if (-not (Test-ExactUvVersion)) { throw "uv version mismatch" }
}

function Expand-SafeReleaseZip([string]$Archive, [string]$Destination, [string]$ExpectedTop) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $Destination) { throw "extraction destination already exists" }
    [void][System.IO.Directory]::CreateDirectory($Destination)
    Set-PrivateAcl $Destination
    $rootFull = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    [long]$total = 0
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($zip.Entries.Count -eq 0 -or $zip.Entries.Count -gt 2000) { throw "unsafe ZIP member count" }
        foreach ($entry in $zip.Entries) {
            $raw = $entry.FullName
            if ([string]::IsNullOrEmpty($raw) -or $raw.Contains('\') -or $raw.IndexOf([char]0) -ge 0) { throw "unsafe ZIP member name" }
            $parts = $raw.Split('/')
            if ($parts.Count -eq 0 -or $parts[0] -ne $ExpectedTop) { throw "ZIP has an unexpected top-level directory" }
            for ($i = 0; $i -lt $parts.Count; $i++) {
                $part = $parts[$i]
                if ($part -eq '' -and $i -eq $parts.Count - 1 -and $raw.EndsWith('/')) { continue }
                if ($part -eq '' -or $part -eq '.' -or $part -eq '..') { throw "ZIP traversal member" }
                if ($part -match '[<>:"|?*\x00-\x1F]' -or $part -match '[ .]$') { throw "ZIP member is not a safe Windows name" }
                $stem = ($part.Split('.')[0]).ToUpperInvariant()
                if ($stem -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') { throw "ZIP member uses a reserved Windows name" }
            }
            if (-not $seen.Add($raw.Normalize([Text.NormalizationForm]::FormC))) { throw "duplicate or case-colliding ZIP member" }
            $unixType = (($entry.ExternalAttributes -shr 16) -band 0xF000)
            $windowsAttributes = ($entry.ExternalAttributes -band 0xFFFF)
            if ($unixType -eq 0xA000 -or ($windowsAttributes -band [int][System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "ZIP links are forbidden"
            }
            $total += $entry.Length
            if ($total -gt 134217728) { throw "ZIP expanded size is too large" }
            $target = [System.IO.Path]::GetFullPath((Join-Path $Destination ($parts -join [System.IO.Path]::DirectorySeparatorChar)))
            if (-not $target.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) { throw "ZIP member escaped destination" }
        }
        foreach ($entry in $zip.Entries) {
            $parts = $entry.FullName.Split('/')
            $target = [System.IO.Path]::GetFullPath((Join-Path $Destination ($parts -join [System.IO.Path]::DirectorySeparatorChar)))
            if ($entry.FullName.EndsWith('/')) {
                [void][System.IO.Directory]::CreateDirectory($target)
            } else {
                [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target))
                $inputStream = $entry.Open()
                $outputStream = [System.IO.FileStream]::new($target, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                try {
                    $buffer = [byte[]]::new(81920)
                    [long]$copied = 0
                    while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $copied += $count
                        if ($copied -gt $entry.Length -or $copied -gt 134217728) { throw "ZIP member exceeded declared size" }
                        $outputStream.Write($buffer, 0, $count)
                    }
                    if ($copied -ne $entry.Length) { throw "ZIP member size mismatch" }
                }
                finally { $outputStream.Dispose(); $inputStream.Dispose() }
            }
        }
    }
    finally { $zip.Dispose() }
    Protect-PrivateTree $Destination
}

Assert-NotReparse $InstallRoot "install root"
[void][System.IO.Directory]::CreateDirectory($InstallRoot)
Set-PrivateAcl $InstallRoot
$installLockPath = Join-Path $InstallRoot 'install.lock'
try {
    $script:InstallLockStream = [System.IO.FileStream]::new($installLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch [System.IO.IOException] {
    throw "another installer is running"
}
Set-PrivateAcl $installLockPath
foreach ($directory in @($StateDir, $ReleasesDir, $ToolsDir, (Join-Path $InstallRoot 'cache'))) {
    Assert-NotReparse $directory "private directory"
    [void][System.IO.Directory]::CreateDirectory($directory)
    Set-PrivateAcl $directory
}

$ClientPaths = [System.Collections.Generic.List[string]]::new()
if ($PiModels) { $ClientPaths.Add((Join-Path $HOME '.pi\agent\models.json')) }
if ($PrimeModels) { $ClientPaths.Add((Join-Path $HOME '.prime\agent\models.json')) }
foreach ($requestedPath in $ModelsJson) {
    if ([string]::IsNullOrWhiteSpace($requestedPath)) { throw "ModelsJson paths must not be empty" }
    if ($requestedPath.Contains("`r") -or $requestedPath.Contains("`n")) {
        throw "ModelsJson paths must not contain control characters"
    }
    $ClientPaths.Add([System.IO.Path]::GetFullPath($requestedPath))
}
$ClientPaths = @($ClientPaths | Select-Object -Unique)

function Invoke-InstalledRouter([string]$Wrapper, [string[]]$Arguments) {
    $systemPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $systemPowerShell -NoProfile -ExecutionPolicy Bypass -File $Wrapper @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "ica-router command failed ($LASTEXITCODE): $($Arguments -join ' ')"
    }
}

$ExistingWrapper = Join-Path $InstallRoot 'ica-router.ps1'
$ExistingReady = $false
$ExistingRelease = $null
if (-not $ForceInstall -and
    (Test-Path -LiteralPath (Join-Path $StateDir 'secrets.json')) -and
    (Test-Path -LiteralPath $ExistingWrapper) -and
    (Test-Path -LiteralPath $CurrentFile)) {
    Assert-NotReparse $ExistingWrapper 'existing wrapper'
    Assert-NotReparse $CurrentFile 'current pointer'
    $existingReleaseId = [System.IO.File]::ReadAllText($CurrentFile).Trim()
    if ($existingReleaseId -match '^v[0-9]+\.[0-9]+\.[0-9]+(?:-local-[A-Za-z0-9-]+)?$') {
        $ExistingRelease = Join-Path $ReleasesDir $existingReleaseId
        $existingComplete = Join-Path $ExistingRelease '.complete'
        $sourceChanged = $false
        $candidateSource = $null
        if ($SourceDirectory) { $candidateSource = $SourceDirectory }
        elseif ($PSScriptRoot) { $candidateSource = $PSScriptRoot }
        if ($candidateSource) {
            foreach ($relativeSource in @('catalog.json', 'tools\routerctl.py')) {
                $sourceFile = Join-Path $candidateSource $relativeSource
                $existingFile = Join-Path (Join-Path $ExistingRelease 'app') $relativeSource
                if ((Test-Path -LiteralPath $sourceFile) -and (Test-Path -LiteralPath $existingFile)) {
                    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile).Hash
                    $existingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $existingFile).Hash
                    if ($sourceHash -ne $existingHash) { $sourceChanged = $true; break }
                }
            }
            if ($sourceChanged) { Write-Host 'Router source changed since the selected release; performing an update.' }
        }
        if (-not $sourceChanged -and
            (Test-Path -LiteralPath $existingComplete) -and
            (Test-Path -LiteralPath (Join-Path $ExistingRelease '.venv\Scripts\python.exe'))) {
            $systemPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            & $systemPowerShell -NoProfile -ExecutionPolicy Bypass -File $ExistingWrapper doctor *> $null
            $ExistingReady = $LASTEXITCODE -eq 0
        }
    }
}

if ($ExistingReady) {
    try {
        if ($ReplaceKeys) {
            Write-Host "Replacing saved Services Essentials keys. Submit an empty value after the last key."
            Invoke-InstalledRouter $ExistingWrapper @('stop')
            $bootstrapArguments = @('bootstrap','--replace-secrets')
            if ($KeyRotatorPath) { $bootstrapArguments += @('--import-key-rotator', $KeyRotatorPath) }
            else { $bootstrapArguments += '--prompt-keys' }
            if ($ClientPaths.Count -gt 0) {
                foreach ($clientPath in $ClientPaths) {
                    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($clientPath))
                    $bootstrapArguments += @('--client', $clientPath)
                }
            } else { $bootstrapArguments += '--no-configure-clients' }
            if ($NonInteractive -or $env:ICA_ROUTER_NON_INTERACTIVE -eq '1' -or [Console]::IsInputRedirected) {
                $bootstrapArguments += '--non-interactive'
            }
            Invoke-InstalledRouter $ExistingWrapper $bootstrapArguments
        } elseif ($ClientPaths.Count -gt 0) {
            Invoke-InstalledRouter $ExistingWrapper @('stop')
            $clientArguments = @('configure-clients')
            foreach ($clientPath in $ClientPaths) {
                [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($clientPath))
                $clientArguments += @('--client', $clientPath)
            }
            Invoke-InstalledRouter $ExistingWrapper $clientArguments
        } else {
            Write-Host 'Saved Services Essentials keys and a valid LiteLLM configuration already exist.'
            Write-Host 'Skipping installation and ensuring LiteLLM is running.'
        }
        Invoke-InstalledRouter $ExistingWrapper @('start')
        Invoke-InstalledRouter $ExistingWrapper @('status')
        if ($ClientPaths.Count -eq 0) {
            Write-Host 'Pi models.json was not modified.'
            Write-Host '  Easiest: rerun this installer with -PiModels.'
            Write-Host '  Or separately:'
            Write-Host "    powershell -File `"$ExistingWrapper`" stop"
            Write-Host "    powershell -File `"$ExistingWrapper`" configure-clients --client `"$HOME\.pi\agent\models.json`""
            Write-Host "    powershell -File `"$ExistingWrapper`" start"
        }
    }
    catch {
        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -ExecutionPolicy Bypass -File $ExistingWrapper start *> $null
        throw
    }
    finally {
        if ($script:InstallLockStream) { $script:InstallLockStream.Dispose(); $script:InstallLockStream = $null }
    }
    exit 0
}

Install-VerifiedUv
Invoke-Uv @('python','install',$PythonVersion)

function Resolve-SourceDirectory {
    if ($script:SourceDirectory) {
        $resolved = (Resolve-Path -LiteralPath $script:SourceDirectory).Path
        $script:LocalSource = $true
    } elseif ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'catalog.json'))) {
        $resolved = $PSScriptRoot
        $script:LocalSource = $true
    } else {
        $script:LocalSource = $false
        $script:TempSource = Join-Path $script:InstallRoot ('.download-' + [guid]::NewGuid().ToString('N'))
        [void][System.IO.Directory]::CreateDirectory($script:TempSource)
        Set-PrivateAcl $script:TempSource
        $asset = "$script:AppName-$script:SourceRef.zip"
        $checksumName = "$asset.sha256"
        $archive = Join-Path $script:TempSource $asset
        $checksum = Join-Path $script:TempSource $checksumName
        Write-Host "Downloading verified $script:RepoSlug@$script:SourceRef ..."
        Download-Https "https://github.com/$script:RepoSlug/releases/download/$script:SourceRef/$asset" $archive
        Download-Https "https://github.com/$script:RepoSlug/releases/download/$script:SourceRef/$checksumName" $checksum
        $manifest = [System.IO.File]::ReadAllText($checksum, [System.Text.Encoding]::ASCII)
        $pattern = '\A([0-9a-f]{64})  ' + [regex]::Escape($asset) + '(?:\r?\n)?\z'
        $match = [regex]::Match($manifest, $pattern)
        if (-not $match.Success) { throw "invalid checksum manifest" }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
        if ($actual -ne $match.Groups[1].Value) { throw "release asset SHA-256 mismatch" }
        $top = "$script:AppName-$script:SourceRef"
        $extractRoot = Join-Path $script:TempSource 'extracted'
        Expand-SafeReleaseZip $archive $extractRoot $top
        $resolved = Join-Path $extractRoot $top
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resolved 'catalog.json')) -or -not (Test-Path -LiteralPath (Join-Path $resolved 'tools\routerctl.py'))) {
        throw "source is incomplete"
    }
    $sourceVersion = [System.IO.File]::ReadAllText((Join-Path $resolved 'VERSION')).Trim()
    if ($sourceVersion -ne $script:SourceRef.Substring(1)) { throw "source VERSION does not match $script:SourceRef" }
    return $resolved
}

$script:ReleaseDir = $null
$script:ReleasePreexisted = $false
$script:InstallSuccess = $false
$script:Switched = $false
$script:OldWasStopped = $false
$script:RollbackDir = $null

try {
    $script:LocalSource = $false
    $ResolvedSource = Resolve-SourceDirectory
    if ($LocalSource) { $ReleaseId = "$SourceRef-local-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))-$([guid]::NewGuid().ToString('N').Substring(0,8))" }
    else { $ReleaseId = $SourceRef }
    $ReleaseDir = Join-Path $ReleasesDir $ReleaseId
    $script:ReleaseDir = $ReleaseDir
    $AppDir = Join-Path $ReleaseDir 'app'
    $VenvDir = Join-Path $ReleaseDir '.venv'
    $ReuseRelease = $false
    $selectedReleaseAtStart = $null
    if (Test-Path -LiteralPath $CurrentFile) {
        Assert-NotReparse $CurrentFile 'current pointer'
        $selectedReleaseAtStart = [System.IO.File]::ReadAllText($CurrentFile).Trim()
    }
    if (Test-Path -LiteralPath $ReleaseDir) {
        if ($LocalSource) { throw "unexpected local release collision: $ReleaseDir" }
        Assert-NotReparse $ReleaseDir 'release directory'
        $complete = Join-Path $ReleaseDir '.complete'
        if ((Test-Path -LiteralPath $complete) -and
            ([System.IO.File]::ReadAllText($complete).Trim() -eq $SourceRef) -and
            (Test-Path -LiteralPath (Join-Path $VenvDir 'Scripts\python.exe')) -and
            (Test-Path -LiteralPath (Join-Path $AppDir 'tools\routerctl.py'))) {
            $ReuseRelease = $true
            $script:ReleasePreexisted = $true
        } else {
            if ($selectedReleaseAtStart -eq $ReleaseId) { throw "selected release is incomplete; refusing to delete a live release" }
            Remove-Item -Recurse -Force -LiteralPath $ReleaseDir
        }
    }
    if (-not $ReuseRelease) {
        [void][System.IO.Directory]::CreateDirectory($AppDir)
        Set-PrivateAcl $ReleaseDir
        Set-PrivateAcl $AppDir

        foreach ($item in @('catalog.json','tools','examples','scripts','README.md','README.ko.md','SECURITY.md','LICENSE','VERSION','.gitignore','pyproject.toml','uv.lock')) {
            $sourceItem = Join-Path $ResolvedSource $item
            if (Test-Path -LiteralPath $sourceItem) {
                $links = @(Get-ChildItem -Force -Recurse -LiteralPath $sourceItem | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 })
                Assert-NotReparse $sourceItem "source item"
                if ($links.Count -ne 0) { throw "source item contains a reparse point: $item" }
                Copy-Item -Recurse -LiteralPath $sourceItem -Destination $AppDir
            }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $AppDir 'tools\routerctl.py')) -or -not (Test-Path -LiteralPath (Join-Path $AppDir 'uv.lock'))) {
            throw "staged release is incomplete"
        }
        Protect-PrivateTree $ReleaseDir

        Write-Host "Installing locked LiteLLM $LiteLLMVersion runtime ..."
        $oldProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
        $env:UV_PROJECT_ENVIRONMENT = $VenvDir
        try { Invoke-Uv @('sync','--frozen','--no-dev','--project',$AppDir,'--python',$PythonVersion) }
        finally {
            if ($null -eq $oldProjectEnvironment) { Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue }
            else { $env:UV_PROJECT_ENVIRONMENT = $oldProjectEnvironment }
        }
        $Python = Join-Path $VenvDir 'Scripts\python.exe'
        Invoke-Uv @('pip','check','--python',$Python)
        $InstalledVersion = (& $Python -I -c 'from importlib.metadata import version; import sys; print(version(sys.argv[1]))' litellm) -join ''
        if ($LASTEXITCODE -ne 0 -or $InstalledVersion.Trim() -ne $LiteLLMVersion) { throw "LiteLLM version mismatch: $InstalledVersion" }
        Write-Utf8File (Join-Path $ReleaseDir '.complete') ($SourceRef + "`n")
        Set-PrivateAcl $ReleaseDir; Set-PrivateAcl $AppDir; Set-PrivateAcl $VenvDir
    } else {
        $Python = Join-Path $VenvDir 'Scripts\python.exe'
        $InstalledVersion = (& $Python -I -c 'from importlib.metadata import version; import sys; print(version(sys.argv[1]))' litellm) -join ''
        if ($LASTEXITCODE -ne 0 -or $InstalledVersion.Trim() -ne $LiteLLMVersion) { throw "existing release has wrong LiteLLM version" }
    }

    $InstalledPythonVersion = (& $Python -I -c 'import platform; print(platform.python_version())') -join ''
    if ($LASTEXITCODE -ne 0 -or $InstalledPythonVersion.Trim() -ne $PythonVersion) { throw "Python version mismatch: $InstalledPythonVersion" }

    $Wrapper = Join-Path $InstallRoot 'ica-router.ps1'
    $wrapperText = @'
#requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$CurrentFile = Join-Path $Root 'current'
$ReleaseId = [System.IO.File]::ReadAllText($CurrentFile).Trim()
if ($ReleaseId -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+(?:-local-[A-Za-z0-9-]+)?$') { throw 'Invalid current release pointer' }
$Release = Join-Path (Join-Path $Root 'releases') $ReleaseId
$Python = Join-Path $Release '.venv\Scripts\python.exe'
$Control = Join-Path $Release 'app\tools\routerctl.py'
$Catalog = Join-Path $Release 'app\catalog.json'
& $Python $Control --state-dir (Join-Path $Root 'state') --catalog $Catalog --venv (Join-Path $Release '.venv') @args
exit $LASTEXITCODE
'@
    Write-Utf8File $Wrapper $wrapperText
    Set-PrivateAcl $Wrapper

    $OldReleaseId = $null
    $OldRelease = $null
    $OldPython = $null
    $OldControl = $null
    $OldCatalog = $null
    $OldVenv = $null
    if (Test-Path -LiteralPath $CurrentFile) {
        Assert-NotReparse $CurrentFile 'current pointer'
        $OldReleaseId = [System.IO.File]::ReadAllText($CurrentFile).Trim()
        if ($OldReleaseId -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+(?:-local-[A-Za-z0-9-]+)?$') { throw "invalid existing current pointer" }
        $OldRelease = Join-Path $ReleasesDir $OldReleaseId
    }
    if ($OldRelease -and (Test-Path -LiteralPath (Join-Path $OldRelease '.venv\Scripts\python.exe'))) {
        $OldPython = Join-Path $OldRelease '.venv\Scripts\python.exe'
        $OldControl = Join-Path $OldRelease 'app\tools\routerctl.py'
        $OldCatalog = Join-Path $OldRelease 'app\catalog.json'
        $OldVenv = Join-Path $OldRelease '.venv'
    } else {
        $legacyPython = Join-Path $InstallRoot '.venv\Scripts\python.exe'
        $legacyControl = Join-Path $InstallRoot 'app\tools\routerctl.py'
        if ((Test-Path -LiteralPath $legacyPython) -and (Test-Path -LiteralPath $legacyControl)) {
            $OldPython = $legacyPython; $OldControl = $legacyControl
            $OldCatalog = Join-Path $InstallRoot 'app\catalog.json'; $OldVenv = Join-Path $InstallRoot '.venv'
        }
    }
    $script:RollbackDir = Join-Path $InstallRoot ('.rollback-' + [guid]::NewGuid().ToString('N'))
    $rollbackState = Join-Path $RollbackDir 'state'
    $rollbackClients = Join-Path $RollbackDir 'clients'
    [void][System.IO.Directory]::CreateDirectory($rollbackState)
    [void][System.IO.Directory]::CreateDirectory($rollbackClients)
    Set-PrivateAcl $RollbackDir; Set-PrivateAcl $rollbackState; Set-PrivateAcl $rollbackClients
    foreach ($name in @('secrets.json','config.yaml','client-models.generated.json','runtime.json','generation.json')) {
        $sourceState = Join-Path $StateDir $name
        if (Test-Path -LiteralPath $sourceState) {
            Assert-NotReparse $sourceState 'state file'
            Copy-Item -LiteralPath $sourceState -Destination (Join-Path $rollbackState $name)
        }
    }
    $RollbackClientPaths = @($ClientPaths)
    for ($index = 0; $index -lt $RollbackClientPaths.Count; $index++) {
        Write-Utf8File (Join-Path $rollbackClients ("$index.path")) ($RollbackClientPaths[$index] + "`n")
        if (Test-Path -LiteralPath $RollbackClientPaths[$index]) {
            Assert-NotReparse $RollbackClientPaths[$index] 'client models file'
            Copy-Item -LiteralPath $RollbackClientPaths[$index] -Destination (Join-Path $rollbackClients ("$index.data"))
        }
    }
    Protect-PrivateTree $RollbackDir

    function Restore-PreviousRelease {
        $ErrorActionPreference = 'Continue'
        if ($script:Switched) {
            & $Python (Join-Path $AppDir 'tools\routerctl.py') --state-dir $StateDir --catalog (Join-Path $AppDir 'catalog.json') --venv $VenvDir stop 2>$null | Out-Null
        }
        if ($OldReleaseId) {
            $rollbackTemp = Join-Path $InstallRoot ('.current-rollback-' + [guid]::NewGuid().ToString('N'))
            Write-Utf8File $rollbackTemp ($OldReleaseId + "`n")
            Set-PrivateAcl $rollbackTemp
            Replace-FileAtomic $rollbackTemp $CurrentFile
            Set-PrivateAcl $CurrentFile
        } else { Remove-Item -Force -LiteralPath $CurrentFile -ErrorAction SilentlyContinue }
        foreach ($name in @('secrets.json','config.yaml','client-models.generated.json','runtime.json','generation.json')) {
            $savedState = Join-Path $rollbackState $name
            $targetState = Join-Path $StateDir $name
            if (Test-Path -LiteralPath $savedState) {
                $stateTemp = Join-Path $StateDir ('.' + $name + '.rollback-' + [guid]::NewGuid().ToString('N'))
                Copy-Item -LiteralPath $savedState -Destination $stateTemp
                Set-PrivateAcl $stateTemp
                Replace-FileAtomic $stateTemp $targetState
                Set-PrivateAcl $targetState
            } else { Remove-Item -Force -LiteralPath $targetState -ErrorAction SilentlyContinue }
        }
        for ($index = 0; $index -lt $RollbackClientPaths.Count; $index++) {
            $clientPath = [System.IO.File]::ReadAllText((Join-Path $rollbackClients ("$index.path"))).Trim()
            $savedClient = Join-Path $rollbackClients ("$index.data")
            if (Test-Path -LiteralPath $savedClient) {
                [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($clientPath))
                $clientTemp = $clientPath + '.rollback-' + [guid]::NewGuid().ToString('N')
                Copy-Item -LiteralPath $savedClient -Destination $clientTemp
                Set-PrivateAcl $clientTemp
                Replace-FileAtomic $clientTemp $clientPath
                Set-PrivateAcl $clientPath
            } else { Remove-Item -Force -LiteralPath $clientPath -ErrorAction SilentlyContinue }
        }
        Protect-PrivateTree $StateDir
        if ($OldPython -and $OldControl) {
            & $OldPython $OldControl --state-dir $StateDir --catalog $OldCatalog --venv $OldVenv start 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Warning 'previous router could not be restarted automatically' }
        }
        $script:Switched = $false
        $script:OldWasStopped = $false
    }

    if ($OldPython -and $OldControl) {
        & $OldPython $OldControl --state-dir $StateDir --catalog $OldCatalog --venv $OldVenv stop 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not safely stop the existing router" }
        $script:OldWasStopped = $true
    }

    $pointerTemp = Join-Path $InstallRoot ('.current-' + [guid]::NewGuid().ToString('N'))
    Write-Utf8File $pointerTemp ($ReleaseId + "`n")
    Set-PrivateAcl $pointerTemp
    Replace-FileAtomic $pointerTemp $CurrentFile
    $script:Switched = $true
    Set-PrivateAcl $CurrentFile

    $Control = Join-Path $AppDir 'tools\routerctl.py'
    $Catalog = Join-Path $AppDir 'catalog.json'
    $BootstrapArgs = @($Control,'--state-dir',$StateDir,'--catalog',$Catalog,'--venv',$VenvDir,'bootstrap')
    if ($ClientPaths.Count -gt 0) {
        foreach ($clientPath in $ClientPaths) {
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($clientPath))
            $BootstrapArgs += @('--client', $clientPath)
        }
    } else { $BootstrapArgs += '--no-configure-clients' }
    if ($ReplaceKeys -and -not $KeyRotatorPath) {
        $BootstrapArgs += @('--replace-secrets','--prompt-keys')
    } else {
        $BootstrapArgs += @('--import-key-rotator', $(if ($KeyRotatorPath) { $KeyRotatorPath } else { 'auto' }))
        if ($ReplaceKeys) { $BootstrapArgs += '--replace-secrets' }
    }
    if ($NonInteractive -or $env:ICA_ROUTER_NON_INTERACTIVE -eq '1' -or [Console]::IsInputRedirected) { $BootstrapArgs += '--non-interactive' }
    & $Python @BootstrapArgs
    if ($LASTEXITCODE -ne 0) { throw "configuration failed" }
    Protect-PrivateTree $StateDir
    & $Python $Control --state-dir $StateDir --catalog $Catalog --venv $VenvDir doctor
    if ($LASTEXITCODE -ne 0) { throw "doctor failed" }
    & $Python $Control --state-dir $StateDir --catalog $Catalog --venv $VenvDir start
    if ($LASTEXITCODE -ne 0) { throw "router failed to start" }
    $script:Switched = $false
    $script:OldWasStopped = $false
    $script:InstallSuccess = $true


    Write-Host ""
    Write-Host "Installed successfully."
    Write-Host "  Home:    $InstallRoot"
    Write-Host "  Release: $ReleaseId"
    Write-Host "  Status:  powershell -File `"$Wrapper`" status"
    Write-Host "  Stop:    powershell -File `"$Wrapper`" stop"
    Write-Host "  Start:   powershell -File `"$Wrapper`" start"
    if ($ClientPaths.Count -gt 0) {
        Write-Host "Restart Pi/prime-agent, then select a provider ending in '-router'."
    } else {
        Write-Host 'Pi models.json was not modified.'
        Write-Host '  Easiest: rerun this installer with -PiModels.'
        Write-Host '  Or separately:'
        Write-Host "    powershell -File `"$Wrapper`" stop"
        Write-Host "    powershell -File `"$Wrapper`" configure-clients --client `"$HOME\.pi\agent\models.json`""
        Write-Host "    powershell -File `"$Wrapper`" start"
    }
}
catch {
    $failure = $_
    if (($script:Switched -or $script:OldWasStopped) -and (Get-Command Restore-PreviousRelease -ErrorAction SilentlyContinue)) {
        Restore-PreviousRelease
    }
    throw $failure
}
finally {
    if ($script:RollbackDir -and (Test-Path -LiteralPath $script:RollbackDir)) {
        Remove-Item -Recurse -Force -LiteralPath $script:RollbackDir -ErrorAction SilentlyContinue
    }
    if (-not $script:InstallSuccess -and -not $script:ReleasePreexisted -and $script:ReleaseDir -and (Test-Path -LiteralPath $script:ReleaseDir)) {
        $selected = $null
        if (Test-Path -LiteralPath $CurrentFile) { $selected = [System.IO.File]::ReadAllText($CurrentFile).Trim() }
        if (-not $selected -or (Join-Path $ReleasesDir $selected) -ne $script:ReleaseDir) {
            Remove-Item -Recurse -Force -LiteralPath $script:ReleaseDir -ErrorAction SilentlyContinue
        }
    }
    if ($TempSource -and (Test-Path -LiteralPath $TempSource)) { Remove-Item -Recurse -Force -LiteralPath $TempSource }
    if ($script:InstallLockStream) { $script:InstallLockStream.Dispose() }
}
