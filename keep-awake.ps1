# Keep the desktop awake while Punktlig is working, and let it sleep when it
# is not.
#
# The collector is the point of the project: it polls every minute, around the
# clock, and a machine that suspends stops collecting. So while any Punktlig
# python process is alive, the system is held awake.
#
# Only ES_SYSTEM_REQUIRED is set, never ES_DISPLAY_REQUIRED, so the screens
# still switch off on their own timer. The desktop stays dark and quiet; it
# simply does not suspend.
#
# The request is released the moment the last job exits, which is what keeps
# this honest: with nothing running, the machine sleeps normally.
#
# Run with -Persist from a login shortcut to guard the machine continuously.
# Without it, the script exits once nothing has matched for a while, which
# suits guarding a single long job from an interactive session.

param(
    [string]$Pattern = "punktlig",
    [string]$Exclude = "http\.server",
    [int]$GraceSeconds = 90,
    [switch]$Persist
)

Add-Type -Language CSharp @'
using System;
using System.Runtime.InteropServices;
public static class Awake {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint SetThreadExecutionState(uint esFlags);
    const uint ES_CONTINUOUS = 0x80000000;
    const uint ES_SYSTEM_REQUIRED = 0x00000001;
    public static void Hold()    { SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED); }
    public static void Release() { SetThreadExecutionState(ES_CONTINUOUS); }
}
'@

$stamp = { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }

function Running {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern -and
                       $_.CommandLine -notmatch $Exclude }).Count
}

# An S4U scheduled task hides its command line from an unelevated session, so
# the collector is invisible to the check above. Its lock file is not: it is
# held open for as long as the collector runs, and cannot be deleted until it
# stops. Trying to remove it is therefore a reliable liveness test, and a
# harmless one, since a stale file is exactly what should be cleared away.
$collectorLock = Join-Path $env:PUNKTLIG_DATA "collector.lock"
if (-not $env:PUNKTLIG_DATA) { $collectorLock = "D:\punktlig-data\collector.lock" }

function CollectorAlive {
    if (-not (Test-Path $collectorLock)) { return $false }
    try { Remove-Item $collectorLock -ErrorAction Stop; return $false }
    catch { return $true }
}

try {
    $holding = $false
    $idleSince = $null
    while ($true) {
        $busy = ((Running) -gt 0) -or (CollectorAlive)

        if ($busy -and -not $holding) {
            [Awake]::Hold(); $holding = $true
            "$(& $stamp)  holder maskinen vaaken"
        }
        elseif (-not $busy -and $holding) {
            [Awake]::Release(); $holding = $false
            "$(& $stamp)  ingenting kjorer, slipper vaakelaasen"
        }

        if (-not $Persist) {
            if ($busy) { $idleSince = $null }
            elseif ($null -eq $idleSince) { $idleSince = Get-Date }
            elseif (((Get-Date) - $idleSince).TotalSeconds -ge $GraceSeconds) { break }
        }
        Start-Sleep -Seconds 15
    }
}
finally {
    [Awake]::Release()
}
