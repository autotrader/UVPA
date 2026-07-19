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
