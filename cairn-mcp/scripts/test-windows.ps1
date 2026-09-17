# Run cross-built tests on Windows; only disposable sentinel files and loopback servers.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string] $BuildDirectory)
$ErrorActionPreference = 'Stop'
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('cairn-runtime-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    foreach ($test in @(
        @{ Name = 'credentials'; Args = @('-test.v', '-test.timeout=60s') },
        @{ Name = 'stdio'; Args = @('-test.v', '-test.timeout=120s', '-test.run=Stdio') }
    )) {
        $exe = Join-Path $fixture ($test.Name + '.test.exe')
        Copy-Item -LiteralPath (Join-Path $BuildDirectory ($test.Name + '.test.exe')) -Destination $exe
        $info = [Diagnostics.ProcessStartInfo]::new()
        $info.FileName = $exe
        $info.Arguments = $test.Args -join ' '
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($info)
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        try {
            if (-not $process.WaitForExit(150000)) {
                $process.Kill()
                $process.WaitForExit()
                throw ($test.Name + ' tests timed out')
            }
            $process.WaitForExit()
            Write-Output $stdout.Result
            Write-Output $stderr.Result
            if ($process.ExitCode -ne 0) { throw ($test.Name + ' tests failed: ' + $process.ExitCode) }
        } finally { $process.Dispose() }
    }
} finally { Remove-Item -LiteralPath $fixture -Recurse -Force }
