namespace Sts2Headless;

/// <summary>
/// What the headless UI guard rewrites. Shared by the runtime Harmony guard (HeadlessUiGuard)
/// and the setup-time Cecil rewrite (tools/UiGuardPatcher), which stamps lib/sts2.dll with
/// <see cref="Stamp"/> so the runtime can skip the rewrite. Bump <see cref="Version"/> whenever
/// the rewrite itself changes; changing a namespace list changes the stamp automatically.
/// </summary>
internal static class UiGuardSpec
{
    public const int Version = 1;

    /// <summary>AssemblyMetadataAttribute key carrying the stamp in a pre-guarded sts2.dll.</summary>
    public const string MetadataKey = "Sts2Headless.UiGuard";

    // Receivers in these namespaces are presentation-only (nodes, VFX, audio, Godot types).
    public static readonly string[] UiNamespaces =
    {
        "MegaCrit.Sts2.Core.Nodes", "MegaCrit.Sts2.addons", "MegaCrit.Sts2.Core.Bindings",
        "MegaCrit.Sts2.Core.Animation", "MegaCrit.Sts2.Core.Audio", "Godot",
    };

    // Callers in these namespaces are game logic and get guarded.
    public static readonly string[] LogicNamespaces =
    {
        "MegaCrit.Sts2.Core.Models", "MegaCrit.Sts2.Core.Commands", "MegaCrit.Sts2.Core.Events",
        "MegaCrit.Sts2.Core.Entities", "MegaCrit.Sts2.Core.Combat", "MegaCrit.Sts2.Core.Rewards",
        "MegaCrit.Sts2.Core.Rooms", "MegaCrit.Sts2.Core.Runs", "MegaCrit.Sts2.Core.Hooks",
        "MegaCrit.Sts2.Core.GameActions", "MegaCrit.Sts2.Core.MonsterMoves", "MegaCrit.Sts2.Core.Helpers",
    };

    public static string Stamp =>
        $"v{Version};ui={string.Join(",", UiNamespaces)};logic={string.Join(",", LogicNamespaces)}";

    /// <summary>Namespace match used by both implementations ("A" matches "A" and "A.B", not "AB").</summary>
    public static bool InNamespaces(string? ns, string[] namespaces)
    {
        ns ??= "";
        foreach (var n in namespaces)
            if (ns == n || ns.StartsWith(n + ".", StringComparison.Ordinal)) return true;
        return false;
    }
}
