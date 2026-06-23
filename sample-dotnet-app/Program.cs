// Sample guest application that implements Lambda MicroVMs lifecycle hooks.
//
// Listens on port 8080. /execute interprets C# source via Roslyn scripting
// (CSharpScript), redirecting Console.Out / Console.Error so snippet output
// is captured and returned in the JSON response.

using Microsoft.CodeAnalysis.CSharp.Scripting;
using Microsoft.CodeAnalysis.Scripting;

const string BasePath = "/aws/lambda-microvms/runtime/v1";
const int Port = 8080;

string? microVmId = null;

string NowTs() => DateTime.UtcNow.ToString("o");
void Log(string msg) => Console.WriteLine($"{NowTs()} - INFO - [sample-dotnet-app] {msg}");

Log($"Starting sample guest application on port {Port}");

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.UseUrls($"http://0.0.0.0:{Port}");
var app = builder.Build();

app.MapGet("/health", () =>
{
    Log($"Health check called [ts={NowTs()}, microVmId={microVmId}]");
    return Results.Json(new { status = "healthy" });
});

app.MapPost($"{BasePath}/validate", () =>
{
    Log($"Validate hook called [ts={NowTs()}, microVmId={microVmId}]");
    return Results.Ok();
});

app.MapPost($"{BasePath}/ready", () =>
{
    Log($"Ready hook called [ts={NowTs()}, microVmId={microVmId}]");
    return Results.Ok();
});

app.MapPost($"{BasePath}/run", async (HttpRequest req) =>
{
    var data = await req.ReadFromJsonAsync<RunRequest>() ?? new RunRequest();
    microVmId = data.MicroVmId;
    Log($"Run hook called — ts={NowTs()}, microVmId={microVmId}, meshIpv6Address={data.MeshIpv6Address}");
    return Results.Ok();
});

app.MapPost($"{BasePath}/resume", () =>
{
    Log($"Resume hook called [ts={NowTs()}, microVmId={microVmId}]");
    return Results.Ok();
});

app.MapPost($"{BasePath}/suspend", () =>
{
    Log($"Suspend hook called [ts={NowTs()}, microVmId={microVmId}]");
    return Results.Ok();
});

app.MapPost($"{BasePath}/terminate", () =>
{
    Log($"Terminate hook called [ts={NowTs()}, microVmId={microVmId}]");
    return Results.Ok();
});

app.MapPost("/execute", async (HttpRequest req) =>
{
    var data = await req.ReadFromJsonAsync<ExecuteRequest>() ?? new ExecuteRequest();
    if (string.IsNullOrEmpty(data.Code))
    {
        return Results.Json(new { error = "No code provided" }, statusCode: 400);
    }

    Log($"Execute called [ts={NowTs()}, microVmId={microVmId}]");

    var stdout = new StringWriter();
    var stderr = new StringWriter();
    var oldOut = Console.Out;
    var oldErr = Console.Error;
    Console.SetOut(stdout);
    Console.SetError(stderr);

    try
    {
        var options = ScriptOptions.Default
            .WithReferences(
                typeof(object).Assembly,
                typeof(Console).Assembly,
                typeof(System.Linq.Enumerable).Assembly,
                typeof(System.Collections.Generic.List<int>).Assembly,
                typeof(System.IO.File).Assembly)
            .WithImports("System", "System.Linq", "System.Collections.Generic", "System.IO");
        await CSharpScript.RunAsync(data.Code, options);
        return Results.Json(new
        {
            success = true,
            output = stdout.ToString(),
            stderr = stderr.ToString()
        });
    }
    catch (Exception ex)
    {
        return Results.Json(new
        {
            success = false,
            error = ex.ToString(),
            stderr = stderr.ToString()
        });
    }
    finally
    {
        Console.SetOut(oldOut);
        Console.SetError(oldErr);
    }
});

PrintSampleCommands();
app.Run();

void PrintSampleCommands()
{
    Console.WriteLine($@"
Sample commands (server running on port {Port}):

  curl http://127.0.0.1:{Port}/health

  curl -X POST http://127.0.0.1:{Port}{BasePath}/ready

  curl -X POST http://127.0.0.1:{Port}{BasePath}/run \
    -H 'Content-Type: application/json' \
    -d '{{""microVmId"": ""hello_world"", ""meshIpv6Address"": ""::1""}}'

  curl -X POST http://127.0.0.1:{Port}{BasePath}/resume
  curl -X POST http://127.0.0.1:{Port}{BasePath}/suspend
  curl -X POST http://127.0.0.1:{Port}{BasePath}/terminate

  curl -X POST http://127.0.0.1:{Port}/execute \
    -H 'Content-Type: application/json' \
    -d '{{""code"": ""Console.WriteLine(1 + 1);""}}'
");
}

public record RunRequest(string? MicroVmId = null, string? MeshIpv6Address = null);
public record ExecuteRequest(string? Code = null);
