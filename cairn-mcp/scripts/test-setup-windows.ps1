# Native Windows regression checks. All writes stay inside a disposable fixture.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string] $BinaryPath,
    [string] $SetupScript
)
$ErrorActionPreference = 'Stop'
if (-not $SetupScript) { $SetupScript = Join-Path $PSScriptRoot 'setup-windows.ps1' }
# Exercise the real path policy with synthetic drive classifications. No mapped
# drives or shares are created, and Join-Path cannot probe a real Z: provider.
& {
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $SetupScript, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw 'Cannot parse installer for path policy tests.' }
    $pathFunction = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-InstallPathChain'
    }, $false)
    if ($null -eq $pathFunction) { throw 'Installer path policy function is missing.' }
    . ([scriptblock]::Create($pathFunction.Extent.Text))
    function Join-Path([string] $Path, [string] $ChildPath) {
        return [IO.Path]::Combine($Path, $ChildPath)
    }
    function Get-InstallDriveType([string] $Root) {
        if ($Root -ne 'Z:\') { throw 'Drive classification received the wrong root.' }
        if ($case.Name -eq 'lookup-error') { throw 'Synthetic drive lookup failure.' }
        return $case.Type
    }
    $policyFailures = [Collections.Generic.List[string]]::new()
    foreach ($case in @(
        @{ Name = 'mapped-SMB'; Type = [IO.DriveType]::Network; Reject = $true },
        @{ Name = 'unknown'; Type = [IO.DriveType]::Unknown; Reject = $true },
        @{ Name = 'missing-root'; Type = [IO.DriveType]::NoRootDirectory; Reject = $true },
        @{ Name = 'null'; Type = $null; Reject = $true },
        @{ Name = 'unrecognised'; Type = 99; Reject = $true },
        @{ Name = 'lookup-error'; Type = $null; Reject = $true },
        @{ Name = 'fixed'; Type = [IO.DriveType]::Fixed; Reject = $false },
        @{ Name = 'removable'; Type = [IO.DriveType]::Removable; Reject = $false },
        @{ Name = 'CD-ROM'; Type = [IO.DriveType]::CDRom; Reject = $false },
        @{ Name = 'RAM'; Type = [IO.DriveType]::Ram; Reject = $false }
    )) {
        $rejected = $false
        $chain = @()
        try { $chain = @(Get-InstallPathChain 'Z:\Programs\Cairn') }
        catch { $rejected = $true }
        if ($rejected -ne $case.Reject) {
            $policyFailures.Add($case.Name + ': unexpected path acceptance/rejection')
        }
        if (-not $case.Reject -and ($chain -join '|') -ne 'Z:\|Z:\Programs|Z:\Programs\Cairn') {
            $policyFailures.Add($case.Name + ': incorrect local path chain')
        }
    }
    foreach ($path in @('\\server\share\Programs\Cairn', 'Z:\')) {
        $rejected = $false
        try { Get-InstallPathChain $path | Out-Null } catch { $rejected = $true }
        if (-not $rejected) { $policyFailures.Add('accepted UNC path or volume root') }
    }
    if ($policyFailures.Count -gt 0) { throw ($policyFailures -join "`n") }
    Write-Output 'PASS: mapped SMB and indeterminate drives refused; local drive path chains preserved; UNC and volume roots refused'
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('cairn-setup-test-' + [guid]::NewGuid())
$failures = [Collections.Generic.List[string]]::new()
New-Item -ItemType Directory -Path $fixture | Out-Null
$package = Join-Path $fixture 'package'
New-Item -ItemType Directory -Path $package | Out-Null
Copy-Item -LiteralPath $BinaryPath -Destination (Join-Path $package 'cairn-mcp.exe')
Copy-Item -LiteralPath $SetupScript -Destination (Join-Path $package 'setup-windows.ps1')
$setup = Join-Path $package 'setup-windows.ps1'
$running = $null
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trustedSids = @(
    $currentSid,
    'S-1-5-18',
    'S-1-5-32-544'
)
# Supply only synthetic input while exercising the real first-install write path.
function Read-Host {
    param([string] $Prompt, [switch] $AsSecureString)
    return ConvertTo-SecureString 'sentinel-new-credential' -AsPlainText -Force
}
function New-Fixture([string] $Name, [string] $IDValue) {
    $root = Join-Path $fixture $Name
    $config = Join-Path $root 'config'
    New-Item -ItemType Directory -Path $config -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $config 'cf-access-client-id'), $IDValue)
    [IO.File]::WriteAllText((Join-Path $config 'cf-access-client-secret'), 'sentinel-secret')
    Set-PrivateConfigAcl $config
    return @{ Config = $config; Bin = (Join-Path $root 'bin') }
}
function Invoke-Setup($Case) {
    & $setup -ConfigDirectory $Case.Config -InstallDirectory $Case.Bin | Out-Null
}
function Add-AllowRule([string] $Path, [string] $Sid, [System.Security.AccessControl.FileSystemRights] $Rights) {
    $acl = [IO.Directory]::GetAccessControl($Path)
    [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        [System.Security.Principal.SecurityIdentifier]::new($Sid),
        $Rights,
        'ContainerInherit,ObjectInherit',
        'None',
        'Allow'))
    [IO.Directory]::SetAccessControl($Path, $acl)
}
function Add-FileAllowRule([string] $Path, [string] $Sid, [System.Security.AccessControl.FileSystemRights] $Rights) {
    $acl = [IO.File]::GetAccessControl($Path)
    [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        [System.Security.Principal.SecurityIdentifier]::new($Sid), $Rights, 'Allow'))
    [IO.File]::SetAccessControl($Path, $acl)
}
function Set-PrivateConfigAcl([string] $Path) {
    foreach ($name in @('cf-access-client-id', 'cf-access-client-secret')) {
        $fileAcl = New-Object System.Security.AccessControl.FileSecurity
        $fileAcl.SetOwner([System.Security.Principal.SecurityIdentifier]::new($currentSid))
        $fileAcl.SetAccessRuleProtection($true, $false)
        [void] $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($currentSid), 'FullControl', 'Allow'))
        [IO.File]::SetAccessControl((Join-Path $Path $name), $fileAcl)
    }
    $directoryAcl = New-Object System.Security.AccessControl.DirectorySecurity
    $directoryAcl.SetOwner([System.Security.Principal.SecurityIdentifier]::new($currentSid))
    $directoryAcl.SetAccessRuleProtection($true, $false)
    [void] $directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        [System.Security.Principal.SecurityIdentifier]::new($currentSid),
        'FullControl',
        'ContainerInherit,ObjectInherit',
        'None',
        'Allow'))
    [IO.Directory]::SetAccessControl($Path, $directoryAcl)
}
function Set-TrustedDirectoryAcl([string] $Path) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner([System.Security.Principal.SecurityIdentifier]::new($currentSid))
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($trustee in $trustedSids) {
        [void] $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($trustee),
            'FullControl',
            'ContainerInherit,ObjectInherit',
            'None',
            'Allow'))
    }
    [IO.Directory]::SetAccessControl($Path, $acl)
}
function Add-RawAllowRule([string] $Path, [int] $Mask) {
    $acl = [IO.Directory]::GetAccessControl($Path)
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(), 0)
    $flags = [System.Security.AccessControl.AceFlags]'ContainerInherit, ObjectInherit'
    $ace = [System.Security.AccessControl.CommonAce]::new(
        $flags,
        [System.Security.AccessControl.AceQualifier]::AccessAllowed,
        $Mask,
        [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0'),
        $false,
        $null)
    $raw.DiscretionaryAcl.InsertAce($raw.DiscretionaryAcl.Count, $ace)
    $bytes = [Array]::CreateInstance([byte], $raw.BinaryLength)
    $raw.GetBinaryForm($bytes, 0)
    $acl.SetSecurityDescriptorBinaryForm(
        $bytes, [System.Security.AccessControl.AccessControlSections]::Access)
    [IO.Directory]::SetAccessControl($Path, $acl)
}
function New-Junction([string] $Path, [string] $Target) {
    New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null
}
try {
    $fresh = @{ Config = (Join-Path $fixture 'fresh-config'); Bin = (Join-Path $fixture 'fresh-bin') }
    Invoke-Setup $fresh
    foreach ($name in @('cf-access-client-id', 'cf-access-client-secret')) {
        if ([IO.File]::ReadAllText((Join-Path $fresh.Config $name)) -ne 'sentinel-new-credential') {
            $failures.Add('first install did not preserve supplied sentinel')
        }
    }
    $paths = New-Fixture 'missing-programs-parent' 'sentinel-id'
    $safeParent = Split-Path -Parent $paths.Bin
    Set-TrustedDirectoryAcl $safeParent
    $paths.Bin = Join-Path $safeParent 'Programs\Cairn'
    Invoke-Setup $paths
    if (-not (Test-Path -LiteralPath (Join-Path $paths.Bin 'cairn-mcp.exe') -PathType Leaf)) {
        $failures.Add('did not create a protected missing Programs/Cairn suffix')
    }
    foreach ($case in @(
        @{ Name = 'empty'; Value = '' },
        @{ Name = 'oversized'; Value = ('x' * 4097) }
    )) {
        $paths = New-Fixture $case.Name $case.Value
        $before = (Get-FileHash (Join-Path $paths.Config 'cf-access-client-id')).Hash
        $rejected = $false
        try { Invoke-Setup $paths } catch { $rejected = $true }
        if (-not $rejected) { $failures.Add($case.Name + ': accepted invalid credentials') }
        if ((Get-FileHash (Join-Path $paths.Config 'cf-access-client-id')).Hash -ne $before) {
            $failures.Add($case.Name + ': changed existing credential bytes')
        }
    }
    $paths = New-Fixture 'broad-config-acl' 'sentinel-id'
    Add-AllowRule $paths.Config 'S-1-1-0' ([System.Security.AccessControl.FileSystemRights]::FullControl)
    $beforeAcl = [IO.Directory]::GetAccessControl($paths.Config).GetSecurityDescriptorSddlForm('All')
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a broad existing ConfigDirectory') }
    $afterAcl = [IO.Directory]::GetAccessControl($paths.Config).GetSecurityDescriptorSddlForm('All')
    if ($afterAcl -ne $beforeAcl) { $failures.Add('changed rejected ConfigDirectory ACL') }

    $paths = New-Fixture 'broad-credential-acl' 'sentinel-id'
    $credential = Join-Path $paths.Config 'cf-access-client-id'
    Add-FileAllowRule $credential 'S-1-1-0' ([System.Security.AccessControl.FileSystemRights]::FullControl)
    $beforeAcl = [IO.File]::GetAccessControl($credential).GetSecurityDescriptorSddlForm('All')
    $before = (Get-FileHash $credential).Hash
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a broad existing credential file') }
    if ((Get-FileHash $credential).Hash -ne $before) { $failures.Add('changed rejected credential bytes') }
    $afterAcl = [IO.File]::GetAccessControl($credential).GetSecurityDescriptorSddlForm('All')
    if ($afterAcl -ne $beforeAcl) { $failures.Add('changed rejected credential ACL') }

    $paths = New-Fixture 'credential-hardlink' 'sentinel-id'
    $credential = Join-Path $paths.Config 'cf-access-client-id'
    Remove-Item -LiteralPath $credential
    $hardlinkTarget = Join-Path $fixture 'credential-hardlink-target'
    [IO.File]::WriteAllText($hardlinkTarget, 'must remain unchanged')
    New-Item -ItemType HardLink -Path $credential -Target $hardlinkTarget | Out-Null
    $beforeAcl = [IO.File]::GetAccessControl($hardlinkTarget).GetSecurityDescriptorSddlForm('All')
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a hard-linked credential file') }
    if ([IO.File]::ReadAllText($hardlinkTarget) -ne 'must remain unchanged') {
        $failures.Add('changed a hard-linked credential target')
    }
    $afterAcl = [IO.File]::GetAccessControl($hardlinkTarget).GetSecurityDescriptorSddlForm('All')
    if ($afterAcl -ne $beforeAcl) { $failures.Add('changed a hard-linked credential ACL') }

    $paths = New-Fixture 'broad-install-acl' 'sentinel-id'
    New-Item -ItemType Directory -Path $paths.Bin | Out-Null
    Add-AllowRule $paths.Bin 'S-1-1-0' ([System.Security.AccessControl.FileSystemRights]::FullControl)
    $beforeAcl = [IO.Directory]::GetAccessControl($paths.Bin).GetSecurityDescriptorSddlForm('All')
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a broad existing InstallDirectory') }
    $afterAcl = [IO.Directory]::GetAccessControl($paths.Bin).GetSecurityDescriptorSddlForm('All')
    if ($afterAcl -ne $beforeAcl) { $failures.Add('changed rejected InstallDirectory ACL') }
    if (Test-Path -LiteralPath (Join-Path $paths.Bin 'cairn-mcp.exe')) {
        $failures.Add('wrote to a rejected broad InstallDirectory')
    }

    $paths = New-Fixture 'shared-install-directory' 'sentinel-id'
    New-Item -ItemType Directory -Path $paths.Bin | Out-Null
    $unrelated = Join-Path $paths.Bin 'another-application.txt'
    [IO.File]::WriteAllText($unrelated, 'must remain unchanged')
    $beforeAcl = [IO.Directory]::GetAccessControl($paths.Bin).GetSecurityDescriptorSddlForm('All')
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a shared install directory') }
    if ([IO.File]::ReadAllText($unrelated) -ne 'must remain unchanged') {
        $failures.Add('changed shared install directory contents')
    }
    $afterAcl = [IO.Directory]::GetAccessControl($paths.Bin).GetSecurityDescriptorSddlForm('All')
    if ($afterAcl -ne $beforeAcl) { $failures.Add('changed rejected shared install directory ACL') }

    foreach ($danger in @(
        @{ Name = 'delete'; Rights = [System.Security.AccessControl.FileSystemRights]::Delete },
        @{ Name = 'delete-child'; Rights = [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles },
        @{ Name = 'create-directory'; Rights = [System.Security.AccessControl.FileSystemRights]::CreateDirectories }
    )) {
        $name = 'broad-parent-' + $danger.Name
        $paths = New-Fixture $name 'sentinel-id'
        $parent = Split-Path -Parent $paths.Bin
        Add-AllowRule $parent 'S-1-1-0' $danger.Rights
        $rejected = $false
        try { Invoke-Setup $paths } catch { $rejected = $true }
        if (-not $rejected) { $failures.Add($name + ': accepted replace-capable parent ACL') }
        if (Test-Path -LiteralPath $paths.Bin) { $failures.Add($name + ': created install directory') }
    }
    foreach ($generic in @(
        @{ Name = 'generic-all'; Mask = 0x10000000 },
        @{ Name = 'generic-write'; Mask = 0x40000000 }
    )) {
        $name = 'broad-parent-' + $generic.Name
        $paths = New-Fixture $name 'sentinel-id'
        $parent = Split-Path -Parent $paths.Bin
        Set-TrustedDirectoryAcl $parent
        Add-RawAllowRule $parent $generic.Mask
        $rejected = $false
        try { Invoke-Setup $paths } catch { $rejected = $true }
        if (-not $rejected) { $failures.Add($name + ': accepted raw generic parent ACL') }
        if (Test-Path -LiteralPath $paths.Bin) { $failures.Add($name + ': created install directory') }
    }
    $paths = New-Fixture 'read-only-parent' 'sentinel-id'
    $parent = Split-Path -Parent $paths.Bin
    Set-TrustedDirectoryAcl $parent
    Add-RawAllowRule $parent ([int]::MinValue)
    Invoke-Setup $paths
    if (-not (Test-Path -LiteralPath (Join-Path $paths.Bin 'cairn-mcp.exe') -PathType Leaf)) {
        $failures.Add('rejected harmless raw GENERIC_READ ancestry')
    }

    $paths = New-Fixture 'ancestor-junction' 'sentinel-id'
    $junctionTarget = Join-Path $fixture 'ancestor-junction-target'
    New-Item -ItemType Directory -Path $junctionTarget | Out-Null
    $junction = Join-Path (Split-Path -Parent $paths.Bin) 'linked-parent'
    New-Junction $junction $junctionTarget
    $paths.Bin = Join-Path $junction 'Cairn'
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted an install path through an ancestor junction') }
    if (Test-Path -LiteralPath (Join-Path $junctionTarget 'Cairn')) {
        $failures.Add('wrote through an ancestor junction')
    }

    $paths = New-Fixture 'install-junction' 'sentinel-id'
    $junctionTarget = Join-Path $fixture 'install-junction-target'
    New-Item -ItemType Directory -Path $junctionTarget | Out-Null
    New-Junction $paths.Bin $junctionTarget
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a junction as InstallDirectory') }
    if ((Get-ChildItem -Force -LiteralPath $junctionTarget).Count -ne 0) {
        $failures.Add('wrote through an InstallDirectory junction')
    }

    $paths = New-Fixture 'binary-junction' 'sentinel-id'
    New-Item -ItemType Directory -Path $paths.Bin | Out-Null
    $junctionTarget = Join-Path $fixture 'binary-junction-target'
    New-Item -ItemType Directory -Path $junctionTarget | Out-Null
    New-Junction (Join-Path $paths.Bin 'cairn-mcp.exe') $junctionTarget
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a reparse executable destination') }
    if ((Get-ChildItem -Force -LiteralPath $junctionTarget).Count -ne 0) {
        $failures.Add('wrote through an executable destination junction')
    }

    $paths = New-Fixture 'binary-hardlink' 'sentinel-id'
    New-Item -ItemType Directory -Path $paths.Bin | Out-Null
    $directoryAcl = New-Object System.Security.AccessControl.DirectorySecurity
    $directoryAcl.SetOwner([System.Security.Principal.SecurityIdentifier]::new($currentSid))
    $directoryAcl.SetAccessRuleProtection($true, $false)
    foreach ($trusted in $trustedSids) {
        [void] $directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($trusted),
            'FullControl',
            'ContainerInherit,ObjectInherit',
            'None',
            'Allow'))
    }
    [IO.Directory]::SetAccessControl($paths.Bin, $directoryAcl)
    $hardlinkTarget = Join-Path $fixture 'hardlink-target.exe'
    [IO.File]::WriteAllText($hardlinkTarget, 'must remain unchanged')
    New-Item -ItemType HardLink -Path (Join-Path $paths.Bin 'cairn-mcp.exe') -Target $hardlinkTarget | Out-Null
    $beforeTargetAcl = [IO.File]::GetAccessControl($hardlinkTarget).GetSecurityDescriptorSddlForm('All')
    $beforeAcl = [IO.Directory]::GetAccessControl($paths.Bin).GetSecurityDescriptorSddlForm('All')
    $rejected = $false
    try { Invoke-Setup $paths } catch { $rejected = $true }
    if (-not $rejected) { $failures.Add('accepted a hard-linked executable destination') }
    if ([IO.File]::ReadAllText($hardlinkTarget) -ne 'must remain unchanged') {
        $failures.Add('changed a hardlink target outside InstallDirectory')
    }
    $afterTargetAcl = [IO.File]::GetAccessControl($hardlinkTarget).GetSecurityDescriptorSddlForm('All')
    if ($afterTargetAcl -ne $beforeTargetAcl) {
        $failures.Add('changed a hardlink target ACL outside InstallDirectory')
    }
    $afterAcl = [IO.Directory]::GetAccessControl($paths.Bin).GetSecurityDescriptorSddlForm('All')
    if ($afterAcl -ne $beforeAcl) { $failures.Add('changed rejected hardlink directory ACL') }

    $paths = New-Fixture 'running' 'sentinel-id'
    Invoke-Setup $paths
    $binary = Join-Path $paths.Bin 'cairn-mcp.exe'
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $binary
    $info.Arguments = 'stdio --allow-http-upstream --upstream-url http://127.0.0.1:1/mcp --cf-client-id-path "' + (Join-Path $paths.Config 'cf-access-client-id') + '" --cf-client-secret-path "' + (Join-Path $paths.Config 'cf-access-client-secret') + '"'
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $running = [Diagnostics.Process]::Start($info)
    # A running process holds its image open, independent of bridge startup timing.
    try { Invoke-Setup $paths } catch { $failures.Add('identical running binary: ' + $_.Exception.Message + ' at ' + $_.ScriptStackTrace) }
    if ($running.HasExited) { $failures.Add('fixture relay exited unexpectedly') }
    # Alter the source image without changing its executable instructions.
    $stream = [IO.File]::Open((Join-Path $package 'cairn-mcp.exe'), [IO.FileMode]::Append)
    try { $stream.WriteByte(0) } finally { $stream.Dispose() }
    $rejected = $false
    try { Invoke-Setup $paths } catch {
        $rejected = $true
        if ($_.Exception.Message -notmatch 'Stop.*Cairn') { $failures.Add('running upgrade lacks shutdown guidance') }
    }
    if (-not $rejected) { $failures.Add('replaced a running different binary') }
} finally {
    if ($null -ne $running) {
        if (-not $running.HasExited) {
            $running.StandardInput.Close()
            if (-not $running.WaitForExit(15000)) { $running.Kill(); $running.WaitForExit() }
        }
        $running.Dispose()
    }
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
if ($failures.Count -gt 0) { throw ($failures -join "`n") }
Write-Output 'PASS: first install; invalid credentials preserved and rejected; protected dedicated install ACL; unsafe parents and reparse paths refused; identical running install; blocked upgrade guidance'
