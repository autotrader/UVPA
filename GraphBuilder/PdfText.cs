using System.Text;
using UglyToad.PdfPig;
using UglyToad.PdfPig.DocumentLayoutAnalysis.TextExtractor;

namespace GraphBuilder;

/// <summary>PDF-Textextraktion via PdfPig (rein verwaltetes .NET, keine nativen Abhängigkeiten).</summary>
public static class PdfText
{
    /// <summary>Obergrenze pro Dokument, damit die DB-Datei nicht explodiert (Kartenwerke etc.).</summary>
    public const int MaxChars = 1_000_000;

    /// <summary>
    /// Liefert (Text, Seitenzahl). Seiten sind mit '\f' getrennt, damit das
    /// Frontend paginiert anzeigen kann. Bei Fehlern oder reinen Scans: leerer Text.
    /// </summary>
    public static (string Text, int Pages) Extract(string path)
    {
        try
        {
            using var pdf = PdfDocument.Open(path);
            var sb = new StringBuilder();
            var pages = 0;
            foreach (var page in pdf.GetPages())
            {
                pages++;
                if (sb.Length < MaxChars)
                {
                    if (pages > 1)
                        sb.Append('\f');
                    sb.Append(ContentOrderTextExtractor.GetText(page));
                }
            }
            var text = sb.Length > MaxChars ? sb.ToString(0, MaxChars) : sb.ToString();
            // Reine Scans liefern nur Whitespace/Seitentrenner → wie "kein Text" behandeln
            return (string.IsNullOrWhiteSpace(text) ? "" : text, pages);
        }
        catch
        {
            // Beschädigte/verschlüsselte PDFs: Dokument bleibt im Graphen, nur ohne Volltext.
            return ("", 0);
        }
    }
}
