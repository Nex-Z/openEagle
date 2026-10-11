using System.Text.Json;
using System.Windows.Forms;
namespace OpenEagle.Uia;
internal static class OwnedFixture {
 public static int Run(){if(Environment.GetEnvironmentVariable("OPENEAGLE_UIA_FIXTURE_AUTHORIZED")!="1")return 2;var thread=new Thread(()=>{
  using var form=new Form{Text="openEagle owned UIA acceptance",Width=680,Height=480};
  var entry=new TextBox{Name="entry",Multiline=true,Left=20,Top=40,Width=620,Height=130};
  var readOnly=new TextBox{Name="readonly",ReadOnly=true,Text="readonly fixture",Left=20,Top=190,Width=620};
  var password=new TextBox{Name="password",UseSystemPasswordChar=true,Text="fixture-only",Left=20,Top=230,Width=620};
  var disabled=new TextBox{Name="disabled",Enabled=false,Text="disabled fixture",Left=20,Top=270,Width=620};
  var started=false;var start=new Button{Name="start",Text="Start this one authorized UIA round",Left=20,Top=310,Width=400};start.Click+=(_,_)=>{started=true;start.Enabled=false;};
  var stop=new Button{Name="stop",Text="Stop / close fixture",Left=20,Top=350,Width=300};stop.Click+=(_,_)=>form.Close();
  form.Controls.AddRange(new Control[]{new Label{Text="Explicit owned-window UIA test. No native keys. Stop closes this window.",Left=20,Top=10,Width=630},entry,readOnly,password,disabled,start,stop});
  form.Shown+=(_,_)=>{Console.WriteLine(JsonSerializer.Serialize(new{ready=true,hwnd=form.Handle.ToInt64().ToString(),processId=Environment.ProcessId,automationIds=new[]{"entry","readonly","password","disabled"}}));
   var input=new Thread(()=>{try{string? line;while((line=Console.ReadLine()) is not null){var command=line;form.Invoke(()=>{if(command=="close"){form.Close();return;}if(command=="change")entry.Text="external fixture change";Console.WriteLine(JsonSerializer.Serialize(new{oracle=true,started,text=entry.Text,foregroundOwned=Driver.GetForegroundWindow()==form.Handle}));});}}catch{}}){IsBackground=true};input.Start();};
  Application.Run(form);
 });thread.SetApartmentState(ApartmentState.STA);thread.Start();thread.Join();return 0;}
}