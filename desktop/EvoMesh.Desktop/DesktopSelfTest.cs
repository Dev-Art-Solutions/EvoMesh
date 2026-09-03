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
                Model = "qwen3:14b",
                NumCtx = 32768,
            };
            // The colon is deliberate: an Ollama model tag routinely has one
            // (name:tag), and it is exactly what a naive first-colon split on
            // the mapping key gets wrong.
            settings.Providers["ollama"].ModelNumCtx["ornith-1.5:35b-128k"] = 131072;
            settings.SystemAgents["guardian"] = new AgentModelEditorSettings(
                "ollama",
                "qwen3:14b",
                4096);
            settings.Harness.Enabled = true;
            settings.Harness.AllowWrite = true;
            settings.Harness.TranscriptChars = 9000;
            settings.Runtime.CycleSeconds = 45;
            settings.Runtime.MemoryChars = 2222;
            settings.Evolution.Autonomous = false;
            settings.Evolution.AutoPromote = true;
            settings.Evolution.AutoRestart = false;
            settings.Evolution.Objective = "tighten the console";
            settings.Git.AuthorName = "Mesh Evo Agent";
            settings.Git.Remote = "upstream";
            settings.Telegram.Enabled = true;
            settings.Telegram.Token = "123456:AAHtestTOKENvalue";
            settings.Telegram.AllowedChatIds = "42, 77";
            settings.Save(temporary);
            var saved = EvoMeshYamlSettings.Load(temporary);
            if (saved.EnvironmentName != "desktop-self-test" ||
                saved.Providers["ollama"].Model != "qwen3:14b" ||
                saved.SystemAgents["guardian"].Model != "qwen3:14b")
            {
                throw new InvalidOperationException("Settings round-trip failed.");
            }
            // num_ctx exists because Ollama's own default context (2048 tokens)
            // silently truncates a prompt the mesh already budgeted for -- a
            // save that lost it would reintroduce exactly that bug.
            if (saved.Providers["ollama"].NumCtx != 32768 ||
                !saved.Providers["ollama"].ModelNumCtx.TryGetValue("ornith-1.5:35b-128k", out var tagNumCtx) ||
                tagNumCtx != 131072 ||
                saved.SystemAgents["guardian"].NumCtx != 4096)
            {
                throw new InvalidOperationException("num_ctx settings were lost on save.");
            }
            // Missing entirely (which defaults to off) is the exact shape that
            // left the Evolver logging "the harness is off" every cycle with no
            // way to author a generation -- a save must not reproduce that by
            // silently dropping a block this tab has no controls for.
            if (!saved.Harness.Enabled || !saved.Harness.AllowWrite || saved.Harness.TranscriptChars != 9000)
            {
                throw new InvalidOperationException("harness settings were lost on save.");
            }
            // Saving settings must not silently drop the runtime cadence, the
            // prompt budgets, the workspace path, or the evolution policy.
            if (saved.WorkspacePath != "workspace" ||
                saved.Runtime.CycleSeconds != 45 ||
                saved.Runtime.MemoryChars != 2222 ||
                saved.Runtime.PromptChars != 6000 ||
                saved.Evolution.Autonomous ||
                !saved.Evolution.AutoPromote ||
                saved.Evolution.AutoRestart ||
                saved.Evolution.Objective != "tighten the console")
            {
                throw new InvalidOperationException("Runtime or evolution settings were lost on save.");
            }
            // A token with a colon in it is the shape BotFather actually hands
            // out, and it is the one shape a naive key/value parser truncates.
            if (saved.Git.AuthorName != "Mesh Evo Agent" ||
                saved.Git.Remote != "upstream" ||
                !saved.Telegram.Enabled ||
                saved.Telegram.Token != "123456:AAHtestTOKENvalue" ||
                saved.Telegram.AllowedChatIds != "42, 77")
            {
                throw new InvalidOperationException("Git or Telegram settings were lost on save.");
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
