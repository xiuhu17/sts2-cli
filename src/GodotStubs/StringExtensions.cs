namespace Godot;

public static class StringExtensions
{
    // ActModel.DefeatedAllEnemiesAchievement / CharacterModel.RunWonAchievement
    public static string Capitalize(this string instance)
    {
        var words = instance.Replace('_', ' ').Split(' ', StringSplitOptions.RemoveEmptyEntries);
        return string.Join(" ", words.Select(w => char.ToUpperInvariant(w[0]) + w.Substring(1).ToLowerInvariant()));
    }

    // CrystalSphereItem.TexturePath: "CrystalSphereGold" -> "crystal_sphere_gold"
    public static string ToSnakeCase(this string instance) =>
        System.Text.RegularExpressions.Regex.Replace(instance, "(?<=[a-z0-9])([A-Z])", "_$1").ToLowerInvariant();

    // Preserve Godot resource/user prefixes rather than using OS path semantics.
    public static string PathJoin(this string instance, string file)
    {
        if (instance.Length == 0) return file;
        if (instance.EndsWith('/') || file.StartsWith('/')) return instance + file;
        return instance + "/" + file;
    }
}
