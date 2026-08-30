namespace EvoMesh.Desktop;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--self-test")
        {
            DesktopSelfTest.Run(args.Length > 1 ? args[1] : FindRepositoryRoot());
            return 0;
        }
        ApplicationConfiguration.Initialize();
        var root = args.Length > 0 ? Path.GetFullPath(args[0]) : FindRepositoryRoot();
        var uv = args.Length > 1 ? args[1] : "uv";
        Application.Run(new MainForm(root, uv));
        return 0;
    }

    private static string FindRepositoryRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "pyproject.toml")))
            {
                return current.FullName;
            }
            current = current.Parent;
        }
        return Environment.CurrentDirectory;
    }
}
