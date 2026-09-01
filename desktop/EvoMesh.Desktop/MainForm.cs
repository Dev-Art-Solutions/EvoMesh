using System.Drawing;
using System.Text.Json;

namespace EvoMesh.Desktop;

internal sealed class MainForm : Form
{
    private readonly EvoMeshRuntimeProcess _runtime;
    private readonly string _configPath;
    private readonly RichTextBox _output = new();
    private readonly TextBox _command = new();
    private readonly Label _status = new();
    private readonly Button _start = new();
    private readonly Button _stop = new();
    private readonly List<Control> _restartRequiredControls = [];
    private readonly Dictionary<string, (TextBox Url, ComboBox Model, TextBox Key)> _providers = [];
    private TextBox _environmentName = null!;
    private TextBox _dataPath = null!;
    private TextBox _generationPath = null!;
    private ComboBox _logLevel = null!;
    private ComboBox _defaultProvider = null!;
    private Button _saveSettings = null!;
    private Button _reloadSettings = null!;
    private Label _settingsNotice = null!;
    private ComboBox _agentProvider = null!;
    private ComboBox _agentModel = null!;

    public MainForm(string rootPath, string uvExecutable)
    {
        _runtime = new EvoMeshRuntimeProcess(rootPath, uvExecutable);
        _configPath = Path.Combine(rootPath, "evomesh.yaml");
        Text = "EvoMesh Control Center";
        MinimumSize = new Size(980, 680);
        Size = new Size(1200, 820);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10F);
        BackColor = Color.FromArgb(245, 248, 252);

        Controls.Add(BuildTabs());
        Controls.Add(BuildHeader());
        _runtime.OutputReceived += AppendOutput;
        _runtime.RunningChanged += UpdateRuntimeState;
        EnsureConfiguration();
        LoadSettings();
        UpdateRuntimeState(false);
        Shown += async (_, _) =>
        {
            if (await _runtime.TryAttachAsync())
            {
                AppendOutput("[Control Center connected automatically]");
            }
            await RefreshOllamaModelsAsync(showErrors: false);
        };
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _runtime.Dispose();
        }
        base.Dispose(disposing);
    }

    internal void ValidateUiForTest()
    {
        CreateControl();
        PerformLayout();
        var header = Controls.OfType<Panel>().Single(control => control.Dock == DockStyle.Top);
        var layout = header.Controls.OfType<TableLayoutPanel>().Single();
        header.PerformLayout();
        layout.PerformLayout();
        if (_status.Bounds.IntersectsWith(_start.Bounds))
        {
            throw new InvalidOperationException("Status label overlaps the Start button.");
        }
        if (!_providers.TryGetValue("ollama", out var ollama) ||
            ollama.Model.DropDownStyle != ComboBoxStyle.DropDown)
        {
            throw new InvalidOperationException("Settings Ollama model must be an editable dropdown.");
        }
    }

    private Control BuildHeader()
    {
        var headerColor = Color.FromArgb(8, 42, 82);
        var panel = new Panel { Dock = DockStyle.Top, Height = 82, BackColor = headerColor };
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 4,
            RowCount = 1,
            Padding = new Padding(18, 10, 18, 10),
            BackColor = headerColor,
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 150));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));

        var brand = new Panel { Dock = DockStyle.Fill, BackColor = headerColor };
        var title = new Label { Text = "EvoMesh", ForeColor = Color.White, Font = new Font("Segoe UI", 21F, FontStyle.Bold), AutoSize = true, Location = new Point(0, 0) };
        var subtitle = new Label { Text = "Local multi-agent control center", ForeColor = Color.FromArgb(170, 220, 235), AutoSize = true, Location = new Point(3, 38) };
        brand.Controls.AddRange([title, subtitle]);

        _status.AutoSize = false;
        _status.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _status.Dock = DockStyle.Fill;
        _status.TextAlign = ContentAlignment.MiddleRight;
        _status.Margin = new Padding(4, 0, 12, 0);
        _start.Text = "Start Mesh";
        _start.Dock = DockStyle.Fill;
        _start.Margin = new Padding(5, 11, 5, 11);
        StyleButton(_start);
        _start.Click += async (_, _) => await RunSafeAsync(_runtime.StartAsync);
        _stop.Text = "Stop Mesh";
        _stop.Dock = DockStyle.Fill;
        _stop.Margin = new Padding(5, 11, 0, 11);
        StyleButton(_stop);
        _stop.Click += async (_, _) => await RunSafeAsync(_runtime.StopAsync);
        layout.Controls.Add(brand, 0, 0);
        layout.Controls.Add(_status, 1, 0);
        layout.Controls.Add(_start, 2, 0);
        layout.Controls.Add(_stop, 3, 0);
        panel.Controls.Add(layout);
        return panel;
    }

    private Control BuildTabs()
    {
        var tabs = new TabControl { Dock = DockStyle.Fill, Padding = new Point(18, 7) };
        tabs.TabPages.Add(BuildConsoleTab());
        tabs.TabPages.Add(BuildAgentsTab());
        tabs.TabPages.Add(BuildSettingsTab());
        return tabs;
    }

    private TabPage BuildConsoleTab()
    {
        var page = new TabPage("Console & Chat") { Padding = new Padding(14), BackColor = BackColor };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1 };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 92));

        var quick = new FlowLayoutPanel { Dock = DockStyle.Fill, Height = 45, AutoSize = true };
        foreach (var (text, command) in new[]
        {
            ("Status", "/status"), ("Agents", "/agents"), ("Ollama models", "/models ollama"),
            ("Skills", "/skills"), ("Help", "/help")
        })
        {
            var button = MakeButton(text, 120);
            button.Click += async (_, _) => await SendCommandAsync(command);
            quick.Controls.Add(button);
        }

        _output.Dock = DockStyle.Fill;
        _output.ReadOnly = true;
        _output.BackColor = Color.FromArgb(17, 25, 39);
        _output.ForeColor = Color.FromArgb(225, 235, 245);
        _output.Font = new Font("Cascadia Mono", 10F);
        _output.BorderStyle = BorderStyle.FixedSingle;

        var input = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Padding = new Padding(0, 10, 0, 0) };
        input.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        input.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));
        _command.Dock = DockStyle.Fill;
        _command.Multiline = true;
        _command.PlaceholderText = "Write a message to the selected agent or enter a /command...";
        _command.KeyDown += async (_, args) =>
        {
            if (args.KeyCode == Keys.Enter && !args.Shift)
            {
                args.SuppressKeyPress = true;
                await SendCurrentAsync();
            }
        };
        var send = MakeButton("Send", 120);
        send.Dock = DockStyle.Fill;
        send.Margin = new Padding(10, 0, 0, 0);
        send.Click += async (_, _) => await SendCurrentAsync();
        input.Controls.Add(_command, 0, 0);
        input.Controls.Add(send, 1, 0);

        layout.Controls.Add(quick, 0, 0);
        layout.Controls.Add(_output, 0, 1);
        layout.Controls.Add(input, 0, 2);
        page.Controls.Add(layout);
        return page;
    }

    private TabPage BuildAgentsTab()
    {
        var page = new TabPage("Agents") { Padding = new Padding(18), BackColor = BackColor };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));

        var architect = new GroupBox { Text = "Create an agent with Agent Architect", Dock = DockStyle.Fill, Padding = new Padding(16) };
        var architectLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3 };
        architectLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        architectLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        architectLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        architectLayout.Controls.Add(new Label { Text = "Describe the agent you want. Architect will continue the interview in Console & Chat.", AutoSize = true }, 0, 0);
        var request = new TextBox { Dock = DockStyle.Fill, Multiline = true, PlaceholderText = "Example: Create a Bulgarian research agent that reads D:\\Papers and uses qwen3:14b." };
        architectLayout.Controls.Add(request, 0, 1);
        var ask = MakeButton("Ask Architect", 150);
        ask.Click += async (_, _) =>
        {
            if (string.IsNullOrWhiteSpace(request.Text)) return;
            await SendCommandAsync("/chat architect");
            await SendCommandAsync(request.Text.Trim());
            request.Clear();
        };
        architectLayout.Controls.Add(ask, 0, 2);
        architect.Controls.Add(architectLayout);

        var models = new GroupBox { Text = "Per-agent runtime and model", Dock = DockStyle.Fill, Padding = new Padding(16) };
        var grid = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, RowCount = 4 };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 45));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 55));
        var agentName = AddField(grid, "Agent name", 0, 0);
        _agentProvider = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList };
        _agentProvider.Items.AddRange(["ollama", "inferhub", "openai_compatible"]);
        _agentProvider.SelectedIndex = 0;
        _agentProvider.SelectedIndexChanged += async (_, _) =>
        {
            if (_agentProvider.Text == "ollama")
            {
                await RefreshOllamaModelsAsync(showErrors: false);
            }
        };
        grid.Controls.Add(new Label { Text = "Provider", AutoSize = true, Anchor = AnchorStyles.Left }, 2, 0);
        grid.Controls.Add(_agentProvider, 3, 0);
        grid.Controls.Add(new Label { Text = "Model", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) }, 0, 1);
        _agentModel = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDown,
            Margin = new Padding(3, 5, 12, 5),
        };
        grid.Controls.Add(_agentModel, 1, 1);
        grid.SetColumnSpan(_agentModel, 3);
        var apply = MakeButton("Apply model", 130);
        apply.Click += async (_, _) =>
        {
            if (string.IsNullOrWhiteSpace(agentName.Text) || string.IsNullOrWhiteSpace(_agentModel.Text))
            {
                MessageBox.Show(this, "Select an agent and model first.", "EvoMesh", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            await SendCommandAsync($"/model {Quote(agentName.Text)} {Quote(_agentModel.Text)} {_agentProvider.Text}");
        };
        var list = MakeButton("Refresh models", 150);
        list.Click += async (_, _) =>
        {
            if (_agentProvider.Text == "ollama")
            {
                await RefreshOllamaModelsAsync(showErrors: true);
            }
            else
            {
                await SendCommandAsync($"/models {_agentProvider.Text}");
            }
        };
        var startAgent = MakeButton("Start agent", 130);
        startAgent.Click += async (_, _) => await SendCommandAsync($"/agent start {Quote(agentName.Text)}");
        var stopAgent = MakeButton("Stop agent", 130);
        stopAgent.Click += async (_, _) => await SendCommandAsync($"/agent stop {Quote(agentName.Text)}");
        var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
        actions.Controls.AddRange([apply, list, startAgent, stopAgent]);
        grid.Controls.Add(actions, 0, 2);
        grid.SetColumnSpan(actions, 4);
        grid.Controls.Add(new Label { Text = "Changing a running agent's model safely restarts only that agent. Other agents keep running.", AutoSize = true, ForeColor = Color.DimGray }, 0, 3);
        grid.SetColumnSpan(grid.GetControlFromPosition(0, 3)!, 4);
        models.Controls.Add(grid);

        layout.Controls.Add(architect, 0, 0);
        layout.Controls.Add(models, 0, 1);
        page.Controls.Add(layout);
        return page;
    }

    private TabPage BuildSettingsTab()
    {
        var page = new TabPage("Settings") { Padding = new Padding(18), BackColor = BackColor, AutoScroll = true };
        var grid = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 4, Padding = new Padding(8) };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));

        _settingsNotice = new Label { AutoSize = true, Font = new Font(Font, FontStyle.Bold), ForeColor = Color.FromArgb(172, 83, 0), Margin = new Padding(3, 3, 3, 16) };
        grid.Controls.Add(_settingsNotice, 0, 0);
        grid.SetColumnSpan(_settingsNotice, 4);
        _environmentName = AddField(grid, "Environment name", 0, 1);
        _dataPath = AddField(grid, "SQLite data path", 2, 1);
        _generationPath = AddField(grid, "Generations path", 0, 2);
        _logLevel = AddCombo(grid, "Log level", 2, 2, ["DEBUG", "INFO", "WARNING", "ERROR"]);
        _defaultProvider = AddCombo(grid, "Default provider", 0, 3, ["ollama", "inferhub", "openai_compatible"]);

        var row = 4;
        foreach (var name in new[] { "ollama", "inferhub", "openai_compatible" })
        {
            var heading = new Label { Text = name.Replace('_', ' ').ToUpperInvariant(), AutoSize = true, Font = new Font(Font, FontStyle.Bold), Margin = new Padding(3, 18, 3, 6) };
            grid.Controls.Add(heading, 0, row);
            grid.SetColumnSpan(heading, 4);
            row++;
            var url = AddField(grid, "Base URL", 0, row);
            var model = name == "ollama"
                ? AddEditableCombo(
                    grid,
                    "Default model",
                    2,
                    row,
                    () => RefreshOllamaModelsAsync(showErrors: true))
                : AddEditableCombo(grid, "Default model", 2, row);
            row++;
            var key = AddField(grid, "API key (optional)", 0, row);
            key.UseSystemPasswordChar = true;
            grid.SetColumnSpan(key, 3);
            _providers[name] = (url, model, key);
            row++;
        }

        var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(3, 18, 3, 3) };
        _saveSettings = MakeButton("Save settings", 145);
        _saveSettings.Click += (_, _) => SaveSettings();
        _reloadSettings = MakeButton("Reload", 110);
        _reloadSettings.Click += (_, _) => LoadSettings();
        actions.Controls.AddRange([_saveSettings, _reloadSettings]);
        grid.Controls.Add(actions, 0, row);
        grid.SetColumnSpan(actions, 4);

        _restartRequiredControls.AddRange([
            _environmentName, _dataPath, _generationPath, _logLevel, _defaultProvider,
            .. _providers.Values.SelectMany(item => new Control[] { item.Url, item.Model, item.Key }),
            _saveSettings, _reloadSettings
        ]);
        page.Controls.Add(grid);
        return page;
    }

    private async Task SendCurrentAsync()
    {
        var text = _command.Text.Trim();
        if (text.Length == 0) return;
        _command.Clear();
        await SendCommandAsync(text);
    }

    private async Task RefreshOllamaModelsAsync(bool showErrors)
    {
        if (_agentProvider is null || _agentModel is null)
        {
            return;
        }
        try
        {
            var settings = EvoMeshYamlSettings.Load(_configPath);
            if (!settings.Providers.TryGetValue("ollama", out var ollama))
            {
                throw new InvalidOperationException("Ollama is not configured in evomesh.yaml.");
            }
            var baseUrl = ollama.BaseUrl.TrimEnd('/');
            var tagsUrl = baseUrl.EndsWith("/api", StringComparison.OrdinalIgnoreCase)
                ? $"{baseUrl}/tags"
                : $"{baseUrl}/api/tags";
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var response = await client.GetAsync(tagsUrl);
            response.EnsureSuccessStatusCode();
            using var document = JsonDocument.Parse(await response.Content.ReadAsStreamAsync());
            var names = document.RootElement.GetProperty("models")
                .EnumerateArray()
                .Select(item => item.GetProperty("name").GetString())
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .Cast<string>()
                .Order(StringComparer.OrdinalIgnoreCase)
                .ToArray();
            if (_agentProvider.Text == "ollama")
            {
                PopulateModelCombo(_agentModel, names, ollama.Model);
            }
            if (_providers.TryGetValue("ollama", out var settingsControls))
            {
                PopulateModelCombo(settingsControls.Model, names, ollama.Model);
            }
            AppendOutput($"[loaded {names.Length} Ollama models into the dropdowns]");
        }
        catch (Exception exc)
        {
            AppendOutput($"[Ollama models unavailable] {exc.Message}");
            if (showErrors)
            {
                MessageBox.Show(this, exc.Message, "Unable to load Ollama models", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
    }

    private static void PopulateModelCombo(ComboBox combo, string[] models, string configuredModel)
    {
        var current = combo.Text;
        combo.BeginUpdate();
        combo.Items.Clear();
        combo.Items.AddRange(models);
        combo.EndUpdate();

        var preferred = string.IsNullOrWhiteSpace(current) ? configuredModel : current;
        var match = models.FirstOrDefault(name =>
            string.Equals(name, preferred, StringComparison.OrdinalIgnoreCase));
        if (match is not null)
        {
            combo.SelectedItem = match;
        }
        else if (!string.IsNullOrWhiteSpace(preferred))
        {
            combo.Text = preferred;
        }
        else if (models.Length > 0)
        {
            combo.SelectedIndex = 0;
        }
    }

    private async Task SendCommandAsync(string command)
    {
        if (string.IsNullOrWhiteSpace(command)) return;
        if (!_runtime.IsRunning)
        {
            MessageBox.Show(this, "Start EvoMesh first.", "EvoMesh", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        AppendOutput($"you> {command}");
        await RunSafeAsync(() => _runtime.SendAsync(command));
    }

    private void LoadSettings()
    {
        EnsureConfiguration();
        var settings = EvoMeshYamlSettings.Load(_configPath);
        _environmentName.Text = settings.EnvironmentName;
        _dataPath.Text = settings.DataPath;
        _generationPath.Text = settings.GenerationPath;
        _logLevel.SelectedItem = settings.LogLevel.ToUpperInvariant();
        _defaultProvider.SelectedItem = settings.DefaultProvider;
        foreach (var (name, controls) in _providers)
        {
            if (settings.Providers.TryGetValue(name, out var provider))
            {
                controls.Url.Text = provider.BaseUrl;
                controls.Model.Text = provider.Model;
                controls.Key.Text = provider.ApiKey;
            }
        }
    }

    private void SaveSettings()
    {
        if (_runtime.IsRunning)
        {
            MessageBox.Show(this, "Stop EvoMesh before changing restart-required settings.", "Restart required", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        var settings = new EvoMeshYamlSettings
        {
            EnvironmentName = _environmentName.Text.Trim(),
            DataPath = _dataPath.Text.Trim(),
            GenerationPath = _generationPath.Text.Trim(),
            LogLevel = _logLevel.Text,
            DefaultProvider = _defaultProvider.Text,
        };
        foreach (var (name, controls) in _providers)
        {
            settings.Providers[name] = new ProviderEditorSettings(
                controls.Url.Text.Trim(), controls.Model.Text.Trim(), controls.Key.Text);
        }
        settings.Save(_configPath);
        AppendOutput($"[settings saved to {_configPath}]");
    }

    private void EnsureConfiguration()
    {
        if (!File.Exists(_configPath))
        {
            File.Copy(Path.Combine(_runtime.RootPath, "evomesh.yaml.example"), _configPath);
        }
    }

    private void UpdateRuntimeState(bool running)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => UpdateRuntimeState(running));
            return;
        }
        _status.Text = running ? "● RUNNING" : "● STOPPED";
        _status.ForeColor = running ? Color.FromArgb(78, 220, 130) : Color.FromArgb(255, 170, 120);
        _start.Enabled = !running;
        _stop.Enabled = running;
        foreach (var control in _restartRequiredControls) control.Enabled = !running;
        _settingsNotice.Text = running
            ? "Mesh is running. Restart-required settings are locked; stop the mesh to edit them."
            : "Mesh is stopped. Settings can be edited and will apply on the next start.";
    }

    private void AppendOutput(string text)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => AppendOutput(text));
            return;
        }
        _output.AppendText(text + Environment.NewLine);
        _output.SelectionStart = _output.TextLength;
        _output.ScrollToCaret();
    }

    private async Task RunSafeAsync(Func<Task> action)
    {
        try { await action(); }
        catch (Exception exc)
        {
            AppendOutput($"[error] {exc.Message}");
            MessageBox.Show(this, exc.Message, "EvoMesh error", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static Button MakeButton(string text, int width)
    {
        var button = new Button
        {
            Text = text,
            Width = width,
            Height = 36,
            Margin = new Padding(4),
        };
        StyleButton(button);
        return button;
    }

    private static void StyleButton(Button button)
    {
        button.FlatStyle = FlatStyle.Flat;
        button.UseVisualStyleBackColor = false;
        button.FlatAppearance.BorderColor = Color.FromArgb(30, 112, 168);
        button.FlatAppearance.BorderSize = 1;

        void ApplyColors()
        {
            button.BackColor = button.Enabled
                ? Color.FromArgb(14, 82, 132)
                : Color.FromArgb(215, 222, 229);
            button.ForeColor = button.Enabled
                ? Color.White
                : Color.FromArgb(80, 88, 96);
        }

        button.EnabledChanged += (_, _) => ApplyColors();
        ApplyColors();
    }

    private static TextBox AddField(TableLayoutPanel grid, string label, int column, int row)
    {
        var caption = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) };
        var field = new TextBox { Dock = DockStyle.Fill, Margin = new Padding(3, 5, 12, 5) };
        grid.Controls.Add(caption, column, row);
        grid.Controls.Add(field, column + 1, row);
        return field;
    }

    private static ComboBox AddCombo(TableLayoutPanel grid, string label, int column, int row, string[] items)
    {
        var caption = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) };
        var field = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList, Margin = new Padding(3, 5, 12, 5) };
        field.Items.AddRange(items);
        grid.Controls.Add(caption, column, row);
        grid.Controls.Add(field, column + 1, row);
        return field;
    }

    private ComboBox AddEditableCombo(
        TableLayoutPanel grid,
        string label,
        int column,
        int row,
        Func<Task>? refresh = null)
    {
        var caption = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) };
        var field = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDown,
            AutoCompleteMode = AutoCompleteMode.SuggestAppend,
            AutoCompleteSource = AutoCompleteSource.ListItems,
            Margin = refresh is null ? new Padding(3, 5, 12, 5) : Padding.Empty,
        };
        grid.Controls.Add(caption, column, row);
        if (refresh is null)
        {
            grid.Controls.Add(field, column + 1, row);
        }
        else
        {
            var container = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                Margin = new Padding(3, 5, 12, 5),
            };
            container.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            container.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 88));
            var refreshButton = MakeButton("Refresh", 80);
            refreshButton.Dock = DockStyle.Fill;
            refreshButton.Margin = new Padding(6, 0, 0, 0);
            refreshButton.Click += async (_, _) => await refresh();
            container.Controls.Add(field, 0, 0);
            container.Controls.Add(refreshButton, 1, 0);
            grid.Controls.Add(container, column + 1, row);
        }
        return field;
    }

    private static string Quote(string value) => $"\"{value.Replace("\\", "\\\\").Replace("\"", "\\\"")}\"";
}
