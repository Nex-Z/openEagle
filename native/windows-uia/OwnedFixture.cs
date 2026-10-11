using System.Diagnostics;
using System.Text.Json;
using System.Windows.Forms;
namespace OpenEagle.Uia;
internal static class OwnedFixture {
 public static int Run(bool foreground=false){
  if(Environment.GetEnvironmentVariable("OPENEAGLE_UIA_FIXTURE_AUTHORIZED")!="1")return 2;
  var thread=new Thread(()=>{
   using var target=new Form{Text=foreground?"openEagle UIA foreground text test":"openEagle UIA background target",Width=680,Height=420};
   using var sentinel=new Form{Text="openEagle UIA foreground sentinel",Width=580,Height=250};
   var entry=new TextBox{Name="entry",Multiline=true,Left=20,Top=40,Width=620,Height=130};
   var readOnly=new TextBox{Name="readonly",ReadOnly=true,Text="readonly fixture",Left=20,Top=190,Width=620};
   var password=new TextBox{Name="password",UseSystemPasswordChar=true,Text="fixture-only",Left=20,Top=230,Width=620};
   var disabled=new TextBox{Name="disabled",Enabled=false,Text="disabled fixture",Left=20,Top=270,Width=620};
   target.Controls.AddRange(new Control[]{new Label{Text=foreground?"Authorized foreground text test: keep this window in front.":"UIA target: leave this window in the background.",Left=20,Top=10,Width=630},entry,readOnly,password,disabled});
   var start=new Button{Name="start",Text="Start one background UIA round",Left=20,Top=70,Width=420};
   var stop=new Button{Name="stop",Text="Stop and close both owned windows",Left=20,Top=120,Width=420};
   sentinel.Controls.AddRange(new Control[]{new Label{Text="Click Start here. Keep this sentinel in front; do not activate the target.",Left=20,Top=15,Width=520,Height=45},start,stop});
   var outputLock=new object();void Emit(object message){lock(outputLock){Console.WriteLine(JsonSerializer.Serialize(message));}}
   using var sampling=new CancellationTokenSource();var clock=Stopwatch.StartNew();var started=0;var stopping=false;long sequence=0;
   if(foreground){target.Height=500;start.Text="Start one foreground text write";start.Top=320;stop.Top=365;stop.Text="Stop and close this test window";target.Controls.Add(start);target.Controls.Add(stop);}
   var targetHandle=target.Handle;var sentinelHandle=foreground?IntPtr.Zero:sentinel.Handle;
   string Role(){var foreground=Driver.GetForegroundWindow();return sentinelHandle!=IntPtr.Zero&&foreground==sentinelHandle?"sentinel":foreground==targetHandle?"target":foreground==IntPtr.Zero?"none":"other";}
   void Sample(){lock(outputLock){Emit(new {kind="sample",sequence=Interlocked.Increment(ref sequence),elapsedMs=clock.Elapsed.TotalMilliseconds,role=Role()});}}
   object State()=>new {started=Volatile.Read(ref started)==1,text=entry.Text,targetHwnd=targetHandle.ToInt64().ToString(),sentinelHwnd=sentinelHandle.ToInt64().ToString(),foregroundRole=Role()};
   void CloseBoth(string reason,Form? alreadyClosing=null){if(stopping)return;stopping=true;sampling.Cancel();Emit(new {kind="stopped",reason});if(sentinel!=alreadyClosing&&!sentinel.IsDisposed)sentinel.Close();if(target!=alreadyClosing&&!target.IsDisposed)target.Close();}
   start.Click+=(_,_)=>{if(Role()!=(foreground?"target":"sentinel"))return;Interlocked.Exchange(ref started,1);start.Enabled=false;Sample();};
   stop.Click+=(_,_)=>CloseBoth("user-stop");target.FormClosing+=(_,_)=>CloseBoth("target-closed",target);sentinel.FormClosing+=(_,_)=>CloseBoth("sentinel-closed",sentinel);
   var sampler=new Thread(()=>{while(!sampling.Token.WaitHandle.WaitOne(20)){if(Volatile.Read(ref started)==1)Sample();}}){IsBackground=true};sampler.Start();
   target.Shown+=(_,_)=>{
    // Show the second owned window once. Never Activate, BringToFront, or send input.
    if(!foreground)sentinel.Show();Emit(new {kind="ready",processId=Environment.ProcessId,targetHwnd=targetHandle.ToInt64().ToString(),sentinelHwnd=sentinelHandle.ToInt64().ToString(),automationIds=new[]{"entry","readonly","password","disabled"},sampleIntervalMs=20});
    var input=new Thread(()=>{try{string? line;while((line=Console.ReadLine()) is not null){var command=JsonDocument.Parse(line);var id=command.RootElement.GetProperty("id").GetString();var operation=command.RootElement.GetProperty("operation").GetString();target.Invoke(()=>{if(operation=="close"){Emit(new {kind="reply",id,state=State()});CloseBoth("runner-close");return;}if(operation=="change")entry.Text="external fixture change";if(operation is not ("inspect" or "change"))throw new InvalidOperationException();Emit(new {kind="reply",id,state=State()});});command.Dispose();}}catch{}finally{try{target.BeginInvoke(()=>CloseBoth("input-closed"));}catch{}}}){IsBackground=true};input.Start();
   };
   try{Application.Run(target);}finally{sampling.Cancel();sampler.Join(1000);}
  });thread.SetApartmentState(ApartmentState.STA);thread.Start();thread.Join();return 0;
 }
}