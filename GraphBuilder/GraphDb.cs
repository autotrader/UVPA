using DuckDB.NET.Data;

namespace GraphBuilder;

/// <summary>Zeile für die documents-Tabelle (Volltext-Basis für FTS).</summary>
public sealed record DocumentRow(
    string Id, string FileId, string NodeId, string TypeCode,
    string Title, string Path, string Url, int Pages, string? Text);

/// <summary>Knoten des Graphen: Sitzungen, TOPs und Entitäten (Vorlage/Ort/B-Plan).</summary>
public sealed record NodeRow(
    string Id, string Type, string Label,
    DateTime? Date, string? TopNr, string? VorlageNr, string? Folder);

public sealed record EdgeRow(string Source, string Target, string Type, int Weight);

/// <summary>Kapselt Schema, Bulk-Inserts (Appender) und FTS-Aufbau der graph.db.</summary>
public sealed class GraphDb : IDisposable
{
    private readonly DuckDBConnection _conn;

    public GraphDb(string dbPath)
    {
        if (File.Exists(dbPath))
            File.Delete(dbPath);
        _conn = new DuckDBConnection($"Data Source={dbPath}");
        _conn.Open();
    }

    public void CreateSchema() => Execute(
        """
        CREATE TABLE nodes (
            id         VARCHAR PRIMARY KEY,  -- 's:2020-05-19', 't:5046107', 'v:611/327/2020', 'o:Büchenbach', 'b:472'
            type       VARCHAR NOT NULL,     -- session | top | vorlage | ort | bplan
            label      VARCHAR NOT NULL,
            date       DATE,                 -- Sitzungsdatum (nur session/top)
            top_nr     VARCHAR,
            vorlage_nr VARCHAR,
            folder     VARCHAR               -- Repo-Pfad für Deep-Links ins Frontend
        );

        CREATE TABLE edges (
            source VARCHAR NOT NULL,
            target VARCHAR NOT NULL,
            type   VARCHAR NOT NULL,          -- in_session | has_vorlage | references_vorlage | mentions_ort | mentions_bplan | thread
            weight INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE documents (
            id        VARCHAR PRIMARY KEY,    -- '{node_id}:{file_id}' (eine Datei kann in mehreren Sitzungen hängen)
            file_id   VARCHAR NOT NULL,
            node_id   VARCHAR NOT NULL,       -- zugehöriger TOP- oder Session-Knoten
            type_code VARCHAR,                -- EI | NI | SU | BL | VO | '' (Anlage)
            title     VARCHAR,
            path      VARCHAR,                -- Pfad relativ zur Repo-Wurzel
            url       VARCHAR,                -- Original-URL im Ratsinformationssystem
            pages     INTEGER,
            text      VARCHAR                 -- extrahierter Volltext (NULL = Datei fehlt/Scan)
        );
        """);

    public void InsertNodes(IEnumerable<NodeRow> nodes)
    {
        using var appender = _conn.CreateAppender("nodes");
        foreach (var n in nodes)
        {
            var row = appender.CreateRow();
            row.AppendValue(n.Id).AppendValue(n.Type).AppendValue(n.Label)
               .AppendValue(n.Date).AppendValue(n.TopNr).AppendValue(n.VorlageNr)
               .AppendValue(n.Folder).EndRow();
        }
    }

    public void InsertEdges(IEnumerable<EdgeRow> edges)
    {
        using var appender = _conn.CreateAppender("edges");
        foreach (var e in edges)
        {
            var row = appender.CreateRow();
            row.AppendValue(e.Source).AppendValue(e.Target)
               .AppendValue(e.Type).AppendValue(e.Weight).EndRow();
        }
    }

    public void InsertDocuments(IEnumerable<DocumentRow> docs)
    {
        using var appender = _conn.CreateAppender("documents");
        foreach (var d in docs)
        {
            var row = appender.CreateRow();
            row.AppendValue(d.Id).AppendValue(d.FileId).AppendValue(d.NodeId)
               .AppendValue(d.TypeCode).AppendValue(d.Title).AppendValue(d.Path)
               .AppendValue(d.Url).AppendValue(d.Pages).AppendValue(d.Text).EndRow();
        }
    }

    /// <summary>
    /// Verdichtet den "roten Faden": direkte Kante zwischen zwei TOPs, die dieselbe
    /// Vorlage behandeln (typisch: dieselbe Vorlage über mehrere Sitzungstermine).
    /// </summary>
    public void CreateThreadEdges() => Execute(
        """
        INSERT INTO edges
        SELECT a.source, b.source, 'thread', count(*)::INTEGER
        FROM edges a
        JOIN edges b ON a.target = b.target AND a.source < b.source
        WHERE a.type = 'has_vorlage' AND b.type = 'has_vorlage'
        GROUP BY a.source, b.source;
        """);

    /// <summary>BM25-Volltextindex (deutscher Stemmer) auf Titel + Volltext.</summary>
    public void CreateFtsIndex() => Execute(
        """
        INSTALL fts;
        LOAD fts;
        PRAGMA create_fts_index('documents', 'id', 'title', 'text', stemmer='german', stopwords='none');
        """);

    public long Count(string table)
    {
        using var cmd = _conn.CreateCommand();
        cmd.CommandText = $"SELECT count(*) FROM {table}";
        return (long)cmd.ExecuteScalar()!;
    }

    private void Execute(string sql)
    {
        using var cmd = _conn.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    public void Dispose() => _conn.Dispose();
}
