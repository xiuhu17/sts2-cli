using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Sts2Headless;

class Program
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
        // WhenWritingNull only applies to object properties; this also drops null values of
        // nested dictionaries (e.g. a card's "keywords": null), which make up most of them.
        Converters = { new SkipNullValuesConverter() },
    };

    private sealed class SkipNullValuesConverter : JsonConverter<Dictionary<string, object?>>
    {
        public override Dictionary<string, object?>? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
            throw new NotSupportedException();

        public override void Write(Utf8JsonWriter writer, Dictionary<string, object?> value, JsonSerializerOptions options)
        {
            // A response's own top-level fields are its schema and always present; nested
            // objects (cards, relics, powers, ...) omit their null fields.
            var skipNulls = writer.CurrentDepth > 0;
            writer.WriteStartObject();
            foreach (var (k, v) in value)
            {
                if (v == null)
                {
                    if (skipNulls) continue;
                    writer.WriteNull(k);
                    continue;
                }
                writer.WritePropertyName(k);
                JsonSerializer.Serialize(writer, v, v.GetType(), options);
            }
            writer.WriteEndObject();
        }
    }

    /// <summary>
    /// Locate the directory containing sts2.dll: STS2_LIB env, walk up from BaseDirectory, then BaseDirectory/lib.
    /// </summary>
    private static string ResolveLibDirectory()
    {
        var envLib = Environment.GetEnvironmentVariable("STS2_LIB");
        if (!string.IsNullOrWhiteSpace(envLib))
        {
            var p = Path.GetFullPath(envLib.Trim());
            if (Directory.Exists(p) && File.Exists(Path.Combine(p, "sts2.dll")))
                return p;
        }

        var dir = AppContext.BaseDirectory;
        for (var depth = 0; depth < 16 && !string.IsNullOrEmpty(dir); depth++)
        {
            var candidate = Path.Combine(dir, "lib");
            if (Directory.Exists(candidate) && File.Exists(Path.Combine(candidate, "sts2.dll")))
                return Path.GetFullPath(candidate);
            dir = Directory.GetParent(dir)?.FullName ?? "";
        }

        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "lib"));
    }

    // stdout carries only protocol JSON. Console.Out is pointed at stderr so that output
    // from game/library code (e.g. Sentry's startup notice) can't interleave with it.
    // Responses are written as raw UTF-8, one write() per line (Console.Out would flush every
    // 256 chars and wake the reader on partial lines).
    private static Stream ProtocolOut = Stream.Null;
    private static readonly object ProtocolLock = new();
    private static System.Buffers.ArrayBufferWriter<byte> ProtocolBuf = new(1 << 16);
    private static Utf8JsonWriter? ProtocolJson;

    static void Main(string[] args)
    {
        // Assigned here, not only in the initializer: static field initializers may run
        // lazily (beforefieldinit), i.e. after SetOut below.
        ProtocolOut = Console.OpenStandardOutput();
        Console.SetOut(Console.Error);

        // An unhandled exception on a non-main thread terminates the process. Tell the
        // client why before that happens instead of leaving it with a bare EOF.
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
        {
            Console.Error.WriteLine($"[FATAL] Unhandled: {e.ExceptionObject}");
            try
            {
                var ex = e.ExceptionObject as Exception;
                WriteLine(new Dictionary<string, object?>
                {
                    ["type"] = "fatal",
                    ["message"] = ex != null ? $"{ex.GetType().Name}: {ex.Message}" : e.ExceptionObject?.ToString(),
                    ["stack_trace"] = e.ExceptionObject?.ToString(),
                });
            }
            catch { }
        };
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Console.Error.WriteLine($"[WARN] Unobserved task exception: {e.Exception}");
            e.SetObserved();
        };

        var libDir = ResolveLibDirectory();

        AssemblyLoadContext.Default.Resolving += (ctx, name) =>
        {
            var path = Path.Combine(libDir, name.Name + ".dll");
            if (File.Exists(path))
                return ctx.LoadFromAssemblyPath(Path.GetFullPath(path));

            // Also check game directory (via STS2_GAME_DIR env var)
            var gameDir = Environment.GetEnvironmentVariable("STS2_GAME_DIR") ?? "";
            if (!string.IsNullOrEmpty(gameDir))
            {
                path = Path.Combine(gameDir, name.Name + ".dll");
                if (File.Exists(path))
                    return ctx.LoadFromAssemblyPath(path);
            }

            return null;
        };

        // All game logic runs on one dedicated engine thread, which also reads the commands and
        // writes the responses, so a normal command costs no cross-thread handoff. This thread
        // is the watchdog: if a command runs past its deadline (a game flow that never
        // completes), it answers engine_timeout itself and takes over stdin, refusing further
        // commands so the client restarts the process.
        var timeout = TimeSpan.FromSeconds(
            double.TryParse(Environment.GetEnvironmentVariable("STS2_COMMAND_TIMEOUT"), out var secs) && secs > 0 ? secs : 30);
        var loop = new EngineLoop(timeout);
        var engineThread = new Thread(loop.Run) { IsBackground = true, Name = "sts2-engine" };
        engineThread.Start();
        loop.Watch(engineThread);
    }

    static bool IsQuit(JsonElement cmd) =>
        cmd.ValueKind == JsonValueKind.Object && cmd.TryGetProperty("cmd", out var c)
        && c.ValueKind == JsonValueKind.String && c.GetString() == "quit";

    /// <summary>
    /// The engine thread's read/execute/write loop plus the watchdog that runs on the main
    /// thread. The watchdog never sits on the per-command path: it only polls a timestamp.
    /// </summary>
    private sealed class EngineLoop
    {
        private const int Idle = 0, Running = 1, TimedOut = 2;
        private int _state = Idle;
        private long _started;          // Stopwatch timestamp of the running work item
        private long _deadlineTicks;    // its allowed duration, in Stopwatch ticks
        private string _what = "";      // the command line (for the log)
        private readonly TimeSpan _timeout;

        public EngineLoop(TimeSpan timeout) => _timeout = timeout;

        private void Begin(string what, TimeSpan limit)
        {
            _what = what;
            Volatile.Write(ref _deadlineTicks, (long)(limit.TotalSeconds * System.Diagnostics.Stopwatch.Frequency));
            Volatile.Write(ref _started, System.Diagnostics.Stopwatch.GetTimestamp());
            Volatile.Write(ref _state, Running);
        }

        /// <summary>False if the watchdog already gave up on this work item (it then owns stdin/stdout).</summary>
        private bool End() => Interlocked.CompareExchange(ref _state, Idle, Running) == Running;

        public void Run()
        {
            // Constructing the simulator and the one-time engine warm-up get a generous
            // deadline of their own; a command read meanwhile simply waits in the pipe.
            var initLimit = _timeout > TimeSpan.FromMinutes(5) ? _timeout : TimeSpan.FromMinutes(5);
            Begin("<startup>", initLimit);
            RunSimulator sim;
            try { sim = new RunSimulator(); }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[FATAL] RunSimulator init: {ex}");
                if (End()) WriteLine(new Dictionary<string, object?> { ["type"] = "fatal", ["message"] = $"{ex.GetType().Name}: {ex.Message}", ["stack_trace"] = ex.ToString() });
                Environment.Exit(1);
                return;
            }
            if (!End()) Park();
            WriteLine(new Dictionary<string, object?> { ["type"] = "ready", ["version"] = "0.2.0" });

            // Do the one-time engine initialisation (Harmony patches, localization, ModelDb, ...)
            // now instead of inside the client's first start_run/load_save. It has no inputs,
            // so the result is identical; a command sent meanwhile waits for it.
            if (Environment.GetEnvironmentVariable("STS2_NO_WARMUP") != "1")
            {
                Begin("<warmup>", initLimit);
                try { RunSimulator.Warmup(); }
                catch (Exception ex) { Console.Error.WriteLine($"[WARN] warmup: {ex}"); }
                if (!End()) Park();
            }

            string? line;
            while ((line = Console.ReadLine()) != null)
            {
                line = line.Trim();
                if (string.IsNullOrEmpty(line)) continue;

                JsonElement cmd;
                try { cmd = JsonSerializer.Deserialize<JsonElement>(line); }
                catch (JsonException ex)
                {
                    WriteLine(new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Invalid JSON: {ex.Message}" });
                    continue;
                }

                Dictionary<string, object?>? result;
                Begin(line, _timeout);
                try
                {
                    result = HandleCommand(sim, cmd);
                    if (result != null) LegalActions.Attach(result);
                }
                catch (Exception ex)
                {
                    result = new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"{ex.GetType().Name}: {ex.Message}", ["stack_trace"] = ex.ToString() };
                }
                RunSimulator.AfterCommand();
                if (!End()) Park();  // too late: the client already got engine_timeout

                if (result != null)
                {
                    WriteLine(result);
                    if (result.TryGetValue("type", out var resultTypeObj) &&
                        string.Equals(resultTypeObj as string, "quit_result", StringComparison.Ordinal))
                        return;
                }
            }
        }

        // A work item finished after the watchdog answered for it. Stay off stdin/stdout for good.
        private static void Park() => Thread.Sleep(Timeout.Infinite);

        /// <summary>Main thread: wait for the engine thread to exit, answering for it if it wedges.</summary>
        public void Watch(Thread engineThread)
        {
            var pollMs = (int)Math.Clamp(_timeout.TotalMilliseconds / 10, 5, 100);
            while (true)
            {
                var wait = pollMs;
                if (Volatile.Read(ref _state) == Running)
                {
                    var elapsed = System.Diagnostics.Stopwatch.GetTimestamp() - Volatile.Read(ref _started);
                    var left = Volatile.Read(ref _deadlineTicks) - elapsed;
                    if (left <= 0)
                    {
                        if (Interlocked.CompareExchange(ref _state, TimedOut, Running) == Running)
                        {
                            Wedged();
                            return;
                        }
                        continue;
                    }
                    wait = (int)Math.Min(pollMs, left * 1000 / System.Diagnostics.Stopwatch.Frequency + 1);
                }
                if (engineThread.Join(wait)) return;
            }
        }

        private void Wedged()
        {
            var startup = _what.StartsWith('<');
            if (startup)
                Console.Error.WriteLine($"[ERROR] Engine {_what.Trim('<', '>')} did not finish; refusing commands");
            else
            {
                Console.Error.WriteLine($"[ERROR] Command timed out after {_timeout.TotalSeconds}s: {_what}");
                WriteLine(new Dictionary<string, object?>
                {
                    ["type"] = "error", ["code"] = "engine_timeout",
                    ["message"] = $"Command did not finish within {_timeout.TotalSeconds}s; the engine is stuck. Restart the process.",
                });
            }
            // The engine thread is stuck inside a work item, not in ReadLine, so stdin is ours now.
            string? line;
            while ((line = Console.ReadLine()) != null)
            {
                line = line.Trim();
                if (string.IsNullOrEmpty(line)) continue;
                JsonElement cmd;
                try { cmd = JsonSerializer.Deserialize<JsonElement>(line); }
                catch (JsonException ex)
                {
                    WriteLine(new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Invalid JSON: {ex.Message}" });
                    continue;
                }
                if (IsQuit(cmd))
                {
                    WriteLine(new Dictionary<string, object?> { ["type"] = "quit_result", ["success"] = true, ["save"] = null });
                    Environment.Exit(0);  // the engine thread is stuck; don't wait for it
                }
                WriteLine(new Dictionary<string, object?>
                {
                    ["type"] = "error", ["code"] = "restart_required",
                    ["message"] = "A previous command timed out and the engine is stuck; restart the process.",
                });
            }
        }
    }

    static Dictionary<string, object?>? HandleCommand(RunSimulator sim, JsonElement cmd)
    {
        var cmdType = cmd.GetProperty("cmd").GetString() ?? "";
        switch (cmdType)
        {
            case "start_run":
                return sim.StartRun(
                    cmd.TryGetProperty("character", out var ch) ? ch.GetString() ?? "Ironclad" : "Ironclad",
                    cmd.TryGetProperty("ascension", out var asc) ? asc.GetInt32() : 0,
                    cmd.TryGetProperty("seed", out var s) ? s.GetString() : null,
                    cmd.TryGetProperty("lang", out var lang) ? lang.GetString() ?? "en" : "en",
                    ReadBool(cmd, "manual_rewards"),
                    cmd.TryGetProperty("modifiers", out var mods) && mods.ValueKind == JsonValueKind.Array
                        ? mods.EnumerateArray().Select(m => m.GetString() ?? "").ToList() : null,
                    cmd.TryGetProperty("game_mode", out var gm) ? gm.GetString() : null,
                    cmd.TryGetProperty("profile", out var prof) ? prof.GetString() : null
                );

            case "action":
            {
                var action = cmd.GetProperty("action").GetString() ?? "";
                Dictionary<string, object?>? actionArgs = null;
                if (cmd.TryGetProperty("args", out var argsElem))
                {
                    actionArgs = new Dictionary<string, object?>();
                    foreach (var prop in argsElem.EnumerateObject())
                    {
                        actionArgs[prop.Name] = prop.Value.ValueKind switch
                        {
                            JsonValueKind.Number => prop.Value.GetInt32(),
                            JsonValueKind.String => prop.Value.GetString(),
                            JsonValueKind.True => true,
                            JsonValueKind.False => false,
                            // [0, 2] -> "0,2", the form list-valued args (indices) use.
                            JsonValueKind.Array => string.Join(",", prop.Value.EnumerateArray().Select(v => v.ToString())),
                            _ => prop.Value.ToString(),
                        };
                    }
                }
                return sim.ExecuteAction(action, actionArgs);
            }

            case "load_save":
            {
                var savePath = cmd.TryGetProperty("path", out var sp) ? sp.GetString() : null;
                var saveJson = cmd.TryGetProperty("json", out var sj) ? sj.GetString() : null;
                if (saveJson == null && savePath != null)
                {
                    if (!File.Exists(savePath))
                        return new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Save file not found: {savePath}" };
                    saveJson = File.ReadAllText(savePath);
                }
                if (saveJson == null)
                    return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Provide 'path' or 'json' for load_save" };
                var loadLang = cmd.TryGetProperty("lang", out var le) ? (le.GetString() ?? "en") : "en";
                return sim.LoadSave(saveJson, loadLang, ReadBool(cmd, "manual_rewards"));
            }
            case "get_map":
                return sim.GetFullMap();

            case "__debug_sleep" when Environment.GetEnvironmentVariable("STS2_DEBUG_COMMANDS") == "1":
                // Test hook for the command watchdog: blocks the engine thread.
                Thread.Sleep(cmd.TryGetProperty("ms", out var ms) ? ms.GetInt32() : 60000);
                return new Dictionary<string, object?> { ["type"] = "ok" };

            case "__debug_stats" when Environment.GetEnvironmentVariable("STS2_DEBUG_COMMANDS") == "1":
                return RunSimulator.DebugStats();

            case "set_player":
            {
                var args = new Dictionary<string, JsonElement>();
                foreach (var prop in cmd.EnumerateObject())
                    if (prop.Name != "cmd") args[prop.Name] = prop.Value;
                return sim.SetPlayer(args);
            }

            case "enter_room":
            {
                var roomType = cmd.TryGetProperty("type", out var rt) ? rt.GetString() ?? "" : "";
                var encounter = cmd.TryGetProperty("encounter", out var enc) ? enc.GetString() : null;
                var eventId = cmd.TryGetProperty("event", out var ev) ? ev.GetString() : null;
                return sim.EnterRoom(roomType, encounter, eventId);
            }

            case "set_draw_order":
            {
                var cards = new List<string>();
                if (cmd.TryGetProperty("cards", out var cardsArr))
                    foreach (var c in cardsArr.EnumerateArray())
                        cards.Add(c.GetString() ?? "");
                return sim.SetDrawOrder(cards);
            }

            case "write_continue_save":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
                return sim.SaveCheckpoint(outputPath);
            }

            case "quit":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
                if (!string.IsNullOrEmpty(outputPath))
                {
                    var saveResult = sim.SaveCheckpoint(outputPath);
                    bool saveOk = saveResult.TryGetValue("success", out var sObj) && sObj is bool b && b;
                    if (!saveOk)
                    {
                        // Save failed — do NOT clean up so the caller can retry with a different path.
                        return new Dictionary<string, object?>
                        {
                            ["type"] = "save_error",
                            ["save"] = saveResult,
                        };
                    }
                    sim.CleanUp();
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "quit_result",
                        ["success"] = true,
                        ["save"] = saveResult,
                    };
                }
                sim.CleanUp();
                return new Dictionary<string, object?>
                {
                    ["type"] = "quit_result",
                    ["success"] = true,
                    ["save"] = null,
                };
            }

            default:
                return new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Unknown command: {cmdType}" };
        }
    }

    static bool ReadBool(JsonElement cmd, string name) =>
        cmd.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    static void WriteLine(Dictionary<string, object?> data)
    {
        lock (ProtocolLock)
        {
            ProtocolBuf.Clear();
            var w = ProtocolJson ??= new Utf8JsonWriter(ProtocolBuf, new JsonWriterOptions { Encoder = JsonOpts.Encoder });
            w.Reset(ProtocolBuf);
            try { JsonSerializer.Serialize(w, data, JsonOpts); }
            finally { w.Flush(); }
            System.Buffers.BuffersExtensions.Write(ProtocolBuf, "\n"u8);
            ProtocolOut.Write(ProtocolBuf.WrittenSpan);
            ProtocolOut.Flush();
            // Don't keep a multi-MB buffer alive after one huge response.
            if (ProtocolBuf.Capacity > 1 << 22) ProtocolBuf = new(1 << 16);
        }
    }
}
