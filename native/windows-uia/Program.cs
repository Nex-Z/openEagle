using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace OpenEagle.Uia;
internal sealed record Target(string Hwnd, int ProcessId, string? StartTicks = null, string? RootId = null);
internal sealed record Request(int Version, string RequestId, string Operation, Target? Target, string? AutomationId = null, string? ExpectedFingerprint = null, string? Text = null);
internal sealed record Snapshot(string AutomationId, string ElementId, string IdentityHash, string ValueHash, string Fingerprint);
internal sealed class Rejected(string code) : Exception(code);
internal static class Policy {
 public static void Require(bool condition, string code) { if (!condition) throw new Rejected(code); }
 public static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
 public static void Writable(bool enabled, bool password, bool readOnly, bool supportsValue, bool edit) {
  Require(!password,"UIA_PASSWORD_REJECTED"); Require(edit,"UIA_CONTROL_UNSUPPORTED"); Require(enabled,"UIA_DISABLED"); Require(supportsValue,"UIA_VALUE_UNSUPPORTED"); Require(!readOnly,"UIA_READONLY");
 }
 public static void Match(string expected,string actual) => Require(expected==actual,"UIA_STALE_OBSERVATION");
 public static void Text(string? text) { Require(text is not null && text.Length<=10000,"UIA_TEXT_REJECTED"); for(var i=0;i<text!.Length;i++){var c=text[i];Require(c!='\0',"UIA_TEXT_REJECTED");if(char.IsHighSurrogate(c)){Require(i+1<text.Length&&char.IsLowSurrogate(text[++i]),"UIA_TEXT_REJECTED");}else Require(!char.IsLowSurrogate(c),"UIA_TEXT_REJECTED");} }
}
internal sealed class Driver : IDisposable {
 [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
 [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint processId);
 [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr hwnd,uint flags);
 [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
 private readonly UIA3Automation automation = new();
 private static IntPtr Handle(Target target) {
  Policy.Require(ulong.TryParse(target.Hwnd,NumberStyles.None,CultureInfo.InvariantCulture,out var n)&&n>0&&n<=long.MaxValue&&target.ProcessId>0,"UIA_TARGET_REJECTED");
  var hwnd=new IntPtr((long)n);Policy.Require(IsWindow(hwnd)&&GetAncestor(hwnd,2)==hwnd,"UIA_WINDOW_CHANGED");GetWindowThreadProcessId(hwnd,out var pid);Policy.Require(pid==target.ProcessId,"UIA_PROCESS_CHANGED");return hwnd;
 }
 private (AutomationElement Root,Target Bound) Root(Target target,bool bind=false) {
  var hwnd=Handle(target);using var process=Process.GetProcessById(target.ProcessId);var start=process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture);
  var root=automation.FromHandle(hwnd);Policy.Require(root.Properties.ProcessId.Value==target.ProcessId,"UIA_PROCESS_CHANGED");var id=string.Join(",",root.Properties.RuntimeId.Value);
  if(!bind){Policy.Require(target.StartTicks==start&&target.RootId==id,"UIA_TARGET_REUSED");}
  return(root,new Target(target.Hwnd,target.ProcessId,start,id));
 }
 public Target Bind(Target target)=>Root(target,true).Bound;
 private AutomationElement Find(Target target,string automationId) {
  Policy.Require(automationId.Length is >0 and <=128,"UIA_SELECTOR_REJECTED");var root=Root(target).Root;var walker=automation.TreeWalkerFactory.GetControlViewWalker();var queue=new Queue<(AutomationElement,int)>();queue.Enqueue((root,0));AutomationElement? found=null;var visited=0;
  while(queue.Count>0){var(node,depth)=queue.Dequeue();Policy.Require(++visited<=256,"UIA_SEARCH_LIMIT");Policy.Require(node.Properties.ProcessId.Value==target.ProcessId,"UIA_CROSS_PROCESS_REJECTED");if(node.Properties.AutomationId.ValueOrDefault==automationId){Policy.Require(found is null,"UIA_SELECTOR_AMBIGUOUS");found=node;}
   var child=walker.GetFirstChild(node);if(child is not null)Policy.Require(depth<8,"UIA_SEARCH_LIMIT");while(child is not null){Policy.Require(queue.Count+visited<256,"UIA_SEARCH_LIMIT");queue.Enqueue((child,depth+1));child=walker.GetNextSibling(child);}
  }
  Policy.Require(found is not null,"UIA_CONTROL_NOT_FOUND");return found!;
 }
 private static Snapshot Read(Target target,AutomationElement element,string automationId) {
  var password=element.Properties.IsPassword.Value;Policy.Require(!password,"UIA_PASSWORD_REJECTED");var supported=element.Patterns.Value.IsSupported;
  Policy.Writable(element.Properties.IsEnabled.Value,password,supported&&element.Patterns.Value.Pattern.IsReadOnly.Value,supported,element.Properties.ControlType.Value==ControlType.Edit);
  var value=element.Patterns.Value.Pattern.Value.Value??string.Empty;Policy.Require(value.Length<=10000,"UIA_VALUE_TOO_LARGE");var id=string.Join(",",element.Properties.RuntimeId.Value);var identity=Policy.Hash(JsonSerializer.Serialize(new {target.Hwnd,target.ProcessId,target.StartTicks,target.RootId,automationId,id}));var hash=Policy.Hash(value);return new Snapshot(automationId,id,identity,hash,Policy.Hash(identity+":"+hash));
 }
 public Snapshot Inspect(Target target,string automationId)=>Read(target,Find(target,automationId),automationId);
 public Snapshot Set(Target target,string automationId,string expected,string text,Action beforeWrite,Action afterWrite) {
  Policy.Text(text);var element=Find(target,automationId);var before=Read(target,element,automationId);Policy.Match(expected,before.Fingerprint);var foreground=GetForegroundWindow();Root(target);beforeWrite();element.Patterns.Value.Pattern.SetValue(text);afterWrite();
  Root(target);var after=Read(target,Find(target,automationId),automationId);Policy.Require(after.IdentityHash==before.IdentityHash,"UIA_CONTROL_CHANGED");Policy.Require(after.ValueHash==Policy.Hash(text),"UIA_VALUE_MISMATCH");Policy.Require(GetForegroundWindow()==foreground,"UIA_FOCUS_CHANGED");return after;
 }
 public void Dispose()=>automation.Dispose();
}
internal static class Program {
 private static readonly JsonSerializerOptions Json=new(){PropertyNamingPolicy=JsonNamingPolicy.CamelCase,UnmappedMemberHandling=JsonUnmappedMemberHandling.Disallow};
 [MTAThread] private static int Main(string[] args) {
  Console.InputEncoding=new UTF8Encoding(false,true);Console.OutputEncoding=new UTF8Encoding(false);
  if(args.SequenceEqual(new[]{"--owned-fixture"}))return OwnedFixture.Run();
  if(args.SequenceEqual(new[]{"--owned-foreground-fixture"}))return OwnedFixture.Run(true);
  if(args.SequenceEqual(new[]{"--health"})){Console.WriteLine(JsonSerializer.Serialize(new {version=1,backend="FlaUI.UIA3",packageVersion="5.0.0",platform="windows",protocol="single-request-stdio"},Json));return 0;}
  if(args.SequenceEqual(new[]{"--self-test"}))return SelfTests.Run();
  string? requestId=null;var mayHaveChanged=false;
  try {
   Policy.Require(args.Length==0,"UIA_ARGUMENT_REJECTED");var input=new StringBuilder();while(true){var c=Console.In.Read();if(c<0||c=='\n')break;Policy.Require(input.Length<65536,"UIA_REQUEST_TOO_LARGE");input.Append((char)c);}
   var request=JsonSerializer.Deserialize<Request>(input.ToString(),Json)??throw new Rejected("UIA_BAD_REQUEST");requestId=request.RequestId;
   Policy.Require(request.Version==1&&Guid.TryParse(requestId,out _)&&request.Target is not null,"UIA_BAD_REQUEST");Policy.Require(request.Operation is "bind" or "inspect" or "set","UIA_OPERATION_REJECTED");
   if(request.Operation!="bind")Policy.Require(request.Target!.StartTicks is not null&&request.Target.RootId is not null&&request.AutomationId is {Length:>0 and <=128},"UIA_BINDING_REQUIRED");
   if(request.Operation=="set"){Policy.Text(request.Text);Policy.Require(request.ExpectedFingerprint is {Length:64},"UIA_OBSERVATION_REQUIRED");}
   void Trace(string phase){try{Console.Error.WriteLine(JsonSerializer.Serialize(new {kind="uia-phase",requestId,phase},Json));}catch(IOException){/* Diagnostics must not change provider execution. */}}
   Trace("request-validated");
   // Do not instantiate UIA for malformed requests. Never enumerate the desktop root.
   using var driver=new Driver();object result=request.Operation switch {"bind"=>driver.Bind(request.Target!),"inspect"=>driver.Inspect(request.Target!,request.AutomationId!),"set"=>driver.Set(request.Target!,request.AutomationId!,request.ExpectedFingerprint!,request.Text!,()=>{mayHaveChanged=true;Trace("set-call-boundary");},()=>Trace("set-returned")),_=>throw new Rejected("UIA_OPERATION_REJECTED")};
   Trace("operation-returned");
   Console.WriteLine(JsonSerializer.Serialize(new {version=1,requestId,ok=true,result},Json));return 0;
  }catch(Exception error){Console.WriteLine(JsonSerializer.Serialize(new {version=1,requestId,ok=false,error=new {code=error is Rejected?error.Message:"UIA_PROVIDER_ERROR",mayHaveChanged}},Json));return 0;}
 }
}
