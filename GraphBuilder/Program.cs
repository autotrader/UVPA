using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using GraphBuilder;

// GraphBuilder — liest die vom Legacy-Skript (uvp_agent.py) erzeugte index.json
// samt heruntergeladener PDFs und baut daraus eine DuckDB-Graphdatenbank
// (Knoten, Kanten, Volltext + FTS/BM25-Index) für das statische Frontend.
//
// Aufruf:  GraphBuilder [repoRoot] [--db graph.db] [--limit N] [--no-text]

Console.OutputEncoding = Encoding.UTF8;

var repoRoot = "";
var dbPath = "graph.db";
var limit = 0;
var extractText = true;

for (var i = 0; i < args.Length; i++)
{
    switch (args[i])
    {
        case "--db": dbPath = args[++i]; break;
        case "--limit": limit = int.Parse(args[++i]); break;
        case "--no-text": extractText = false; break;
        default: repoRoot = args[i]; break;
    }
}

// Repo-Wurzel = Verzeichnis mit index.json (notfalls von cwd aus nach oben suchen)
if (repoRoot.Length == 0)
{
    var dir = Directory.GetCurrentDirectory();
    while (dir is not null && !File.Exists(Path.Combine(dir, "index.json")))
        dir = Path.GetDirectoryName(dir);
    repoRoot = dir ?? throw new FileNotFoundException(
        "index.json nicht gefunden — Repo-Wurzel als Argument angeben.");
}

var indexFile = Path.Combine(repoRoot, "index.json");
var sessions = JsonSerializer.Deserialize<List<SessionRecord>>(File.ReadAllText(indexFile))
               ?? throw new InvalidDataException($"Konnte {indexFile} nicht parsen.");
sessions = [.. sessions.OrderBy(s => s.Date)];
if (limit > 0)
    sessions = [.. sessions.TakeLast(limit)];

Console.WriteLine($"GraphBuilder — {sessions.Count} Sitzungen aus {indexFile}");
var sw = Stopwatch.StartNew();

// ── Phase 1: Struktur aus index.json → Knoten, Kanten, Dokument-Jobs ─────────

var nodes = new Dictionary<string, NodeRow>();
var edges = new List<EdgeRow>();
var docRows = new List<(DocumentRow Row, string? AbsPath, string OwnerTopId)>();
var seenDocIds = new HashSet<string>();

void AddDoc(DocRecord doc, string folder, string nodeId, string ownerTopId)
{
    var id = $"{nodeId}:{doc.FileId}";
    if (!seenDocIds.Add(id))
        return;
    var absPath = Path.Combine(repoRoot, folder, doc.Filename);
    var relPath = $"{folder}/{doc.Filename}";
    var url = $"https://ratsinfo.erlangen.de/{doc.Href}";
    var exists = File.Exists(absPath);
    docRows.Add((
        new DocumentRow(id, doc.FileId, nodeId, doc.TypeCode, doc.Title, relPath, url, 0, null),
        exists ? absPath : null,
        ownerTopId));
}

foreach (var session in sessions)
{
    var date = DateTime.Parse(session.Date);
    var sessionId = $"s:{session.Date}";
    nodes[sessionId] = new NodeRow(
        sessionId, "session", $"Sitzung {session.Date}", date, null, null, session.Folder);

    foreach (var doc in session.HeaderDocs)
        AddDoc(doc, session.Folder, sessionId, "");  // EI/NI/SU: nur Volltext, keine Entitäts-Kanten

    for (var t = 0; t < session.Tops.Count; t++)
    {
        var top = session.Tops[t];

        // Reine Struktur-TOPs ("Mitteilungen zur Kenntnis" etc.) ohne Dokumente
        // und ohne Vorlage tragen nichts zum Beziehungsnetz bei.
        if (top.Docs.Count == 0 && top.VorlageNr.Length == 0)
            continue;

        var topId = top.Ktonr.Length > 0
            ? $"t:{top.Ktonr}"
            : $"t:{session.Date}:{top.TopNrSafe}:{t}";
        nodes[topId] = new NodeRow(
            topId, "top", $"{top.TopNr} {top.Title}", date, top.TopNr, top.VorlageNr, top.Folder);
        edges.Add(new EdgeRow(topId, sessionId, "in_session", 1));

        if (top.VorlageNr.Length > 0)
        {
            var key = EntityExtractor.NormalizeVorlage(top.VorlageNr);
            var vId = $"v:{key}";
            nodes.TryAdd(vId, new NodeRow(vId, "vorlage", key, null, null, key, null));
            edges.Add(new EdgeRow(topId, vId, "has_vorlage", 1));
        }

        foreach (var doc in top.Docs)
            AddDoc(doc, top.Folder, topId, topId);
    }
}

Console.WriteLine($"Struktur: {nodes.Count} Knoten, {docRows.Count} Dokumente eingeplant.");

// ── Phase 2: PDF-Volltexte parallel extrahieren ──────────────────────────────

var texts = new ConcurrentDictionary<string, (string Text, int Pages)>();
if (extractText)
{
    var paths = docRows.Where(d => d.AbsPath is not null).Select(d => d.AbsPath!).Distinct().ToList();
    var done = 0;
    Parallel.ForEach(
        paths,
        new ParallelOptions { MaxDegreeOfParallelism = Environment.ProcessorCount },
        path =>
        {
            texts[path] = PdfText.Extract(path);
            var n = Interlocked.Increment(ref done);
            if (n % 250 == 0)
                Console.WriteLine($"  … {n}/{paths.Count} PDFs gelesen ({sw.Elapsed:mm\\:ss})");
        });
    Console.WriteLine($"Volltext: {paths.Count} PDFs gelesen, " +
        $"{texts.Values.Count(t => t.Text.Length > 0)} mit Text ({sw.Elapsed:mm\\:ss}).");
}

// ── Phase 3: Entitäten aus Titeln + Volltexten → Knoten und Kanten ───────────

// Pro TOP: Fundstellen zählen (Titel + jedes Dokument = 1 Zählung → Kantengewicht)
var topEntityCounts = new Dictionary<string, Dictionary<Entity, int>>();

void CountEntities(string topId, string text, string? ownVorlage)
{
    var found = EntityExtractor.Extract(text);
    if (found.Count == 0)
        return;
    var counts = topEntityCounts.TryGetValue(topId, out var c)
        ? c
        : topEntityCounts[topId] = [];
    foreach (var e in found)
    {
        if (e.Type == "vorlage" && e.Key == ownVorlage)
            continue;  // Selbstreferenz — steckt schon in has_vorlage
        counts[e] = counts.GetValueOrDefault(e) + 1;
    }
}

var finalDocs = new List<DocumentRow>();
foreach (var (row, absPath, ownerTopId) in docRows)
{
    var (text, pages) = absPath is not null && texts.TryGetValue(absPath, out var t)
        ? t : ("", 0);
    finalDocs.Add(row with { Pages = pages, Text = text.Length > 0 ? text : null });

    if (ownerTopId.Length > 0 && text.Length > 0)
    {
        var ownVorlage = nodes[ownerTopId].VorlageNr is { Length: > 0 } v
            ? EntityExtractor.NormalizeVorlage(v) : null;
        CountEntities(ownerTopId, text, ownVorlage);
    }
}

foreach (var node in nodes.Values.Where(n => n.Type == "top").ToList())
{
    var ownVorlage = node.VorlageNr is { Length: > 0 } v
        ? EntityExtractor.NormalizeVorlage(v) : null;
    CountEntities(node.Id, node.Label, ownVorlage);
}

foreach (var (topId, counts) in topEntityCounts)
{
    foreach (var (entity, weight) in counts)
    {
        var (entityId, edgeType) = entity.Type switch
        {
            "vorlage" => ($"v:{entity.Key}", "references_vorlage"),
            "ort" => ($"o:{entity.Key}", "mentions_ort"),
            _ => ($"b:{entity.Key}", "mentions_bplan"),
        };
        nodes.TryAdd(entityId, new NodeRow(
            entityId, entity.Type, entity.Label, null, null,
            entity.Type == "vorlage" ? entity.Key : null, null));
        edges.Add(new EdgeRow(topId, entityId, edgeType, weight));
    }
}

// ── Phase 4: Kuratierte externe Registries einlesen (Pläne, Rechtsvorschriften) ──
// Beide Ordner teilen dasselbe registry.json-Format (siehe PlanRecord) und
// werden identisch verarbeitet — nur Knotentyp/Präfix/Kantentyp unterscheiden sich.

var planRows = new List<PlanRow>();
var planFileRows = new List<PlanFileRow>();

void LoadRegistry(string folderName, string nodeType, string idPrefix, string edgeType)
{
    var folder = Path.Combine(repoRoot, folderName);
    var registryFile = Path.Combine(folder, "registry.json");
    if (!File.Exists(registryFile))
    {
        Console.WriteLine($"Hinweis: {registryFile} nicht gefunden — {folderName} übersprungen.");
        return;
    }

    var items = JsonSerializer.Deserialize<List<PlanRecord>>(File.ReadAllText(registryFile)) ?? [];
    var linkCountBefore = edges.Count(e => e.Type == edgeType);
    foreach (var item in items)
    {
        var nodeId = $"{idPrefix}:{item.Id}";
        var planId = $"{nodeType}:{item.Id}";  // 'plans.id' — eindeutig über beide Registries hinweg
        nodes[nodeId] = new NodeRow(
            nodeId, nodeType, item.Title,
            DateTime.TryParse(item.Erstellt, out var d) ? d : null,
            null, item.RisVorlageNr, null);
        planRows.Add(new PlanRow(planId, nodeType, item.Title, item.Beschreibung, item.QuelleUrl,
            string.Join(",", item.Themen)));

        // Verknüpfung mit dem Beziehungsnetz: primär über die exakte
        // Vorlagen-Nummer (falls in der Registry gepflegt), sonst per
        // Titel-Stichwortabgleich gegen TOP-Titel (grobe Näherung fürs MVP).
        if (item.RisVorlageNr is { Length: > 0 } vNr)
        {
            var vId = $"v:{EntityExtractor.NormalizeVorlage(vNr)}";
            if (nodes.ContainsKey(vId))
                edges.Add(new EdgeRow(nodeId, vId, edgeType, 1));
        }
        else
        {
            var keyword = item.Title.Split(['(', '–', '-'])[0].Trim();
            if (keyword.Length >= 6)
            {
                foreach (var top in nodes.Values.Where(n =>
                    n.Type == "top" && n.Label.Contains(keyword, StringComparison.OrdinalIgnoreCase)))
                {
                    edges.Add(new EdgeRow(nodeId, top.Id, edgeType, 1));
                }
            }
        }

        foreach (var file in item.Dateien)
        {
            var absPath = Path.Combine(repoRoot, file.Pfad);
            var (text, pages) = extractText && File.Exists(absPath)
                ? PdfText.Extract(absPath) : ("", 0);
            planFileRows.Add(new PlanFileRow(
                planId, file.Titel, File.Exists(absPath) ? file.Pfad : null,
                file.QuelleUrl, pages, text.Length > 0 ? text : null));
        }
    }
    Console.WriteLine($"{folderName}: {items.Count} aus {registryFile}, " +
        $"{items.Sum(i => i.Dateien.Count)} Dateien, " +
        $"{edges.Count(e => e.Type == edgeType) - linkCountBefore} Verknüpfungen zum Beziehungsnetz.");
}

LoadRegistry("plaene", "plan", "p", "relates_to_plan");
LoadRegistry("recht", "recht", "r", "relates_to_recht");

// ── Phase 5: DuckDB schreiben + Thread-Kanten + FTS-Index ────────────────────

using (var db = new GraphDb(dbPath))
{
    db.CreateSchema();
    db.InsertNodes(nodes.Values);
    db.InsertEdges(edges);
    db.InsertDocuments(finalDocs);
    db.InsertPlans(planRows);
    db.InsertPlanFiles(planFileRows);
    db.CreateThreadEdges();
    if (extractText)
        db.CreateFtsIndex();

    Console.WriteLine();
    Console.WriteLine($"graph.db geschrieben: {Path.GetFullPath(dbPath)}");
    Console.WriteLine($"  Knoten:    {db.Count("nodes")}");
    Console.WriteLine($"  Kanten:    {db.Count("edges")}");
    Console.WriteLine($"  Dokumente: {db.Count("documents")} " +
        $"(davon {db.Count("documents WHERE text IS NOT NULL")} mit Volltext)");
    Console.WriteLine($"  Pläne:     {db.Count("plans WHERE kind = 'plan'")} " +
        $"({db.Count("plan_files pf JOIN plans p ON p.id = pf.plan_id WHERE p.kind = 'plan'")} Dateien)");
    Console.WriteLine($"  Recht:     {db.Count("plans WHERE kind = 'recht'")} " +
        $"({db.Count("plan_files pf JOIN plans p ON p.id = pf.plan_id WHERE p.kind = 'recht'")} Dateien)");
}

Console.WriteLine($"Fertig in {sw.Elapsed:mm\\:ss}. " +
    $"DB-Größe: {new FileInfo(dbPath).Length / (1024.0 * 1024.0):F1} MB");
