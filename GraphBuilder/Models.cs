using System.Text.Json.Serialization;

namespace GraphBuilder;

/// <summary>Eine Sitzung aus index.json (vom Legacy-Python-Skript uvp_agent.py erzeugt).</summary>
public sealed class SessionRecord
{
    [JsonPropertyName("session_id")] public string SessionId { get; set; } = "";
    [JsonPropertyName("date")] public string Date { get; set; } = "";
    [JsonPropertyName("folder")] public string Folder { get; set; } = "";
    [JsonPropertyName("header_docs")] public List<DocRecord> HeaderDocs { get; set; } = [];
    [JsonPropertyName("tops")] public List<TopRecord> Tops { get; set; } = [];
}

/// <summary>Ein Tagesordnungspunkt (TOP) innerhalb einer Sitzung.</summary>
public sealed class TopRecord
{
    [JsonPropertyName("top_nr")] public string TopNr { get; set; } = "";
    [JsonPropertyName("top_nr_safe")] public string TopNrSafe { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("ktonr")] public string Ktonr { get; set; } = "";
    [JsonPropertyName("vorlage_nr")] public string VorlageNr { get; set; } = "";
    [JsonPropertyName("kvonr")] public string Kvonr { get; set; } = "";
    [JsonPropertyName("folder")] public string Folder { get; set; } = "";
    [JsonPropertyName("docs")] public List<DocRecord> Docs { get; set; } = [];
}

/// <summary>Ein Dokument (PDF). type_code: EI, NI, SU, BL, VO oder leer (Anlage).</summary>
public sealed class DocRecord
{
    [JsonPropertyName("file_id")] public string FileId { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("type_code")] public string TypeCode { get; set; } = "";
    [JsonPropertyName("filename")] public string Filename { get; set; } = "";
    [JsonPropertyName("href")] public string Href { get; set; } = "";
}

/// <summary>Ein kuratierter externer Plan/Konzept aus plaene/registry.json.</summary>
public sealed class PlanRecord
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("beschreibung")] public string Beschreibung { get; set; } = "";
    [JsonPropertyName("erstellt")] public string? Erstellt { get; set; }
    [JsonPropertyName("themen")] public List<string> Themen { get; set; } = [];
    [JsonPropertyName("quelle_url")] public string? QuelleUrl { get; set; }
    [JsonPropertyName("dateien")] public List<PlanFile> Dateien { get; set; } = [];
    [JsonPropertyName("ris_vorlage_nr")] public string? RisVorlageNr { get; set; }
}

public sealed class PlanFile
{
    [JsonPropertyName("titel")] public string Titel { get; set; } = "";
    [JsonPropertyName("pfad")] public string Pfad { get; set; } = "";
    [JsonPropertyName("quelle_url")] public string? QuelleUrl { get; set; }
}
