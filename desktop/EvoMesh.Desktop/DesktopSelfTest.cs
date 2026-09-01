namespace EvoMesh.Desktop;

internal static class DesktopSelfTest
{
    public static void Run(string rootPath)
    {
        var example = Path.Combine(rootPath, "evomesh.yaml.example");
        if (!File.Exists(example))
        {
            throw new FileNotFoundException("EvoMesh settings example was not found.", example);
        }
        var temporary = Path.Combine(Path.GetTempPath(), $"evomesh-desktop-{Guid.NewGuid():N}.yaml");
        try
        {
            File.Copy(example, temporary);
            var settings = EvoMeshYamlSettings.Load(temporary);
            if (settings.DefaultProvider != "ollama" || !settings.Providers.ContainsKey("ollama"))
            {
                throw new InvalidOperationException("Settings loader did not read the Ollama provider.");
            }
            settings.EnvironmentName = "desktop-self-test";
            settings.Providers["ollama"] = settings.Providers["ollama"] with
            {
                Model = "qwen3:14b"
            };
            settings.SystemAgents["guardian"] = new AgentModelEditorSettings(
                "ollama",
                "qwen3:14b");
            settings.Save(temporary);
            var saved = EvoMeshYamlSettings.Load(temporary);
            if (saved.EnvironmentName != "desktop-self-test" ||
                saved.Providers["ollama"].Model != "qwen3:14b" ||
                saved.SystemAgents["guardian"].Model != "qwen3:14b")
            {
                throw new InvalidOperationException("Settings round-trip failed.");
            }
        }
        finally
        {
            if (File.Exists(temporary)) File.Delete(temporary);
        }
        using var form = new MainForm(rootPath, "uv");
        form.ValidateUiForTest();
    }

    public static async Task RunControlAsync(string rootPath, string uvExecutable)
    {
        using var runtime = new EvoMeshRuntimeProcess(rootPath, uvExecutable, controlPort: 18765);
        var output = new List<string>();
        runtime.OutputReceived += output.Add;
        await runtime.StartAsync();
        if (!runtime.IsRunning)
        {
            throw new InvalidOperationException("Control Center did not connect to the mesh.");
        }
        await runtime.SendAsync("/status");
        if (!output.Any(line => line.Contains("status: READY", StringComparison.Ordinal)))
        {
            throw new InvalidOperationException("Control Center did not receive mesh status.");
        }
        await runtime.StopAsync();
        if (runtime.IsRunning)
        {
            throw new InvalidOperationException("Control Center did not stop the mesh.");
        }
    }
}
