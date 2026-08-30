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
            settings.Save(temporary);
            var saved = EvoMeshYamlSettings.Load(temporary);
            if (saved.EnvironmentName != "desktop-self-test" ||
                saved.Providers["ollama"].Model != "qwen3:14b")
            {
                throw new InvalidOperationException("Settings round-trip failed.");
            }
        }
        finally
        {
            if (File.Exists(temporary)) File.Delete(temporary);
        }
    }
}
