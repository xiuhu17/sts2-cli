using MegaCrit.Sts2.Core.HoverTips;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Headless;

/// <summary>
/// Debug-only audit of the text the adapter derives from the game's models (the tests run
/// it in both languages):
///   raw_names     - a model whose name renders as a loc key ("SPEED_POTION_POWER.title") or
///                   empty: every card/relic/potion/power/enchantment/affliction/monster/event.
///   dropped_tips  - a hover tip the game shows for a card/relic/potion/power that our "tips"
///                   list leaves out (unformattable text, bare keys).
///   unexplained   - a [gold] term in a description that names a keyword or power but has no
///                   tip, neither ours nor the game's (the game itself shows none: kept as is).
///   explained_by_us_only - never filled: our tips are the game's tips, nothing added.
/// </summary>
public partial class RunSimulator
{
    private static readonly System.Text.RegularExpressions.Regex GoldTerm =
        new(@"\[gold\]([^\[\]{}]+?)\[/gold\]", System.Text.RegularExpressions.RegexOptions.Compiled);

    internal static Dictionary<string, object?> AuditText()
    {
        var rawNames = new List<Dictionary<string, object?>>();
        var dropped = new List<Dictionary<string, object?>>();
        var unexplained = new List<Dictionary<string, object?>>();
        int names = 0, tipped = 0;

        // Titles a [gold] term can refer to: card keywords, static tips (Block, ...), powers.
        var tipTerms = new HashSet<string>();
        foreach (var (table, suffix) in new[] { ("card_keywords", ".title"), ("static_hover_tips", ".title"), ("powers", ".title") })
            foreach (var k in _loc.KeysWithPrefix(table, ""))
                if (k.EndsWith(suffix) && _loc.Raw(table, k) is string v)
                    tipTerms.Add(LocLookup.Clean(v).Trim());

        static bool Bad(string? n) => string.IsNullOrWhiteSpace(n) || IsBareKey(n!) || n!.Contains(".title")
                                      || n.Contains(".name") || n.Contains('{');

        foreach (var m in ModelDb.All.ToList())
        {
            string kind = m switch
            {
                CardModel => "card", RelicModel => "relic", PotionModel => "potion", PowerModel => "power",
                EnchantmentModel => "enchantment", AfflictionModel => "affliction", MonsterModel => "monster",
                EventModel => "event", _ => "",
            };
            if (kind == "") continue;
            string? name;
            try { name = ModelName(m); } catch (Exception ex) { name = "!" + ex.GetType().Name; }
            names++;
            if (Bad(name)) rawNames.Add(new() { ["kind"] = kind, ["id"] = m.Id.Entry, ["name"] = name });

            // Tips: the game's list against ours.
            IEnumerable<IHoverTip>? gameTips = null;
            string table = "";
            try
            {
                switch (m)
                {
                    case CardModel c: gameTips = c.HoverTips; table = "cards"; break;
                    case RelicModel r: gameTips = r.HoverTipsExcludingRelic; table = "relics"; break;
                    case PotionModel p: gameTips = p.ExtraHoverTips; table = "potions"; break;
                    case PowerModel pw: gameTips = PowerExtraTips(pw); table = "powers"; break;
                }
            }
            catch { gameTips = null; }
            if (table == "") continue;
            List<IHoverTip> game;
            try { game = IHoverTip.RemoveDupes((gameTips ?? Enumerable.Empty<IHoverTip>()).Where(x => x != null).ToList()).ToList(); }
            catch { game = new(); }
            var ours = Tips(() => game) ?? new();
            tipped++;
            var ourTitles = new HashSet<string>(ours.Select(x => x.GetValueOrDefault("title") as string ?? ""));
            var gameTitles = new HashSet<string>();
            foreach (var tip in game)
            {
                string? title = null;
                try
                {
                    title = tip switch
                    {
                        HoverTip ht => LocLookup.Clean(_loc.ResolveKey(ht.Title ?? "") ?? ht.Title ?? "").Trim(),
                        CardHoverTip ch => CardName(ch.Card),
                        _ => null,
                    };
                }
                catch { }
                if (string.IsNullOrEmpty(title)) continue;
                gameTitles.Add(title);
                if (!ourTitles.Contains(title))
                    dropped.Add(new() { ["kind"] = kind, ["id"] = m.Id.Entry, ["tip"] = title });
            }
            // [gold] terms naming a keyword/power with no tip at all.
            var raw = _loc.Raw(table, m.Id.Entry + ".description") ?? "";
            if (m is PowerModel && _loc.Raw(table, m.Id.Entry + ".smartDescription") is string sd) raw += " " + sd;
            foreach (System.Text.RegularExpressions.Match g in GoldTerm.Matches(raw))
            {
                var term = LocLookup.Clean(g.Groups[1].Value).Trim();
                if (term.Length == 0 || term == name || !tipTerms.Contains(term)) continue;
                // "Summon" is explained by the "Summon 6" tip, "Stun" by "Stunned".
                bool Covers(string tt) => tt == term || tt.StartsWith(term) || term.StartsWith(tt);
                if (ourTitles.Any(Covers) || gameTitles.Any(Covers)) continue;
                if (unexplained.Any(u => u["kind"] as string == kind && u["id"] as string == m.Id.Entry && u["term"] as string == term)) continue;
                unexplained.Add(new() { ["kind"] = kind, ["id"] = m.Id.Entry, ["term"] = term });
            }
        }
        return new Dictionary<string, object?>
        {
            ["type"] = "text_audit",
            ["lang"] = _loc.Lang,
            ["names_checked"] = names,
            ["tips_checked"] = tipped,
            ["raw_names"] = rawNames,
            ["dropped_tips"] = dropped,
            ["unexplained"] = unexplained,
        };
    }
}
