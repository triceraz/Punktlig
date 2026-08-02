# Hold the machine awake only while a Punktlig job is actually running.
#
# Sleep is otherwise wanted: the Claude app's execution request is overridden
# so an idle desktop suspends normally. But an idle desktop is exactly what a
# long dataset build or training run looks like to Windows, which counts user
# input, not CPU load. Without this the machine would suspend mid-job.
#
# Only ES_SYSTEM_REQUIRED is set, never ES_DISPLAY_REQUIRED, so the screens
# still switch off on their own timer. The request is released the moment the
# job exits, which is what makes this safe to leave running.

param(
    [string]$ProcessName = "python",
    [int]$GraceSeconds = 90
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

try {
    [Awake]::Hold()
    "$(& $stamp)  holder maskinen vaaken mens '$ProcessName' kjorer"

    # A grace period covers the gap between two chained jobs, so the build
    # finishing and the training starting does not drop the request.
    $idleSince = $null
    while ($true) {
        $running = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
        if ($running.Count -gt 0) {
            $idleSince = $null
        }
        elseif ($null -eq $idleSince) {
            $idleSince = Get-Date
        }
        elseif (((Get-Date) - $idleSince).TotalSeconds -ge $GraceSeconds) {
            "$(& $stamp)  ingen '$ProcessName' paa $GraceSeconds s, slipper vaakelaasen"
            break
        }
        Start-Sleep -Seconds 15
    }
}
finally {
    [Awake]::Release()
}
