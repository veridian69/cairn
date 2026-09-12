# Run from an extracted Windows package in PowerShell as your normal user.
[CmdletBinding()]
param(
    [string] $ConfigDirectory = (Join-Path $env:USERPROFILE '.config\cairn'),
    [string] $InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Programs\Cairn')
)

$ErrorActionPreference = 'Stop'
$binary = Join-Path $PSScriptRoot 'cairn-mcp.exe'
if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
    throw 'Extract the Windows package first; cairn-mcp.exe must be beside this script.'
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User
$systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$administratorsSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$trustedInstallerSid = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
$installGrantSids = @($sid.Value, $systemSid.Value, $administratorsSid.Value)
$trustedAuthoritySids = @($sid.Value, $systemSid.Value, $administratorsSid.Value, $trustedInstallerSid)
$configDir = [IO.Path]::GetFullPath($ConfigDirectory)
$binDir = [IO.Path]::GetFullPath($InstallDirectory)

function Assert-NotReparsePoint([string] $Path) {
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -Force -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing reparse point: $Path"
        }
    }
}

function Assert-NonNullDacl($Acl, [string] $Path) {
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $Acl.GetSecurityDescriptorBinaryForm(), 0)
    if ($null -eq $raw.DiscretionaryAcl) {
        throw "Refusing null DACL: $Path"
    }
}

function Get-InstallDriveType([string] $Root) {
    return [IO.DriveInfo]::new($Root).DriveType
}

function Get-InstallPathChain([string] $Path) {
    $root = [IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrEmpty($root) -or $root.StartsWith('\')) {
        throw 'InstallDirectory must be on a local Windows volume; UNC paths are not supported.'
    }
    # A mapped SMB drive has a drive-letter root too. Require a recognised
    # local type before inspecting ancestors; unknown types and lookup errors
    # must fail closed. Local volumes still undergo every existing ACL check.
    $driveType = Get-InstallDriveType $root
    if ($driveType -notin @(
        [IO.DriveType]::Fixed, [IO.DriveType]::Removable,
        [IO.DriveType]::CDRom, [IO.DriveType]::Ram
    )) {
        throw 'InstallDirectory must be on a local Windows volume; network or indeterminate drives are not supported.'
    }
    if ([string]::Equals(
        $Path.TrimEnd([char[]]@('\', '/')),
        $root.TrimEnd([char[]]@('\', '/')),
        [StringComparison]::OrdinalIgnoreCase)) {
        throw 'InstallDirectory must be a dedicated Cairn directory, not a volume root.'
    }
    $chain = [Collections.Generic.List[string]]::new()
    $chain.Add($root)
    $current = $root
    foreach ($part in ($Path.Substring($root.Length) -split '[\\/]')) {
        if ($part.Length -eq 0) { continue }
        $current = Join-Path $current $part
        $chain.Add($current)
    }
    return $chain.ToArray()
}

function Assert-SafeInstallAncestor([string] $Path, [bool] $RejectCreateChild) {
    Assert-NotReparsePoint $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Install path ancestor is not a directory: $Path"
    }
    $acl = [IO.Directory]::GetAccessControl(
        $Path, [System.Security.AccessControl.AccessControlSections]'Owner, Access')
    Assert-NonNullDacl $acl $Path
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($trustedAuthoritySids -notcontains $owner) {
        throw "Install path ancestor has an untrusted owner: $Path"
    }
    $dangerous = [System.Security.AccessControl.FileSystemRights]::Delete -bor
        [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [System.Security.AccessControl.FileSystemRights]::TakeOwnership
    $dangerousMask = ([int64][int]$dangerous) -bor [int64]0x10000000
    if ($RejectCreateChild) {
        $dangerous = $dangerous -bor
            [System.Security.AccessControl.FileSystemRights]::CreateDirectories -bor
            [System.Security.AccessControl.FileSystemRights]::CreateFiles
        $dangerousMask = ([int64][int]$dangerous) -bor
            [int64]0x10000000 -bor [int64]0x40000000
    }
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(), 0)
    foreach ($ace in $raw.DiscretionaryAcl) {
        if (([int]$ace.AceFlags -band
            [int][System.Security.AccessControl.AceFlags]::InheritOnly) -ne 0) {
            continue
        }
        if ($ace -isnot [System.Security.AccessControl.QualifiedAce]) {
            throw "Install path ancestor contains an unsupported access entry: $Path"
        }
        if ($ace.AceQualifier -ne [System.Security.AccessControl.AceQualifier]::AccessAllowed) {
            continue
        }
        if ($trustedAuthoritySids -contains $ace.SecurityIdentifier.Value) { continue }
        if (([int64]$ace.AccessMask -band $dangerousMask) -ne 0) {
            throw "Install path ancestor grants replacement access to an untrusted principal: $Path"
        }
    }
}

function New-ProtectedInstallDirectoryAcl {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($trustee in @($sid, $systemSid, $administratorsSid)) {
        [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $trustee, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    return $acl
}

function New-ProtectedInstallFileAcl {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($trustee in @($sid, $systemSid, $administratorsSid)) {
        [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $trustee, 'FullControl', 'Allow'))
    }
    return $acl
}

function New-ProtectedConfigDirectoryAcl {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    return $acl
}

function New-ProtectedConfigFileAcl {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid, 'FullControl', 'Allow'))
    return $acl
}

function Assert-ProtectedConfigAcl([string] $Path, [bool] $Directory) {
    if ($Directory) {
        $acl = [IO.Directory]::GetAccessControl(
            $Path, [System.Security.AccessControl.AccessControlSections]'Owner, Access')
    } else {
        $acl = [IO.File]::GetAccessControl(
            $Path, [System.Security.AccessControl.AccessControlSections]'Owner, Access')
    }
    Assert-NonNullDacl $acl $Path
    if (-not $acl.AreAccessRulesProtected -or
        $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) {
        throw "Existing credential path is not protected for the current user: $Path"
    }
    $hasCurrentControl = $false
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(), 0)
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($ace -isnot [System.Security.AccessControl.QualifiedAce] -or
            $ace.AceQualifier -ne [System.Security.AccessControl.AceQualifier]::AccessAllowed) {
            throw "Existing credential path has an unsupported access entry: $Path"
        }
        if ($installGrantSids -notcontains $ace.SecurityIdentifier.Value) {
            throw "Existing credential path grants access to an untrusted principal: $Path"
        }
        if (($ace.AccessMask -band
            [int][System.Security.AccessControl.FileSystemRights]::FullControl) -ne
            [int][System.Security.AccessControl.FileSystemRights]::FullControl) {
            throw "Existing credential path does not grant required control: $Path"
        }
        if ($ace.SecurityIdentifier.Value -eq $sid.Value) {
            $hasCurrentControl = $true
        }
    }
    if (-not $hasCurrentControl) {
        throw "Existing credential path does not grant the current user control: $Path"
    }
}

function Assert-ProtectedInstallAcl([string] $Path, [bool] $Directory) {
    if ($Directory) {
        $acl = [IO.Directory]::GetAccessControl(
            $Path, [System.Security.AccessControl.AccessControlSections]'Owner, Access')
    } else {
        $acl = [IO.File]::GetAccessControl(
            $Path, [System.Security.AccessControl.AccessControlSections]'Owner, Access')
    }
    Assert-NonNullDacl $acl $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Install ACL still inherits access: $Path"
    }
    if ($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) {
        throw "Install ACL has the wrong owner: $Path"
    }
    $seen = @{}
    foreach ($rule in $acl.GetAccessRules(
        $true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne
            [System.Security.AccessControl.AccessControlType]::Allow) {
            throw "Install ACL contains an unsupported deny entry: $Path"
        }
        $ruleSid = $rule.IdentityReference.Value
        if ($installGrantSids -notcontains $ruleSid) {
            throw "Install ACL grants access to an untrusted principal: $Path"
        }
        if (($rule.FileSystemRights -band
            [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
            [System.Security.AccessControl.FileSystemRights]::FullControl) {
            throw "Install ACL does not grant required control: $Path"
        }
        if ($Directory) {
            $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
            if (($rule.InheritanceFlags -band $inherit) -ne $inherit -or
                $rule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) {
                throw "Install directory ACL does not protect child entries: $Path"
            }
        }
        $seen[$ruleSid] = $true
    }
    foreach ($trustee in $installGrantSids) {
        if (-not $seen.ContainsKey($trustee)) {
            throw "Install ACL is missing a required trustee: $Path"
        }
    }
}

function Assert-DedicatedInstallDirectory([string] $Path) {
    foreach ($entry in @(Get-ChildItem -Force -LiteralPath $Path)) {
        if ($entry.Name -ne 'cairn-mcp.exe') {
            throw 'InstallDirectory must be dedicated to Cairn; it contains another entry.'
        }
        Assert-NotReparsePoint $entry.FullName
        if (-not (Test-Path -LiteralPath $entry.FullName -PathType Leaf)) {
            throw 'The existing cairn-mcp.exe destination is not a regular file.'
        }
        if ($entry.LinkType -eq 'HardLink') {
            throw 'Refusing a hard-linked cairn-mcp.exe destination.'
        }
    }
}

function Initialize-ProtectedInstallDirectory([string] $Path) {
    $chain = @(Get-InstallPathChain $Path)
    $leafIndex = $chain.Count - 1
    $firstMissing = $chain.Count
    for ($index = 0; $index -lt $chain.Count; $index++) {
        if (-not (Test-Path -LiteralPath $chain[$index])) {
            $firstMissing = $index
            break
        }
    }
    $existingAncestorEnd = [Math]::Min($leafIndex - 1, $firstMissing - 1)
    for ($index = 0; $index -le $existingAncestorEnd; $index++) {
        $rejectCreate = ($index -eq $leafIndex - 1) -or
            ($firstMissing -lt $chain.Count -and $index -eq $firstMissing - 1)
        Assert-SafeInstallAncestor $chain[$index] $rejectCreate
    }
    if ($firstMissing -eq $chain.Count) {
        Assert-NotReparsePoint $Path
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            throw 'InstallDirectory must be a directory.'
        }
        Assert-DedicatedInstallDirectory $Path
        Assert-ProtectedInstallAcl $Path $true
    } else {
        for ($index = $firstMissing; $index -lt $chain.Count; $index++) {
            [IO.Directory]::CreateDirectory(
                $chain[$index], (New-ProtectedInstallDirectoryAcl)) | Out-Null
            Assert-NotReparsePoint $chain[$index]
            Assert-ProtectedInstallAcl $chain[$index] $true
        }
    }
    Assert-NotReparsePoint $Path
    Assert-ProtectedInstallAcl $Path $true
    Assert-DedicatedInstallDirectory $Path
}

# Restrict the directory before creating any credential files. Never put
# credential values in command arguments, environment variables or logs.
Assert-NotReparsePoint (Split-Path -Parent $configDir)
Assert-NotReparsePoint $configDir
if (Test-Path -LiteralPath $configDir) {
    if (-not (Test-Path -LiteralPath $configDir -PathType Container)) {
        throw 'ConfigDirectory must be a directory.'
    }
    Assert-ProtectedConfigAcl $configDir $true
} else {
    [IO.Directory]::CreateDirectory(
        $configDir, (New-ProtectedConfigDirectoryAcl)) | Out-Null
    Assert-NotReparsePoint $configDir
    Assert-ProtectedConfigAcl $configDir $true
}

foreach ($name in @('cf-access-client-id', 'cf-access-client-secret')) {
    $path = Join-Path $configDir $name
    Assert-NotReparsePoint $path
    if (Test-Path -LiteralPath $path) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Existing credential path is not a regular file: $path"
        }
        if ((Get-Item -Force -LiteralPath $path).LinkType -eq 'HardLink') {
            throw "Refusing a hard-linked credential file: $path"
        }
        Assert-ProtectedConfigAcl $path $false
    } else {
        $secret = Read-Host "Enter $name" -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
        try {
            $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
            if ([string]::IsNullOrEmpty($value)) { throw "$name must not be empty" }
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($value)
            $stream = [IO.FileStream]::new(
                $path,
                [IO.FileMode]::CreateNew,
                [System.Security.AccessControl.FileSystemRights]::Write,
                [IO.FileShare]::None,
                4096,
                [IO.FileOptions]::None,
                (New-ProtectedConfigFileAcl))
            try {
                $stream.Write($bytes, 0, $bytes.Length)
            } finally {
                $stream.Dispose()
                [Array]::Clear($bytes, 0, $bytes.Length)
            }
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
            $value = $null
            $secret.Dispose()
        }
        Assert-ProtectedConfigAcl $path $false
    }
}

Initialize-ProtectedInstallDirectory $binDir
$installedBinary = Join-Path $binDir 'cairn-mcp.exe'
Assert-NotReparsePoint $installedBinary
if (Test-Path -LiteralPath $installedBinary -PathType Leaf) {
    Assert-ProtectedInstallAcl $installedBinary $false
}

# EOF validates the STDIO credentials without sending an MCP request. Pin an
# unreachable loopback upstream so this check cannot contact productive Cairn.
$checkInfo = [Diagnostics.ProcessStartInfo]::new()
$checkInfo.FileName = $binary
$checkInfo.Arguments = 'stdio --allow-http-upstream --upstream-url http://127.0.0.1:1/mcp --cf-client-id-path "' + (Join-Path $configDir 'cf-access-client-id') + '" --cf-client-secret-path "' + (Join-Path $configDir 'cf-access-client-secret') + '"'
$checkInfo.UseShellExecute = $false
$checkInfo.RedirectStandardInput = $true
$checkInfo.RedirectStandardOutput = $true
$checkInfo.RedirectStandardError = $true
$check = [Diagnostics.Process]::Start($checkInfo)
try {
    $check.StandardInput.Close()
    if (-not $check.WaitForExit(30000)) {
        $check.Kill()
        $check.WaitForExit()
        throw 'Local credential validation timed out; installation was not completed.'
    }
    if ($check.ExitCode -ne 0) {
        throw 'Local credential validation failed. Repair the credential files in the configuration directory and rerun setup; existing values were preserved.'
    }
} finally {
    $check.Dispose()
}

$sameBinary = (Test-Path -LiteralPath $installedBinary -PathType Leaf) -and
    ((Get-FileHash -LiteralPath $binary).Hash -eq (Get-FileHash -LiteralPath $installedBinary).Hash)
if (-not $sameBinary) {
    try { Copy-Item -LiteralPath $binary -Destination $installedBinary -Force }
    catch { throw 'Cannot replace the installed binary. Stop the Cairn MCP in all clients, check directory permissions, then rerun setup.' }
}
Assert-NotReparsePoint $installedBinary
if (-not (Test-Path -LiteralPath $installedBinary -PathType Leaf)) {
    throw 'The installed cairn-mcp.exe is not a regular file.'
}
[IO.File]::SetAccessControl($installedBinary, (New-ProtectedInstallFileAcl))
Assert-ProtectedInstallAcl $installedBinary $false
Write-Host 'Installed. Set $cairnUpstreamUrl to the HTTPS MCP endpoint supplied by your operator, then register:'
Write-Host ('codex mcp add cairn -- "' + $installedBinary + '" stdio --upstream-url "$cairnUpstreamUrl"')
