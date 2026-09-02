namespace EvoMesh.Desktop;

internal sealed record ProviderEditorSettings(
    string BaseUrl,
    string Model,
    string ApiKey = "",
    double TimeoutSeconds = 600);
internal sealed record AgentModelEditorSettings(string Provider, string Model);

/// <summary>Runtime cadence and prompt budgets. Small local models need small budgets.</summary>
internal sealed class RuntimeEditorSettings
{
    public int CycleSeconds { get; set; } = 60;
    public double StaggerSeconds { get; set; } = 1.5;
    public int PromptChars { get; set; } = 6000;
    public int MemoryChars { get; set; } = 3000;
    public int ContextChars { get; set; } = 1500;
    public int InboxChars { get; set; } = 1000;
    public int BeliefsChars { get; set; } = 700;
}

internal sealed class EvolutionEditorSettings
{
    public bool Autonomous { get; set; } = true;
    public int CycleSeconds { get; set; } = 300;
    public bool AutoValidate { get; set; } = true;
    public int MaxRepairs { get; set; } = 2;
    public bool AutoPromote { get; set; }
    public bool AutoRestart { get; set; } = true;
    public double RestartDelaySeconds { get; set; } = 5;
    public string Objective { get; set; } = "";
}

/// <summary>Who signs a generation, and where it goes once it lands.</summary>
internal sealed class GitEditorSettings
{
    public string AuthorName { get; set; } = "Mesh Evo Agent";
    public string AuthorEmail { get; set; } = "mesh-evo-agent@evomesh.local";
    public bool AutoPush { get; set; } = true;
    public string Remote { get; set; } = "origin";
    public string Branch { get; set; } = "";
}

/// <summary>The BotFather token and who is allowed to use it.</summary>
internal sealed class TelegramEditorSettings
{
    public bool Enabled { get; set; }
    public string Token { get; set; } = "";
    public string AllowedChatIds { get; set; } = "";
    public bool AdoptFirstChat { get; set; } = true;
    public int PollTimeoutSeconds { get; set; } = 30;
    public bool Announcements { get; set; } = true;
}

internal sealed class EvoMeshYamlSettings
{
    public string EnvironmentName { get; set; } = "local";
    public string DataPath { get; set; } = "data/evomesh.db";
    public string GenerationPath { get; set; } = "generations";
    public string WorkspacePath { get; set; } = "workspace";
    public string LogLevel { get; set; } = "INFO";
    public string DefaultProvider { get; set; } = "ollama";
    public RuntimeEditorSettings Runtime { get; } = new();
    public EvolutionEditorSettings Evolution { get; } = new();
    public GitEditorSettings Git { get; } = new();
    public TelegramEditorSettings Telegram { get; } = new();
    public Dictionary<string, ProviderEditorSettings> Providers { get; } = new(StringComparer.OrdinalIgnoreCase);
    public Dictionary<string, AgentModelEditorSettings> SystemAgents { get; } = new(StringComparer.OrdinalIgnoreCase);

    public static EvoMeshYamlSettings Load(string path)
    {
        var result = new EvoMeshYamlSettings();
        if (!File.Exists(path))
        {
            return result;
        }

        string? section = null;
        string? modelSection = null;
        string? provider = null;
        string? systemAgent = null;
        foreach (var rawLine in File.ReadAllLines(path))
        {
            var indent = rawLine.TakeWhile(char.IsWhiteSpace).Count();
            var line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith('#') || !line.Contains(':'))
            {
                continue;
            }
            var separator = line.IndexOf(':');
            var key = line[..separator].Trim();
            var value = Unquote(line[(separator + 1)..].Trim());

            if (indent == 0)
            {
                section = key;
                modelSection = null;
                provider = null;
                systemAgent = null;
                switch (key)
                {
                    case "environment_name": result.EnvironmentName = value; break;
                    case "data_path": result.DataPath = value; break;
                    case "generation_path": result.GenerationPath = value; break;
                    case "workspace_path": result.WorkspacePath = value; break;
                    case "log_level": result.LogLevel = value; break;
                }
            }
            else if (section == "runtime" && indent >= 2)
            {
                switch (key)
                {
                    case "cycle_seconds": result.Runtime.CycleSeconds = ParseInt(value, result.Runtime.CycleSeconds); break;
                    case "stagger_seconds": result.Runtime.StaggerSeconds = ParseDouble(value, result.Runtime.StaggerSeconds); break;
                    case "prompt_chars": result.Runtime.PromptChars = ParseInt(value, result.Runtime.PromptChars); break;
                    case "memory_chars": result.Runtime.MemoryChars = ParseInt(value, result.Runtime.MemoryChars); break;
                    case "context_chars": result.Runtime.ContextChars = ParseInt(value, result.Runtime.ContextChars); break;
                    case "inbox_chars": result.Runtime.InboxChars = ParseInt(value, result.Runtime.InboxChars); break;
                    case "beliefs_chars": result.Runtime.BeliefsChars = ParseInt(value, result.Runtime.BeliefsChars); break;
                }
            }
            else if (section == "evolution" && indent >= 2)
            {
                switch (key)
                {
                    case "autonomous": result.Evolution.Autonomous = ParseBool(value, true); break;
                    case "cycle_seconds": result.Evolution.CycleSeconds = ParseInt(value, result.Evolution.CycleSeconds); break;
                    case "auto_validate": result.Evolution.AutoValidate = ParseBool(value, true); break;
                    case "max_repairs": result.Evolution.MaxRepairs = ParseInt(value, result.Evolution.MaxRepairs); break;
                    case "auto_promote": result.Evolution.AutoPromote = ParseBool(value, false); break;
                    case "auto_restart": result.Evolution.AutoRestart = ParseBool(value, true); break;
                    case "restart_delay_seconds":
                        result.Evolution.RestartDelaySeconds = ParseDouble(value, result.Evolution.RestartDelaySeconds);
                        break;
                    case "objective":
                        result.Evolution.Objective = value is "null" or "~" ? "" : value;
                        break;
                }
            }
            else if (section == "git" && indent >= 2)
            {
                switch (key)
                {
                    case "author_name": result.Git.AuthorName = value; break;
                    case "author_email": result.Git.AuthorEmail = value; break;
                    case "auto_push": result.Git.AutoPush = ParseBool(value, true); break;
                    case "remote": result.Git.Remote = value; break;
                    case "branch": result.Git.Branch = value; break;
                }
            }
            else if (section == "telegram" && indent >= 2)
            {
                switch (key)
                {
                    case "enabled": result.Telegram.Enabled = ParseBool(value, false); break;
                    case "token": result.Telegram.Token = value; break;
                    case "allowed_chat_ids": result.Telegram.AllowedChatIds = ParseIdList(value); break;
                    case "adopt_first_chat": result.Telegram.AdoptFirstChat = ParseBool(value, true); break;
                    case "poll_timeout_seconds":
                        result.Telegram.PollTimeoutSeconds = ParseInt(value, result.Telegram.PollTimeoutSeconds);
                        break;
                    case "announcements": result.Telegram.Announcements = ParseBool(value, true); break;
                }
            }
            else if (section == "models" && indent == 2 && key == "default_provider")
            {
                result.DefaultProvider = value;
            }
            else if (section == "models" && indent == 2 && key == "providers")
            {
                modelSection = "providers";
            }
            else if (section == "models" && modelSection == "providers" &&
                     indent == 4 && value.Length == 0)
            {
                provider = key;
                result.Providers.TryAdd(provider, new ProviderEditorSettings("", ""));
            }
            else if (section == "models" && modelSection == "providers" &&
                     indent >= 6 && provider is not null)
            {
                var current = result.Providers[provider];
                result.Providers[provider] = key switch
                {
                    "base_url" => current with { BaseUrl = value },
                    "model" => current with { Model = value },
                    "api_key" => current with { ApiKey = value },
                    "timeout_seconds" => current with { TimeoutSeconds = ParseDouble(value, current.TimeoutSeconds) },
                    _ => current,
                };
            }
            else if (section == "system_agents" && indent == 2 && value.Length == 0)
            {
                systemAgent = key;
                result.SystemAgents.TryAdd(systemAgent, new AgentModelEditorSettings("", ""));
            }
            else if (section == "system_agents" && indent >= 4 && systemAgent is not null)
            {
                var current = result.SystemAgents[systemAgent];
                result.SystemAgents[systemAgent] = key switch
                {
                    "provider" => current with { Provider = value },
                    "model" => current with { Model = value },
                    _ => current,
                };
            }
        }
        return result;
    }

    public void Save(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using var writer = new StreamWriter(path, false, new System.Text.UTF8Encoding(false));
        writer.WriteLine($"environment_name: {Quote(EnvironmentName)}");
        writer.WriteLine($"data_path: {Quote(DataPath)}");
        writer.WriteLine($"generation_path: {Quote(GenerationPath)}");
        writer.WriteLine($"workspace_path: {Quote(WorkspacePath)}");
        writer.WriteLine($"log_level: {Quote(LogLevel)}");
        // Written back verbatim: a settings save must never silently drop the
        // cadence and budget values the mesh depends on.
        writer.WriteLine("runtime:");
        writer.WriteLine($"  cycle_seconds: {Runtime.CycleSeconds}");
        writer.WriteLine($"  stagger_seconds: {Runtime.StaggerSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture)}");
        writer.WriteLine($"  prompt_chars: {Runtime.PromptChars}");
        writer.WriteLine($"  memory_chars: {Runtime.MemoryChars}");
        writer.WriteLine($"  context_chars: {Runtime.ContextChars}");
        writer.WriteLine($"  inbox_chars: {Runtime.InboxChars}");
        writer.WriteLine($"  beliefs_chars: {Runtime.BeliefsChars}");
        writer.WriteLine("evolution:");
        writer.WriteLine($"  autonomous: {(Evolution.Autonomous ? "true" : "false")}");
        writer.WriteLine($"  cycle_seconds: {Evolution.CycleSeconds}");
        writer.WriteLine($"  auto_validate: {(Evolution.AutoValidate ? "true" : "false")}");
        writer.WriteLine($"  max_repairs: {Evolution.MaxRepairs}");
        writer.WriteLine($"  auto_promote: {(Evolution.AutoPromote ? "true" : "false")}");
        writer.WriteLine($"  auto_restart: {(Evolution.AutoRestart ? "true" : "false")}");
        writer.WriteLine($"  restart_delay_seconds: {Number(Evolution.RestartDelaySeconds)}");
        writer.WriteLine($"  objective: {(string.IsNullOrWhiteSpace(Evolution.Objective) ? "null" : Quote(Evolution.Objective))}");
        writer.WriteLine("git:");
        writer.WriteLine($"  author_name: {Quote(Git.AuthorName)}");
        writer.WriteLine($"  author_email: {Quote(Git.AuthorEmail)}");
        writer.WriteLine($"  auto_push: {(Git.AutoPush ? "true" : "false")}");
        writer.WriteLine($"  remote: {Quote(Git.Remote)}");
        writer.WriteLine($"  branch: {Quote(Git.Branch)}");
        writer.WriteLine("telegram:");
        writer.WriteLine($"  enabled: {(Telegram.Enabled ? "true" : "false")}");
        writer.WriteLine($"  token: {Quote(Telegram.Token)}");
        writer.WriteLine($"  allowed_chat_ids: [{FormatIdList(Telegram.AllowedChatIds)}]");
        writer.WriteLine($"  adopt_first_chat: {(Telegram.AdoptFirstChat ? "true" : "false")}");
        writer.WriteLine($"  poll_timeout_seconds: {Telegram.PollTimeoutSeconds}");
        writer.WriteLine($"  announcements: {(Telegram.Announcements ? "true" : "false")}");
        writer.WriteLine("system_agents:");
        foreach (var (agentId, agent) in SystemAgents)
        {
            writer.WriteLine($"  {agentId}:");
            writer.WriteLine($"    provider: {Quote(agent.Provider)}");
            writer.WriteLine($"    model: {Quote(agent.Model)}");
        }
        writer.WriteLine("models:");
        writer.WriteLine($"  default_provider: {Quote(DefaultProvider)}");
        writer.WriteLine("  providers:");
        foreach (var (name, provider) in Providers)
        {
            writer.WriteLine($"    {name}:");
            writer.WriteLine($"      base_url: {Quote(provider.BaseUrl)}");
            writer.WriteLine($"      model: {Quote(provider.Model)}");
            writer.WriteLine($"      timeout_seconds: {Number(provider.TimeoutSeconds)}");
            if (!string.IsNullOrEmpty(provider.ApiKey))
            {
                writer.WriteLine($"      api_key: {Quote(provider.ApiKey)}");
            }
        }
    }

    /// <summary>
    /// Normalises "12, 34" and "[12,34]" alike into the comma-separated form the
    /// editor shows, so a hand-written config and a saved one round-trip the same.
    /// </summary>
    private static string ParseIdList(string value)
    {
        var inner = value.Trim().Trim('[', ']');
        var ids = inner
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(item => Unquote(item))
            .Where(item => long.TryParse(item, out _));
        return string.Join(", ", ids);
    }

    private static string FormatIdList(string value) =>
        string.Join(
            ", ",
            value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Where(item => long.TryParse(item, out _)));

    private static string Number(double value) =>
        value.ToString(System.Globalization.CultureInfo.InvariantCulture);

    private static int ParseInt(string value, int fallback) =>
        int.TryParse(value, System.Globalization.NumberStyles.Integer,
            System.Globalization.CultureInfo.InvariantCulture, out var parsed) ? parsed : fallback;

    private static double ParseDouble(string value, double fallback) =>
        double.TryParse(value, System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out var parsed) ? parsed : fallback;

    private static bool ParseBool(string value, bool fallback) =>
        value.Trim().ToLowerInvariant() switch
        {
            "true" or "yes" or "on" or "1" => true,
            "false" or "no" or "off" or "0" => false,
            _ => fallback,
        };

    private static string Quote(string value) => $"'{value.Replace("'", "''")}'";

    private static string Unquote(string value)
    {
        if (value.Length >= 2 && ((value[0] == '\'' && value[^1] == '\'') ||
                                  (value[0] == '"' && value[^1] == '"')))
        {
            return value[1..^1].Replace("''", "'");
        }
        return value;
    }
}
