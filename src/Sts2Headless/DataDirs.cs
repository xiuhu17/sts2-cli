namespace Sts2Headless;

/// <summary>
/// Locates the repo's data directories (localization_eng / localization_zhs) independently of
/// where the engine binary was built or published (bin/Debug, bin/Release, bin/R2R, a publish
/// folder, ...): STS2_DATA_DIR, then walking up from the binary, then next to the lib/ folder,
/// then walking up from the working directory.
/// </summary>
internal static class DataDirs
{
    private static readonly Dictionary<string, string> s_found = new();

    public static string Localization(string name)
    {
        lock (s_found)
        {
            if (s_found.TryGetValue(name, out var hit)) return hit;
            var dir = Find(name);
            s_found[name] = dir;
            return dir;
        }
    }

    /// <summary>True if the English localization tables were found (names otherwise degrade to raw keys).</summary>
    public static bool LocalizationFound => HasJson(Localization("localization_eng"));

    private static bool HasJson(string dir) =>
        Directory.Exists(dir) && Directory.EnumerateFiles(dir, "*.json").Any();

    private static string Find(string name)
    {
        foreach (var root in Roots())
        {
            if (string.IsNullOrEmpty(root)) continue;
            var candidate = Path.GetFullPath(Path.Combine(root, name));
            if (HasJson(candidate)) return candidate;
        }
        // Historic location (repo root five levels above bin/<config>/net9.0); reported as missing.
        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", name));
    }

    private static IEnumerable<string> Roots()
    {
        var env = Environment.GetEnvironmentVariable("STS2_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(env)) yield return env.Trim();
        var dir = AppContext.BaseDirectory;
        for (var depth = 0; depth < 16 && !string.IsNullOrEmpty(dir); depth++)
        {
            yield return dir;
            dir = Directory.GetParent(dir)?.FullName ?? "";
        }
        var lib = Environment.GetEnvironmentVariable("STS2_LIB");
        if (!string.IsNullOrWhiteSpace(lib)) yield return Path.Combine(lib.Trim(), "..");
        // An engine published outside the repo, launched from inside it.
        dir = Environment.CurrentDirectory;
        for (var depth = 0; depth < 16 && !string.IsNullOrEmpty(dir); depth++)
        {
            yield return dir;
            dir = Directory.GetParent(dir)?.FullName ?? "";
        }
    }
}
