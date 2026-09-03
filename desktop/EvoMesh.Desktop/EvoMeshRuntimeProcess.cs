using System.Diagnostics;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace EvoMesh.Desktop;

internal sealed class EvoMeshRuntimeProcess : IDisposable
{
    private const string ControlHost = "127.0.0.1";
    private const int DefaultControlPort = 8765;

    /// <summary>The mesh exits with this when it wants to come back up on new code.</summary>
    private const int RestartExitCode = 86;

    /// <summary>How often a connected mesh is asked whether it is still there.</summary>
    private static readonly TimeSpan PingInterval = TimeSpan.FromSeconds(3);

    /// <summary>
    /// How often a mesh that is *not* connected is looked for again. Without this
    /// the Control Center answers the first probe at startup and then never asks
    /// again, so it goes on reporting STOPPED at a mesh that is plainly running --
    /// one started from the launcher script, or one that restarted itself into a
    /// new generation.
    /// </summary>
    private static readonly TimeSpan ProbeInterval = TimeSpan.FromSeconds(5);

    private readonly string _requestedUvExecutable;
    private readonly SemaphoreSlim _requestLock = new(1, 1);
    private readonly object _logLock = new();
    private readonly string _logPath;
    private readonly int _controlPort;
    private readonly CancellationTokenSource _healthCancellation = new();
    private Process? _process;
    private TcpClient? _client;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private bool _isRunning;
    private bool _restarting;
    private bool _stopRequested;

    /// <summary>
    /// Cursor into the mesh's announcement log (goal progress, promotions,
    /// restarts). The control connection is request-response only -- one
    /// client's /restart reply must never carry another client's
    /// announcement -- so this is polled with /notifications instead of
    /// being pushed to, the same way Telegram is pushed to on the mesh side.
    /// </summary>
    private long _lastNotificationId;

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
    public DateTimeOffset? LastHealthCheck { get; private set; }

    public event Action<string>? OutputReceived;
    public event Action<bool>? RunningChanged;

    /// <summary>Raised after every health check, connected or not, with when it ran.</summary>
    public event Action<bool, DateTimeOffset>? HealthChecked;

    /// <summary>
    /// Begins the loop that keeps <see cref="IsRunning"/> true to reality. It runs
    /// for the lifetime of the object rather than only while connected, which is
    /// the whole point: a disconnected Control Center has to keep looking.
    /// </summary>
    public void StartHealthLoop()
    {
        var cancellationToken = _healthCancellation.Token;
        _ = Task.Run(async () =>
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(IsRunning ? PingInterval : ProbeInterval, cancellationToken);
                    await CheckHealthAsync(cancellationToken);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception exc)
                {
                    Log($"Health loop error: {exc.Message}");
                }
            }
        }, cancellationToken);
    }

    private async Task CheckHealthAsync(CancellationToken cancellationToken)
    {
        if (IsRunning)
        {
            try
            {
                await RequestAsync("/ping", cancellationToken);
                await PollNotificationsAsync(cancellationToken);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch
            {
                // The mesh went away. Drop the dead connection and let the next
                // pass of this same loop start looking for it again.
                Disconnect(notify: true);
                Emit("[the mesh stopped answering; watching for it to come back]");
            }
        }
        else if (!_stopRequested)
        {
            await TryAttachAsync(announce: false);
            if (IsRunning)
            {
                Emit("[reconnected to the running mesh]");
            }
        }
        LastHealthCheck = DateTimeOffset.Now;
        HealthChecked?.Invoke(IsRunning, LastHealthCheck.Value);
    }

    /// <summary>
    /// Pulls whatever the mesh has announced on its own since the last poll --
    /// a notified goal's progress, a promotion, a restart notice -- and
    /// prints each line. /notifications returns "id\tISO-timestamp\ttext" per
    /// line and nothing at all when there is nothing new past the cursor.
    /// </summary>
    private async Task PollNotificationsAsync(CancellationToken cancellationToken)
    {
        var response = await RequestAsync($"/notifications {_lastNotificationId}", cancellationToken);
        if (response.Output.StartsWith("No new notifications", StringComparison.Ordinal))
        {
            return;
        }
        foreach (var line in response.Output.Split('\n', StringSplitOptions.RemoveEmptyEntries))
        {
            var parts = line.Split('\t', 3);
            if (parts.Length != 3 || !long.TryParse(parts[0], out var id))
            {
                continue;
            }
            _lastNotificationId = Math.Max(_lastNotificationId, id);
            Emit($"[notice] {parts[2]}");
        }
    }

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
            // A fresh attach is a fresh session with whatever mesh process is
            // on the other end -- its own announcement ids start at 1, so a
            // cursor left over from a previous process would skip everything
            // until ids caught back up past it.
            _lastNotificationId = 0;
            SetRunning(true);
            if (announce)
            {
                Emit("[attached to the running mesh]");
            }
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
        _stopRequested = false;
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
        _process.Exited += (_, _) => OnProcessExited(_process?.ExitCode ?? -1);

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
        _stopRequested = true;
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
        _healthCancellation.Cancel();
        _healthCancellation.Dispose();
        Disconnect(notify: false);
        if (_process is null || _process.HasExited)
        {
            _process?.Dispose();
        }
        _requestLock.Dispose();
    }

    /// <summary>
    /// Reacts to the runtime going away. Exit code 86 is the mesh asking to be
    /// brought back up on the generation it just landed in the tree -- the whole
    /// point of applying one -- so it is restarted here rather than reported as a
    /// crash and left to a human.
    /// </summary>
    private void OnProcessExited(int exitCode)
    {
        Disconnect(notify: true);
        if (exitCode == RestartExitCode && !_stopRequested)
        {
            EmitAndLog("[the mesh landed a new generation and is restarting into it]");
            _ = RestartAsync();
            return;
        }
        EmitAndLog($"[runtime process exited with code {exitCode}]");
    }

    private async Task RestartAsync()
    {
        if (_restarting)
        {
            return;
        }
        _restarting = true;
        try
        {
            // The old process has to finish releasing the control port before the
            // new one can bind it; without the pause the restart fails on a port
            // that is still in TIME_WAIT and the mesh stays down.
            await Task.Delay(TimeSpan.FromSeconds(2));
            var previous = _process;
            _process = null;
            previous?.Dispose();
            await StartAsync();
            Emit("[the mesh is back up on its new generation]");
        }
        catch (Exception exc)
        {
            EmitAndLog($"[the automatic restart failed: {exc.Message}]");
        }
        finally
        {
            _restarting = false;
        }
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
