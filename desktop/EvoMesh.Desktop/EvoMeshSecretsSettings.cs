namespace EvoMesh.Desktop;

/// <summary>A flat ref -&gt; API key mapping, round-tripped to
/// evomesh.secrets.yaml -- the file .gitignore keeps out of every commit, so
/// this is the one place the Control Center ever writes a real key. Mirrors
/// EvoMeshYamlSettings's own load/save style (line-based, no YAML library
/// dependency) rather than a parallel parsing approach.</summary>
internal sealed class EvoMeshSecretsSettings
{
    public Dictionary<string, string> Refs { get; } = new(StringComparer.Ordinal);

    public static EvoMeshSecretsSettings Load(string path)
    {
        var result = new EvoMeshSecretsSettings();
        if (!File.Exists(path))
        {
            return result;
        }
        foreach (var rawLine in File.ReadAllLines(path))
        {
            var line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith('#') || !line.Contains(':'))
            {
                continue;
            }
            var separator = line.IndexOf(':');
            var refName = Unquote(line[..separator].Trim());
            var key = Unquote(line[(separator + 1)..].Trim());
            if (refName.Length > 0)
            {
                result.Refs[refName] = key;
            }
        }
        return result;
    }

    public void Save(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using var writer = new StreamWriter(path, false, new System.Text.UTF8Encoding(false));
        writer.WriteLine("# Real API keys, named by ref. Gitignored -- see .gitignore and");
        writer.WriteLine("# evomesh.secrets.yaml.example. Written by the Control Center's");
        writer.WriteLine("# \"Edit secrets\" dialog; safe to hand-edit too.");
        foreach (var (refName, key) in Refs)
        {
            writer.WriteLine($"{Quote(refName)}: {Quote(key)}");
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
