using System.Drawing;
using System.Text.Json;

namespace EvoMesh.Desktop;

internal sealed class MainForm : Form
{
    private static readonly (string Id, string Name)[] CoreAgents =
    [
        ("architect", "Agent Architect"),
        ("guardian", "Guardian"),
        ("evaluator", "Evaluator"),
        ("evolver", "Environment Evolver"),
    ];
    private readonly EvoMeshRuntimeProcess _runtime;
    private readonly string _configPath;
    private readonly RichTextBox _output = new();
    private readonly TextBox _command = new();
    private readonly Label _status = new();
    private readonly Button _start = new();
    private readonly Button _stop = new();
    private readonly Dictionary<string, (TextBox Url, ComboBox Model, TextBox Key)> _providers = [];
    private readonly Dictionary<string, (ComboBox Provider, ComboBox Model)> _systemAgents = [];
    private bool _loadingSettings;
    private bool _running;
    private DateTimeOffset? _lastCheck;
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
    private CheckBox _autoRestart = null!;
    private TextBox _gitAuthorName = null!;
    private TextBox _gitAuthorEmail = null!;
    private CheckBox _gitAutoPush = null!;
    private TextBox _gitRemote = null!;
    private TextBox _gitBranch = null!;
    private CheckBox _telegramEnabled = null!;
    private TextBox _telegramToken = null!;
    private TextBox _telegramChats = null!;
    private CheckBox _telegramAdoptFirst = null!;
    private CheckBox _telegramAnnouncements = null!;
    private Label _telegramNotice = null!;

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
        _runtime.HealthChecked += ShowHealthCheck;
        EnsureConfiguration();
        LoadSettings();
        UpdateRuntimeState(false);
        Shown += async (_, _) =>
        {
            if (await _runtime.TryAttachAsync())
            {
                AppendOutput("[Control Center connected automatically]");
            }
            // From here the mesh is watched continuously. A mesh started from
            // the launcher script, or one that restarted itself into a new
            // generation, is picked up without anyone touching this window.
            _runtime.StartHealthLoop();
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
        if (_systemAgents.Count != CoreAgents.Length ||
            _systemAgents.Values.Any(item => item.Model.DropDownStyle != ComboBoxStyle.DropDown))
        {
            throw new InvalidOperationException("All core agents must have editable model dropdowns.");
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
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 235));
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
            ("Skills", "/skills"), ("Evolution", "/evolution status"),
            ("World", "/context world"), ("Restart mesh", "/restart"), ("Help", "/help")
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

        AddHeading(grid, "EVOLUTION & GIT", ref row);
        AddHelp(
            grid,
            "A generation the mesh validates is committed, pushed to the remote, and the mesh " +
            "restarts itself into it. Commits are authored by the identity below, so the agent's " +
            "work is never mistaken for yours.",
            ref row);
        _autoRestart = AddCheck(grid, "Restart into a landed generation", 0, row);
        _gitAutoPush = AddCheck(grid, "Push a landed generation to the remote", 2, row);
        row++;
        _gitAuthorName = AddField(grid, "Commit author name", 0, row);
        _gitAuthorEmail = AddField(grid, "Commit author email", 2, row);
        row++;
        _gitRemote = AddField(grid, "Remote", 0, row);
        _gitBranch = AddField(grid, "Branch (blank = current)", 2, row);
        row++;

        AddHeading(grid, "TELEGRAM", ref row);
        AddHelp(
            grid,
            "Create a bot with @BotFather, paste the token it gives you, and enable it. Everything " +
            "you send the bot goes through the same commands as this console. Leave the chat ids " +
            "empty with adoption on and the first person to send /start claims the bot.",
            ref row);
        _telegramEnabled = AddCheck(grid, "Enable the Telegram bot", 0, row);
        _telegramAdoptFirst = AddCheck(grid, "Let the first chat claim the bot", 2, row);
        row++;
        _telegramToken = AddField(grid, "Bot token (BotFather)", 0, row);
        _telegramToken.UseSystemPasswordChar = true;
        grid.SetColumnSpan(_telegramToken, 3);
        row++;
        _telegramChats = AddField(grid, "Allowed chat ids", 0, row);
        _telegramAnnouncements = AddCheck(grid, "Announce promotions and restarts", 2, row);
        row++;

        var telegramActions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill };
        var testToken = MakeButton("Test token", 130);
        testToken.Click += async (_, _) => await TestTelegramTokenAsync();
        var liveStatus = MakeButton("Live status", 130);
        liveStatus.Click += async (_, _) => await SendCommandAsync("/telegram status");
        var allowChat = MakeButton("Allow chat id", 145);
        allowChat.Click += async (_, _) => await ManageTelegramChatAsync("allow");
        var revokeChat = MakeButton("Revoke chat id", 150);
        revokeChat.Click += async (_, _) => await ManageTelegramChatAsync("revoke");
        telegramActions.Controls.AddRange([testToken, liveStatus, allowChat, revokeChat]);
        grid.Controls.Add(telegramActions, 0, row);
        grid.SetColumnSpan(telegramActions, 4);
        row++;
        _telegramNotice = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(880, 0),
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 8),
            Text = "\"Test token\" asks Telegram directly and needs neither a save nor a "
                 + "running mesh. \"Live status\" asks the running mesh, and is the only "
                 + "place a chat that claimed the bot at runtime shows up — those are kept "
                 + "in the database, not in this file.",
        };
        grid.Controls.Add(_telegramNotice, 0, row);
        grid.SetColumnSpan(_telegramNotice, 4);
        row++;

        var systemHeading = new Label
        {
            Text = "CORE AGENT MODELS",
            AutoSize = true,
            Font = new Font(Font, FontStyle.Bold),
            Margin = new Padding(3, 18, 3, 6),
        };
        grid.Controls.Add(systemHeading, 0, row);
        grid.SetColumnSpan(systemHeading, 4);
        row++;
        var systemHelp = new Label
        {
            Text = "These provider/model assignments are applied to the built-in agents on the next mesh start.",
            AutoSize = true,
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 8),
        };
        grid.Controls.Add(systemHelp, 0, row);
        grid.SetColumnSpan(systemHelp, 4);
        row++;
        foreach (var (agentId, agentName) in CoreAgents)
        {
            var provider = AddCombo(
                grid,
                agentName,
                0,
                row,
                ["ollama", "inferhub", "openai_compatible"]);
            var model = AddEditableCombo(grid, "Model", 2, row);
            provider.SelectedIndexChanged += async (_, _) =>
            {
                if (!_loadingSettings && provider.Text == "ollama")
                {
                    await RefreshOllamaModelsAsync(showErrors: false);
                }
            };
            _systemAgents[agentId] = (provider, model);
            row++;
        }

        var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(3, 18, 3, 3) };
        _saveSettings = MakeButton("Save settings", 145);
        _saveSettings.Click += async (_, _) => await SaveSettingsAsync();
        _reloadSettings = MakeButton("Reload", 110);
        _reloadSettings.Click += (_, _) => LoadSettings();
        actions.Controls.AddRange([_saveSettings, _reloadSettings]);
        grid.Controls.Add(actions, 0, row);
        grid.SetColumnSpan(actions, 4);

        page.Controls.Add(grid);
        return page;
    }

    /// <summary>
    /// Asks Telegram itself whether the token in the box is real.
    /// </summary>
    /// <remarks>
    /// Deliberately independent of the mesh and of saving. A human who has just
    /// pasted a token wants to know it is right before committing to a restart,
    /// and the failure they need to see -- a typo, a revoked bot, no network --
    /// is one only Telegram can report.
    /// </remarks>
    private async Task TestTelegramTokenAsync()
    {
        var token = _telegramToken.Text.Trim();
        if (token.Length == 0)
        {
            MessageBox.Show(this, "Paste the token BotFather gave you first.", "Telegram",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            using var response = await client.GetAsync($"https://api.telegram.org/bot{token}/getMe");
            using var document = JsonDocument.Parse(await response.Content.ReadAsStreamAsync());
            var root = document.RootElement;
            if (!response.IsSuccessStatusCode || !root.GetProperty("ok").GetBoolean())
            {
                var reason = root.TryGetProperty("description", out var description)
                    ? description.GetString()
                    : $"HTTP {(int)response.StatusCode}";
                AppendOutput($"[Telegram refused the token] {reason}");
                MessageBox.Show(this, $"Telegram refused the token:{Environment.NewLine}{Environment.NewLine}{reason}", "Telegram",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            var bot = root.GetProperty("result");
            var name = bot.TryGetProperty("username", out var username) ? username.GetString() : "?";
            AppendOutput($"[Telegram accepted the token] the bot is @{name}");
            var newline = Environment.NewLine;
            MessageBox.Show(this,
                $"Telegram accepted the token.{newline}{newline}The bot is @{name}.{newline}{newline}" +
                "Save the settings and restart the mesh, then send it /start from Telegram.",
                "Telegram", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        catch (Exception exc)
        {
            AppendOutput($"[Telegram could not be reached] {exc.Message}");
            MessageBox.Show(this, exc.Message, "Telegram is unreachable",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    /// <summary>Adds or removes one chat id on the running mesh.</summary>
    private async Task ManageTelegramChatAsync(string action)
    {
        var id = _telegramChats.Text.Split(',').LastOrDefault()?.Trim() ?? "";
        if (!long.TryParse(id, out _))
        {
            MessageBox.Show(this,
                $"Put the chat id to {action} last in the \"Allowed chat ids\" box, " +
                "then press this again.",
                "Telegram", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        await SendCommandAsync($"/telegram {action} {id}");
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
            foreach (var (agentId, controls) in _systemAgents)
            {
                if (controls.Provider.Text != "ollama")
                {
                    continue;
                }
                var configuredModel = settings.SystemAgents.TryGetValue(agentId, out var agent)
                    ? agent.Model
                    : ollama.Model;
                PopulateModelCombo(controls.Model, names, configuredModel);
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
        _loadingSettings = true;
        try
        {
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
            _autoRestart.Checked = settings.Evolution.AutoRestart;
            _gitAuthorName.Text = settings.Git.AuthorName;
            _gitAuthorEmail.Text = settings.Git.AuthorEmail;
            _gitAutoPush.Checked = settings.Git.AutoPush;
            _gitRemote.Text = settings.Git.Remote;
            _gitBranch.Text = settings.Git.Branch;
            _telegramEnabled.Checked = settings.Telegram.Enabled;
            _telegramToken.Text = settings.Telegram.Token;
            _telegramChats.Text = settings.Telegram.AllowedChatIds;
            _telegramAdoptFirst.Checked = settings.Telegram.AdoptFirstChat;
            _telegramAnnouncements.Checked = settings.Telegram.Announcements;
            foreach (var (agentId, controls) in _systemAgents)
            {
                var configured = settings.SystemAgents.GetValueOrDefault(agentId);
                var providerName = configured?.Provider ?? settings.DefaultProvider;
                var modelName = configured?.Model;
                if (string.IsNullOrWhiteSpace(modelName) &&
                    settings.Providers.TryGetValue(providerName, out var provider))
                {
                    modelName = provider.Model;
                }
                controls.Provider.SelectedItem = providerName;
                controls.Model.Text = modelName ?? "local-model";
            }
        }
        finally
        {
            _loadingSettings = false;
        }
    }

    private async Task SaveSettingsAsync()
    {
        if (_runtime.IsRunning)
        {
            // Every setting on this tab is read once, at boot. Offering the
            // restart here is the difference between a saved file and a mesh
            // that is actually using it.
            var choice = MessageBox.Show(
                this,
                "These settings are read when the mesh boots. Save them and restart the mesh now?",
                "Restart required",
                MessageBoxButtons.YesNoCancel,
                MessageBoxIcon.Question);
            if (choice == DialogResult.Cancel)
            {
                return;
            }
            WriteSettings();
            if (choice == DialogResult.Yes)
            {
                await SendCommandAsync("/restart");
            }
            else
            {
                AppendOutput("[saved; the mesh keeps running on the settings it booted with]");
            }
            return;
        }
        WriteSettings();
    }

    /// <summary>
    /// Writes the editor's values over the file that is already there.
    /// </summary>
    /// <remarks>
    /// Loading first is what keeps this honest: the tab does not show every
    /// setting the mesh has, and building a fresh object would quietly reset
    /// each one it does not show -- the evolution objective, the promotion
    /// policy, the prompt budgets -- to a default nobody asked for.
    /// </remarks>
    private void WriteSettings()
    {
        var settings = EvoMeshYamlSettings.Load(_configPath);
        settings.EnvironmentName = _environmentName.Text.Trim();
        settings.DataPath = _dataPath.Text.Trim();
        settings.GenerationPath = _generationPath.Text.Trim();
        settings.LogLevel = _logLevel.Text;
        settings.DefaultProvider = _defaultProvider.Text;
        settings.Evolution.AutoRestart = _autoRestart.Checked;
        settings.Git.AuthorName = _gitAuthorName.Text.Trim();
        settings.Git.AuthorEmail = _gitAuthorEmail.Text.Trim();
        settings.Git.AutoPush = _gitAutoPush.Checked;
        settings.Git.Remote = _gitRemote.Text.Trim();
        settings.Git.Branch = _gitBranch.Text.Trim();
        settings.Telegram.Enabled = _telegramEnabled.Checked;
        settings.Telegram.Token = _telegramToken.Text.Trim();
        settings.Telegram.AllowedChatIds = _telegramChats.Text.Trim();
        settings.Telegram.AdoptFirstChat = _telegramAdoptFirst.Checked;
        settings.Telegram.Announcements = _telegramAnnouncements.Checked;
        foreach (var (name, controls) in _providers)
        {
            var existing = settings.Providers.GetValueOrDefault(name);
            settings.Providers[name] = new ProviderEditorSettings(
                controls.Url.Text.Trim(),
                controls.Model.Text.Trim(),
                controls.Key.Text,
                existing?.TimeoutSeconds ?? 600);
        }
        foreach (var (agentId, controls) in _systemAgents)
        {
            settings.SystemAgents[agentId] = new AgentModelEditorSettings(
                controls.Provider.Text,
                controls.Model.Text.Trim());
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
        _running = running;
        RenderStatus();
        _start.Enabled = !running;
        _stop.Enabled = running;
        // The settings stay editable while the mesh runs: saving now offers the
        // restart that makes them take effect, which beats making a human stop
        // the mesh by hand just to type a token into a box.
        _settingsNotice.Text = running
            ? "Mesh is running. Saving asks whether to restart it so the new settings take effect."
            : "Mesh is stopped. Settings can be edited and will apply on the next start.";
    }

    /// <summary>
    /// Puts the time of the last check on screen next to the verdict. A status
    /// with no timestamp cannot be told apart from one nobody has re-examined
    /// since the window opened, which is exactly the confusion this fixes.
    /// </summary>
    private void ShowHealthCheck(bool running, DateTimeOffset when)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => ShowHealthCheck(running, when));
            return;
        }
        _running = running;
        _lastCheck = when;
        RenderStatus();
    }

    private void RenderStatus()
    {
        var checkedAt = _lastCheck is null ? "" : $"  checked {_lastCheck:HH:mm:ss}";
        _status.Text = (_running ? "● RUNNING" : "● STOPPED") + checkedAt;
        _status.ForeColor = _running ? Color.FromArgb(78, 220, 130) : Color.FromArgb(255, 170, 120);
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

    private void AddHeading(TableLayoutPanel grid, string text, ref int row)
    {
        var heading = new Label
        {
            Text = text,
            AutoSize = true,
            Font = new Font(Font, FontStyle.Bold),
            Margin = new Padding(3, 18, 3, 6),
        };
        grid.Controls.Add(heading, 0, row);
        grid.SetColumnSpan(heading, 4);
        row++;
    }

    private static void AddHelp(TableLayoutPanel grid, string text, ref int row)
    {
        var help = new Label
        {
            Text = text,
            AutoSize = true,
            MaximumSize = new Size(880, 0),
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 8),
        };
        grid.Controls.Add(help, 0, row);
        grid.SetColumnSpan(help, 4);
        row++;
    }

    private static CheckBox AddCheck(TableLayoutPanel grid, string label, int column, int row)
    {
        var field = new CheckBox
        {
            Text = label,
            AutoSize = true,
            Anchor = AnchorStyles.Left,
            Margin = new Padding(3, 8, 8, 8),
        };
        grid.Controls.Add(field, column, row);
        grid.SetColumnSpan(field, 2);
        return field;
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
