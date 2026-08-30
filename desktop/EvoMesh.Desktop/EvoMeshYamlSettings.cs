namespace EvoMesh.Desktop;

internal sealed record ProviderEditorSettings(string BaseUrl, string Model, string ApiKey = "");

internal sealed class EvoMeshYamlSettings
{
    public string EnvironmentName { get; set; } = "local";
    public string DataPath { get; set; } = "data/evomesh.db";
    public string GenerationPath { get; set; } = "generations";
    public string LogLevel { get; set; } = "INFO";
    public string DefaultProvider { get; set; } = "ollama";
    public Dictionary<string, ProviderEditorSettings> Providers { get; } = new(StringComparer.OrdinalIgnoreCase);

    public static EvoMeshYamlSettings Load(string path)
    {
        var result = new EvoMeshYamlSettings();
        if (!File.Exists(path))
        {
            return result;
        }

        string? provider = null;
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
                provider = null;
                switch (key)
                {
                    case "environment_name": result.EnvironmentName = value; break;
                    case "data_path": result.DataPath = value; break;
                    case "generation_path": result.GenerationPath = value; break;
                    case "log_level": result.LogLevel = value; break;
                }
            }
            else if (indent == 2 && key == "default_provider")
            {
                result.DefaultProvider = value;
            }
            else if (indent == 4 && value.Length == 0)
            {
                provider = key;
                result.Providers.TryAdd(provider, new ProviderEditorSettings("", ""));
            }
            else if (indent >= 6 && provider is not null)
            {
                var current = result.Providers[provider];
                result.Providers[provider] = key switch
                {
                    "base_url" => current with { BaseUrl = value },
                    "model" => current with { Model = value },
                    "api_key" => current with { ApiKey = value },
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
        writer.WriteLine($"log_level: {Quote(LogLevel)}");
        writer.WriteLine("models:");
        writer.WriteLine($"  default_provider: {Quote(DefaultProvider)}");
        writer.WriteLine("  providers:");
        foreach (var (name, provider) in Providers)
        {
            writer.WriteLine($"    {name}:");
            writer.WriteLine($"      base_url: {Quote(provider.BaseUrl)}");
            writer.WriteLine($"      model: {Quote(provider.Model)}");
            if (!string.IsNullOrEmpty(provider.ApiKey))
            {
                writer.WriteLine($"      api_key: {Quote(provider.ApiKey)}");
            }
        }
    }

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
