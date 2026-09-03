namespace EvoMesh.Desktop;

internal sealed record ProviderEditorSettings(
    string BaseUrl,
    string Model,
    string ApiKey = "",
    double TimeoutSeconds = 600,
    int NumCtx = 65536)
{
    /// <summary>Per-model override, keyed by model tag. Not itself part of the
    /// record's equality/`with` shape since it is mutated in place rather than
    /// replaced -- there is no editor control for it, only load/save round-trip.</summary>
    public Dictionary<string, int> ModelNumCtx { get; } = new(StringComparer.OrdinalIgnoreCase);
}

internal sealed record AgentModelEditorSettings(string Provider, string Model, int? NumCtx = null);

/// <summary>A model that can look at the project before it answers. Off by default.
/// No editor surface here beyond load/save -- the point is that saving the Settings
/// tab must never silently erase a harness block a human or the mesh already wrote.</summary>
internal sealed class HarnessEditorSettings
{
    public bool Enabled { get; set; }
    public bool AllowWrite { get; set; }
    public int MaxSteps { get; set; } = 24;
    public double MaxSeconds { get; set; } = 300;
    public int TranscriptChars { get; set; } = 12000;
    public string ShellAllow { get; set; } = "";
    public double ShellSeconds { get; set; } = 60;
    public int ToolResultChars { get; set; } = 4000;
    public int ToolResultLines { get; set; } = 200;
    public int GrepMatches { get; set; } = 40;
    public string SessionPath { get; set; } = ".runtime/harness";
    public int Workers { get; set; } = 1;
    public int MaxQueue { get; set; } = 8;
}

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
    public HarnessEditorSettings Harness { get; } = new();

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
        string? modelNumCtxProvider = null;
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
                modelNumCtxProvider = null;
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
            else if (section == "harness" && indent >= 2)
            {
                switch (key)
                {
                    case "enabled": result.Harness.Enabled = ParseBool(value, false); break;
                    case "allow_write": result.Harness.AllowWrite = ParseBool(value, false); break;
                    case "max_steps": result.Harness.MaxSteps = ParseInt(value, result.Harness.MaxSteps); break;
                    case "max_seconds": result.Harness.MaxSeconds = ParseDouble(value, result.Harness.MaxSeconds); break;
                    case "transcript_chars":
                        result.Harness.TranscriptChars = ParseInt(value, result.Harness.TranscriptChars);
                        break;
                    case "shell_allow": result.Harness.ShellAllow = ParseNameList(value); break;
                    case "shell_seconds": result.Harness.ShellSeconds = ParseDouble(value, result.Harness.ShellSeconds); break;
                    case "tool_result_chars":
                        result.Harness.ToolResultChars = ParseInt(value, result.Harness.ToolResultChars);
                        break;
                    case "tool_result_lines":
                        result.Harness.ToolResultLines = ParseInt(value, result.Harness.ToolResultLines);
                        break;
                    case "grep_matches": result.Harness.GrepMatches = ParseInt(value, result.Harness.GrepMatches); break;
                    case "session_path": result.Harness.SessionPath = value; break;
                    case "workers": result.Harness.Workers = ParseInt(value, result.Harness.Workers); break;
                    case "max_queue": result.Harness.MaxQueue = ParseInt(value, result.Harness.MaxQueue); break;
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
                modelNumCtxProvider = null;
                result.Providers.TryAdd(provider, new ProviderEditorSettings("", ""));
            }
            else if (section == "models" && modelSection == "providers" &&
                     indent >= 8 && modelNumCtxProvider is not null)
            {
                // A model tag routinely contains a colon of its own
                // (name:tag), which the naive first-colon split above gets
                // wrong -- so this one nested shape re-splits the raw line
                // quote-aware instead of trusting the key/value already cut.
                var (tag, tagValue) = SplitQuotedKeyPair(line);
                if (ParseIntOrNull(tagValue, null) is int n)
                {
                    result.Providers[modelNumCtxProvider].ModelNumCtx[tag] = n;
                }
            }
            else if (section == "models" && modelSection == "providers" &&
                     indent == 6 && key == "model_num_ctx" && value.Length == 0 && provider is not null)
            {
                modelNumCtxProvider = provider;
            }
            else if (section == "models" && modelSection == "providers" &&
                     indent >= 6 && provider is not null)
            {
                modelNumCtxProvider = null;
                var current = result.Providers[provider];
                result.Providers[provider] = key switch
                {
                    "base_url" => current with { BaseUrl = value },
                    "model" => current with { Model = value },
                    "api_key" => current with { ApiKey = value },
                    "timeout_seconds" => current with { TimeoutSeconds = ParseDouble(value, current.TimeoutSeconds) },
                    "num_ctx" => current with { NumCtx = ParseInt(value, current.NumCtx) },
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
                    "num_ctx" => current with { NumCtx = ParseIntOrNull(value, current.NumCtx) },
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
        // Round-tripped even though this tab has no controls for it: a save
        // that only knows the fields it shows would otherwise erase whatever
        // enabled the harness, which the Evolver's propose stage cannot run
        // without.
        writer.WriteLine("harness:");
        writer.WriteLine($"  enabled: {(Harness.Enabled ? "true" : "false")}");
        writer.WriteLine($"  allow_write: {(Harness.AllowWrite ? "true" : "false")}");
        writer.WriteLine($"  max_steps: {Harness.MaxSteps}");
        writer.WriteLine($"  max_seconds: {Number(Harness.MaxSeconds)}");
        writer.WriteLine($"  transcript_chars: {Harness.TranscriptChars}");
        writer.WriteLine($"  shell_allow: [{FormatNameList(Harness.ShellAllow)}]");
        writer.WriteLine($"  shell_seconds: {Number(Harness.ShellSeconds)}");
        writer.WriteLine($"  tool_result_chars: {Harness.ToolResultChars}");
        writer.WriteLine($"  tool_result_lines: {Harness.ToolResultLines}");
        writer.WriteLine($"  grep_matches: {Harness.GrepMatches}");
        writer.WriteLine($"  session_path: {Quote(Harness.SessionPath)}");
        writer.WriteLine($"  workers: {Harness.Workers}");
        writer.WriteLine($"  max_queue: {Harness.MaxQueue}");
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
            if (agent.NumCtx is int agentNumCtx)
            {
                writer.WriteLine($"    num_ctx: {agentNumCtx}");
            }
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
            writer.WriteLine($"      num_ctx: {provider.NumCtx}");
            if (provider.ModelNumCtx.Count > 0)
            {
                writer.WriteLine("      model_num_ctx:");
                foreach (var (tag, tagNumCtx) in provider.ModelNumCtx)
                {
                    writer.WriteLine($"        {Quote(tag)}: {tagNumCtx}");
                }
            }
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

    /// <summary>Bare, comma-separated program/command names -- shell_allow's shape,
    /// not numeric like the chat-id lists above, and not expected to be quoted.</summary>
    private static string ParseNameList(string value)
    {
        var inner = value.Trim().Trim('[', ']');
        var names = inner
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(Unquote)
            .Where(item => item.Length > 0);
        return string.Join(", ", names);
    }

    private static string FormatNameList(string value) =>
        string.Join(
            ", ",
            value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));

    /// <summary>
    /// A mapping key that is itself allowed to contain a colon -- an Ollama model
    /// tag such as "name:tag" under model_num_ctx -- which the file-wide
    /// first-colon split above gets wrong. Only that one nested shape needs this;
    /// every other key in this file is a bare identifier with no colon of its own.
    /// </summary>
    private static (string Key, string Value) SplitQuotedKeyPair(string line)
    {
        if (line.Length > 0 && (line[0] == '\'' || line[0] == '"'))
        {
            var quoteChar = line[0];
            var closing = line.IndexOf(quoteChar, 1);
            var separator = closing > 0 ? line.IndexOf(':', closing + 1) : -1;
            if (separator > 0)
            {
                return (Unquote(line[..(closing + 1)]), Unquote(line[(separator + 1)..].Trim()));
            }
        }
        var plainSeparator = line.IndexOf(':');
        return (line[..plainSeparator].Trim(), Unquote(line[(plainSeparator + 1)..].Trim()));
    }

    private static string Number(double value) =>
        value.ToString(System.Globalization.CultureInfo.InvariantCulture);

    private static int ParseInt(string value, int fallback) =>
        int.TryParse(value, System.Globalization.NumberStyles.Integer,
            System.Globalization.CultureInfo.InvariantCulture, out var parsed) ? parsed : fallback;

    private static int? ParseIntOrNull(string value, int? fallback) =>
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
