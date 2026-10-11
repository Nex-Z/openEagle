param([Parameter(Mandatory=$true)][long]$TargetHandle)
$ErrorActionPreference='Stop'
# 仅诊断系统元数据；不包含输入、激活、切桌面或提权 API。
Add-Type -TypeDefinition @"
using System; using System.Text; using System.Runtime.InteropServices;
public static class GuiWindowReadOnly {
 [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h,uint flags);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);
 [DllImport("user32.dll")] public static extern IntPtr GetThreadDesktop(uint tid);
 [DllImport("user32.dll")] public static extern IntPtr GetProcessWindowStation();
 [DllImport("user32.dll",SetLastError=true)] public static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
 [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr h);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern bool GetUserObjectInformation(IntPtr h,int index,StringBuilder value,uint length,out uint needed);
 public static string ObjectName(IntPtr h) {var s=new StringBuilder(256); uint needed; return h==IntPtr.Zero?null:GetUserObjectInformation(h,2,s,512,out needed)?s.ToString():null;}
}
"@
$target=[IntPtr]$TargetHandle
[uint32]$targetProcessId=0
$targetThreadId=[GuiWindowReadOnly]::GetWindowThreadProcessId($target,[ref]$targetProcessId)
$foreground=[GuiWindowReadOnly]::GetForegroundWindow()
[uint32]$foregroundProcessId=0
$foregroundThreadId=[GuiWindowReadOnly]::GetWindowThreadProcessId($foreground,[ref]$foregroundProcessId)
$inputDesktop=[GuiWindowReadOnly]::OpenInputDesktop(0,$false,1)
$inputError=if($inputDesktop -eq [IntPtr]::Zero){[Runtime.InteropServices.Marshal]::GetLastWin32Error()}else{0}
try {
 [pscustomobject]@{targetHandle=$TargetHandle;isWindow=[GuiWindowReadOnly]::IsWindow($target);isVisible=[GuiWindowReadOnly]::IsWindowVisible($target);rootHandle=[GuiWindowReadOnly]::GetAncestor($target,2).ToInt64();targetPid=$targetProcessId;targetThread=$targetThreadId;targetSession=(Get-Process -Id $targetProcessId).SessionId;diagnosticSession=(Get-Process -Id $PID).SessionId;targetDesktop=[GuiWindowReadOnly]::ObjectName([GuiWindowReadOnly]::GetThreadDesktop($targetThreadId));diagnosticWindowStation=[GuiWindowReadOnly]::ObjectName([GuiWindowReadOnly]::GetProcessWindowStation());inputDesktop=[GuiWindowReadOnly]::ObjectName($inputDesktop);inputDesktopError=$inputError;foregroundHandle=$foreground.ToInt64();foregroundPid=$foregroundProcessId;foregroundSession=if($foregroundProcessId){(Get-Process -Id $foregroundProcessId).SessionId}else{$null};foregroundDesktop=[GuiWindowReadOnly]::ObjectName([GuiWindowReadOnly]::GetThreadDesktop($foregroundThreadId))} | ConvertTo-Json -Compress
} finally {if($inputDesktop -ne [IntPtr]::Zero){[void][GuiWindowReadOnly]::CloseDesktop($inputDesktop)}}
