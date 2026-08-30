using System.Diagnostics;
using System.Text;

namespace EvoMesh.Desktop;

internal sealed class EvoMeshRuntimeProcess : IDisposable
{
    private Process? _process;

    public EvoMeshRuntimeProcess(string rootPath, string uvExecutable)
    {
        RootPath = rootPath;
        UvExecutable = uvExecutable;
    }

    public string RootPath { get; }
    public string UvExecutable { get; }
    public bool IsRunning => _process is { HasExited: false };

    public event Action<string>? OutputReceived;
    public event Action<bool>? RunningChanged;

    public Task StartAsync()
    {
        if (IsRunning)
        {
            return Task.CompletedTask;
        }

        EnsureConfiguration();
        var configPath = Path.Combine(RootPath, "evomesh.yaml");
        var startInfo = new ProcessStartInfo
        {
            FileName = UvExecutable,
            Arguments = $"run evomesh --config \"{configPath}\"",
            WorkingDirectory = RootPath,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";

        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        _process.OutputDataReceived += (_, args) => Emit(args.Data);
        _process.ErrorDataReceived += (_, args) => Emit(args.Data);
        _process.Exited += (_, _) =>
        {
            Emit("[runtime stopped]");
            RunningChanged?.Invoke(false);
        };
        if (!_process.Start())
        {
            throw new InvalidOperationException("Unable to start EvoMesh.");
        }
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();
        Emit($"[started with {UvExecutable}]");
        RunningChanged?.Invoke(true);
        return Task.CompletedTask;
    }

    public async Task SendAsync(string command)
    {
        if (!IsRunning || _process is null)
        {
            throw new InvalidOperationException("Start EvoMesh before sending commands.");
        }
        await _process.StandardInput.WriteLineAsync(command);
        await _process.StandardInput.FlushAsync();
    }

    public async Task StopAsync()
    {
        var process = _process;
        if (process is null || process.HasExited)
        {
            return;
        }
        try
        {
            await process.StandardInput.WriteLineAsync("/exit");
            await process.StandardInput.FlushAsync();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await process.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync();
        }
        finally
        {
            RunningChanged?.Invoke(false);
        }
    }

    public void Dispose()
    {
        if (_process is { HasExited: false })
        {
            _process.Kill(entireProcessTree: true);
        }
        _process?.Dispose();
    }

    private void EnsureConfiguration()
    {
        var config = Path.Combine(RootPath, "evomesh.yaml");
        var example = Path.Combine(RootPath, "evomesh.yaml.example");
        if (!File.Exists(config))
        {
            File.Copy(example, config);
        }
    }

    private void Emit(string? value)
    {
        if (!string.IsNullOrWhiteSpace(value))
        {
            OutputReceived?.Invoke(value);
        }
    }
}
