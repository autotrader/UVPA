using System.Text.RegularExpressions;

namespace GraphBuilder;

/// <summary>Eine im Text erkannte Entität, wird zu einem Knoten im Graphen.</summary>
/// <param name="Type">vorlage | ort | bplan</param>
/// <param name="Key">Normalisierter Schlüssel, z. B. "611/327/2020", "Büchenbach", "472"</param>
/// <param name="Label">Anzeigename für das Frontend</param>
public readonly record struct Entity(string Type, string Key, string Label);

/// <summary>
/// Regelbasierte Entitäten-Extraktion (bewusst ohne KI): Vorlagen-Aktenzeichen,
/// Erlanger Stadtteile und Bebauungsplan-Nummern.
/// </summary>
public static partial class EntityExtractor
{
    // Vorlagen wie "611/327/2020", "610.3/091/2020", "VI/248/2024".
    // Der mittlere Teil ist immer dreistellig — das schließt Datumsangaben
    // im Format 19/05/2020 zuverlässig aus.
    [GeneratedRegex(@"\b([A-Za-z0-9]{1,4}(?:\.\d)?/\d{3}/20\d{2})\b")]
    private static partial Regex VorlageRegex();

    [GeneratedRegex(@"Bebauungsplan(?:es|s)?\s+Nr\.?\s*(\d+)", RegexOptions.IgnoreCase)]
    private static partial Regex BplanRegex();

    // Erlanger Stadtteile / Ortsteile. Case-sensitiv mit Wortgrenzen, damit
    // z. B. "Anger" nicht in "Anlieger" oder "Angerstraße" matcht.
    [GeneratedRegex(@"\b(Alterlangen|Büchenbach|Bruck|Burgberg|Dechsendorf|Eltersdorf|Frauenaurach|Häusling|Hüttendorf|Innenstadt|Kosbach|Kriegenbrunn|Röthelheim|Sieglitzhof|Steudach|Tennenlohe|Anger)\b")]
    private static partial Regex StadtteilRegex();

    /// <summary>Extrahiert alle Entitäten aus einem Text (dedupliziert).</summary>
    public static HashSet<Entity> Extract(string text)
    {
        var entities = new HashSet<Entity>();
        if (string.IsNullOrEmpty(text))
            return entities;

        foreach (Match m in VorlageRegex().Matches(text))
        {
            var key = m.Groups[1].Value.ToUpperInvariant();
            entities.Add(new Entity("vorlage", key, key));
        }

        foreach (Match m in BplanRegex().Matches(text))
        {
            var nr = m.Groups[1].Value.TrimStart('0');
            if (nr.Length > 0)
                entities.Add(new Entity("bplan", nr, $"B-Plan {nr}"));
        }

        foreach (Match m in StadtteilRegex().Matches(text))
        {
            var name = m.Groups[1].Value;
            entities.Add(new Entity("ort", name, name));
        }

        return entities;
    }

    /// <summary>Normalisiert eine Vorlagen-Nummer aus index.json auf den Entitäts-Key.</summary>
    public static string NormalizeVorlage(string vorlageNr) =>
        vorlageNr.Trim().ToUpperInvariant();
}
