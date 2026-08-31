using System.Diagnostics;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace EvoMesh.Desktop;

internal sealed class EvoMeshRuntimeProcess : IDisposable
{
    private const string ControlHost = "127.0.0.1";
    private const int DefaultControlPort = 8765;
    private readonly string _requestedUvExecutable;
    private readonly SemaphoreSlim _requestLock = new(1, 1);
    private readonly object _logLock = new();
    private readonly string _logPath;
    private readonly int _controlPort;
    private Process? _process;
    private TcpClient? _client;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private CancellationTokenSource? _monitorCancellation;
    private bool _isRunning;

    public EvoMeshRuntimeProcess(string rootPath, string uvExecutable, int controlPort = DefaultControlPort)
    {
        RootPath = rootPath;
        _requestedUvExecutable = uvExecutable;
        _controlPort = controlPort;
        _logPath = Path.Combine(rootPath, ".runtime", "logs", "control-center.log");
    }

    public string RootPath { get; }
    public string LogPath => _logPath;
    public bool IsRunning => _isRunning;

    public event Action<string>? OutputReceived;
    public event Action<bool>? RunningChanged;

    public async Task<bool> TryAttachAsync(bool announce = true)
    {
        if (IsRunning)
        {
            return true;
        }

        var client = new TcpClient();
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromMilliseconds(800));
            await client.ConnectAsync(ControlHost, _controlPort, timeout.Token);
            var stream = client.GetStream();
            _client = client;
            _reader = new StreamReader(stream, new UTF8Encoding(false), leaveOpen: true);
            _writer = new StreamWriter(stream, new UTF8Encoding(false), leaveOpen: true)
            {
                AutoFlush = true,
            };
            var response = await RequestAsync("/ping", timeout.Token);
            if (!response.Output.Contains("EvoMesh control ready", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("The local control port belongs to another application.");
            }
            SetRunning(true);
            if (announce)
            {
                Emit("[attached to the running mesh]");
            }
            StartMonitor();
            return true;
        }
        catch
        {
            client.Dispose();
            Disconnect(notify: false);
            return false;
        }
    }

    public async Task StartAsync()
    {
        if (await TryAttachAsync())
        {
            return;
        }

        EnsureConfiguration();
        var uvExecutable = ResolveUvExecutable();
        var configPath = Path.Combine(RootPath, "evomesh.yaml");
        var meshLogPath = Path.Combine(RootPath, ".runtime", "logs", "mesh.log");
        var startInfo = new ProcessStartInfo
        {
            FileName = uvExecutable,
            WorkingDirectory = RootPath,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var argument in new[]
        {
            "run", "--locked", "--no-dev", "evomesh", "--config", configPath,
            "--headless", "--control-host", ControlHost, "--control-port", _controlPort.ToString(),
            "--log-file", meshLogPath
        })
        {
            startInfo.ArgumentList.Add(argument);
        }
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        startInfo.Environment["UV_CACHE_DIR"] = Path.Combine(RootPath, ".runtime", "uv-cache");
        startInfo.Environment["UV_PYTHON_INSTALL_DIR"] = Path.Combine(RootPath, ".runtime", "python");

        Directory.CreateDirectory(Path.GetDirectoryName(_logPath)!);
        Log($"Starting with: {uvExecutable}");
        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        _process.OutputDataReceived += (_, args) => EmitAndLog(args.Data);
        _process.ErrorDataReceived += (_, args) => EmitAndLog(args.Data);
        _process.Exited += (_, _) =>
        {
            EmitAndLog($"[runtime process exited with code {_process?.ExitCode}]");
            Disconnect(notify: true);
        };

        try
        {
            if (!_process.Start())
            {
                throw new InvalidOperationException("Unable to start the EvoMesh process.");
            }
            _process.BeginOutputReadLine();
            _process.BeginErrorReadLine();
            Emit($"[started with {uvExecutable}]");

            for (var attempt = 0; attempt < 80; attempt++)
            {
                if (_process.HasExited)
                {
                    throw new InvalidOperationException(
                        $"EvoMesh exited with code {_process.ExitCode}. See {_logPath}");
                }
                if (await TryAttachAsync(announce: false))
                {
                    Emit("[mesh control connection ready]");
                    return;
                }
                await Task.Delay(250);
            }
            throw new TimeoutException($"EvoMesh did not open its control connection. See {_logPath}");
        }
        catch (Exception exc)
        {
            Log($"Start failed: {exc}");
            throw new InvalidOperationException($"{exc.Message}{Environment.NewLine}Log: {_logPath}", exc);
        }
    }

    public async Task SendAsync(string command)
    {
        if (!IsRunning)
        {
            throw new InvalidOperationException("Start EvoMesh before sending commands.");
        }
        try
        {
            var response = await RequestAsync(command, CancellationToken.None);
            Emit(response.Output);
            if (!response.Running)
            {
                Disconnect(notify: true);
            }
        }
        catch (Exception exc)
        {
            Disconnect(notify: true);
            throw new InvalidOperationException("The mesh control connection was lost.", exc);
        }
    }

    public async Task StopAsync()
    {
        if (!IsRunning)
        {
            return;
        }
        await SendAsync("/exit");
        var process = _process;
        if (process is { HasExited: false })
        {
            try
            {
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await process.WaitForExitAsync(timeout.Token);
            }
            catch (OperationCanceledException)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
            }
        }
        Disconnect(notify: true);
    }

    public void Dispose()
    {
        Disconnect(notify: false);
        if (_process is null || _process.HasExited)
        {
            _process?.Dispose();
        }
        _requestLock.Dispose();
    }

    private async Task<ControlResponse> RequestAsync(string command, CancellationToken cancellationToken)
    {
        await _requestLock.WaitAsync(cancellationToken);
        try
        {
            if (_writer is null || _reader is null)
            {
                throw new InvalidOperationException("The control connection is not available.");
            }
            var request = JsonSerializer.Serialize(new { command });
            await _writer.WriteLineAsync(request.AsMemory(), cancellationToken);
            var line = await _reader.ReadLineAsync(cancellationToken);
            if (line is null)
            {
                throw new IOException("The control connection was closed.");
            }
            return JsonSerializer.Deserialize<ControlResponse>(line, JsonOptions)
                ?? throw new InvalidDataException("The mesh returned an empty control response.");
        }
        finally
        {
            _requestLock.Release();
        }
    }

    private void StartMonitor()
    {
        _monitorCancellation?.Cancel();
        _monitorCancellation = new CancellationTokenSource();
        var cancellationToken = _monitorCancellation.Token;
        _ = Task.Run(async () =>
        {
            try
            {
                while (!cancellationToken.IsCancellationRequested && IsRunning)
                {
                    await Task.Delay(TimeSpan.FromSeconds(3), cancellationToken);
                    await RequestAsync("/ping", cancellationToken);
                }
            }
            catch (OperationCanceledException) { }
            catch
            {
                Disconnect(notify: true);
            }
        }, cancellationToken);
    }

    private string ResolveUvExecutable()
    {
        var candidates = new List<string>();
        if (Path.IsPathRooted(_requestedUvExecutable))
        {
            candidates.Add(_requestedUvExecutable);
        }
        else
        {
            foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? "")
                .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            {
                candidates.Add(Path.Combine(directory.Trim('"'), "uv.exe"));
            }
            candidates.Add(Path.GetFullPath(Path.Combine(RootPath, "..", ".tools", "uv", "bin", "uv.exe")));
            var userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            candidates.Add(Path.Combine(userProfile, ".local", "bin", "uv.exe"));
            candidates.Add(Path.Combine(localAppData, "Microsoft", "WinGet", "Links", "uv.exe"));
        }
        var match = candidates.FirstOrDefault(File.Exists);
        if (match is not null)
        {
            return Path.GetFullPath(match);
        }
        throw new FileNotFoundException(
            $"uv.exe was not found. Install uv and restart the Control Center. Log: {_logPath}");
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

    private void Disconnect(bool notify)
    {
        _monitorCancellation?.Cancel();
        _monitorCancellation?.Dispose();
        _monitorCancellation = null;
        _writer?.Dispose();
        _reader?.Dispose();
        _client?.Dispose();
        _writer = null;
        _reader = null;
        _client = null;
        SetRunning(false, notify);
    }

    private void SetRunning(bool value, bool notify = true)
    {
        if (_isRunning == value)
        {
            return;
        }
        _isRunning = value;
        if (notify)
        {
            RunningChanged?.Invoke(value);
        }
    }

    private void EmitAndLog(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return;
        }
        Log(value);
        Emit(value);
    }

    private void Emit(string? value)
    {
        if (!string.IsNullOrWhiteSpace(value))
        {
            OutputReceived?.Invoke(value);
        }
    }

    private void Log(string value)
    {
        lock (_logLock)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_logPath)!);
            File.AppendAllText(
                _logPath,
                $"{DateTimeOffset.Now:O} {value}{Environment.NewLine}",
                Encoding.UTF8);
        }
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private sealed record ControlResponse(string Output, bool Running, bool Shutdown = false, bool Error = false);
}
