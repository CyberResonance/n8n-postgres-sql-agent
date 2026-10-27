"""Builds Postgres_SQL_Agent.json: plain-language question -> live schema -> Gemini writes SQL -> read-only SQL guard ->
Postgres query -> Gemini explains the result. Use a read-only database user as the first line of defence; the guard is the second."""
import json
from pathlib import Path

HERE = Path(__file__).parent

SCHEMA_SQL = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
              "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position LIMIT 600;")

PROMPT_JS = r"""// Compact the schema and ask for one read-only PostgreSQL query.
const q = $('Question').first().json.question;
const tables = {};
for (const r of $input.all()) { const j = r.json; if (!j.table_name) continue; (tables[j.table_name] = tables[j.table_name] || []).push(`${j.column_name} ${j.data_type}`); }
const schema = Object.entries(tables).map(([t, c]) => `${t}(${c.join(', ')})`).join('\n');
const prompt = `You write PostgreSQL for a read-only analytics assistant.
Schema (public):
${schema || '(no tables found)'}

Question: ${q}

Rules: exactly ONE statement, SELECT or WITH ... SELECT only, never modify data, use only tables/columns from the schema,
always LIMIT 100 or less, prefer readable column aliases. If the question cannot be answered from the schema, return sql "" and explain why.
Return JSON only: {"sql": "...", "explanation": "one sentence"}`;
return [{ json: { question: q, schema, requestBody: { contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature: 0, responseMimeType: 'application/json' } } } }];"""

GUARD_JS = r"""// Second line of defence (the first is a read-only DB user): allow a single SELECT/WITH statement only.
const q = $('Build SQL Prompt').first().json.question;
let ai = {};
try { ai = JSON.parse($input.first().json.candidates?.[0]?.content?.parts?.[0]?.text || '{}'); } catch (e) { ai = {}; }
let sql = String(ai.sql || '').trim();
const reject = (reason) => [{ json: { ok: false, question: q, sql, reason, explanation: ai.explanation || '' } }];
if (!sql) return reject(ai.explanation || 'The model could not write a query for this question.');
const stripped = sql.replace(/--[^\n]*/g, ' ').replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/'(?:[^']|'')*'/g, "''").trim().replace(/;\s*$/, '');
if (stripped.includes(';')) return reject('Multiple statements are not allowed.');
if (!/^(select|with)\b/i.test(stripped)) return reject('Only SELECT queries are allowed.');
const banned = /\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|vacuum|analyze|call|do|execute|prepare|lock|comment|refresh|reindex|cluster|set|reset|listen|notify|pg_sleep|pg_terminate_backend|pg_cancel_backend|dblink|lo_import|lo_export)\b/i;
const hit = stripped.match(banned);
if (hit) return reject(`Keyword "${hit[1]}" is not allowed in a read-only query.`);
sql = sql.trim().replace(/;\s*$/, '');
const lim = stripped.match(/\blimit\s+(\d+)\s*$/i);
if (!lim) sql += ' LIMIT 100'; else if (Number(lim[1]) > 100) sql = sql.replace(/\blimit\s+\d+\s*$/i, 'LIMIT 100');
return [{ json: { ok: true, question: q, sql, explanation: ai.explanation || '' } }];"""

ANSWER_PROMPT_JS = r"""// Ask Gemini to explain the rows in plain language (first 50 rows only).
const g = $('SQL Guard').first().json;
const rows = $input.all().map(i => i.json).filter(r => Object.keys(r).length);
const prompt = `Question: ${g.question}
SQL used: ${g.sql}
Result (${rows.length} rows, first 50 shown):
${JSON.stringify(rows.slice(0, 50))}

Answer the question in 2-4 sentences using only these rows. If the result is empty, say so plainly.`;
return [{ json: { rows: rows.slice(0, 100), rowCount: rows.length, requestBody: { contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature: 0.2 } } } }];"""

FINAL_JS = r"""const g = $('SQL Guard').first().json;
const r = $('Build Answer Prompt').first().json;
const answer = $input.first().json.candidates?.[0]?.content?.parts?.[0]?.text || '';
return [{ json: { ok: true, question: g.question, sql: g.sql, rowCount: r.rowCount, rows: r.rows, answer } }];"""

GEMINI = {"method": "POST", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
          "authentication": "predefinedCredentialType", "nodeCredentialType": "googlePalmApi",
          "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.requestBody) }}", "options": {"timeout": 60000}}
GCRED = {"googlePalmApi": {"id": "", "name": "Gemini API"}}
PCRED = {"postgres": {"id": "", "name": "Postgres (read-only user)"}}


def node(name, typ, ver, pos, params, **extra):
    n = {"parameters": params, "name": name, "type": typ, "typeVersion": ver, "position": pos}
    n.update(extra)
    return n


nodes = [
    node("Manual Test", "n8n-nodes-base.manualTrigger", 1, [-1100, -100], {}),
    node("Webhook (POST /ask-db)", "n8n-nodes-base.webhook", 2, [-1100, 100],
         {"httpMethod": "POST", "path": "ask-db", "responseMode": "responseNode", "options": {}}, webhookId="ask-db"),
    node("Question", "n8n-nodes-base.code", 2, [-880, 0], {"jsCode": r"""const b = $input.first().json.body || {};
return [{ json: { question: String(b.question || 'Which 5 customers spent the most in the last 90 days?').slice(0, 500) } }];"""}),
    node("Get Schema", "n8n-nodes-base.postgres", 2.5, [-660, 0], {"operation": "executeQuery", "query": SCHEMA_SQL, "options": {}},
         credentials=PCRED, alwaysOutputData=True),
    node("Build SQL Prompt", "n8n-nodes-base.code", 2, [-440, 0], {"jsCode": PROMPT_JS}),
    node("Gemini: Write SQL", "n8n-nodes-base.httpRequest", 4.2, [-220, 0], GEMINI, credentials=GCRED, retryOnFail=True, maxTries=3, waitBetweenTries=5000),
    node("SQL Guard", "n8n-nodes-base.code", 2, [0, 0], {"jsCode": GUARD_JS}),
    node("Safe?", "n8n-nodes-base.if", 2.2, [220, 0], {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
        "conditions": [{"leftValue": "={{ $json.ok }}", "rightValue": True, "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}}),
    node("Run Query", "n8n-nodes-base.postgres", 2.5, [440, -100], {"operation": "executeQuery", "query": "={{ $json.sql }}", "options": {}},
         credentials=PCRED, alwaysOutputData=True),
    node("Build Answer Prompt", "n8n-nodes-base.code", 2, [660, -100], {"jsCode": ANSWER_PROMPT_JS}),
    node("Gemini: Explain", "n8n-nodes-base.httpRequest", 4.2, [880, -100], GEMINI, credentials=GCRED, retryOnFail=True, maxTries=3, waitBetweenTries=5000),
    node("Final Answer", "n8n-nodes-base.code", 2, [1100, -100], {"jsCode": FINAL_JS}),
    node("Respond: Answer", "n8n-nodes-base.respondToWebhook", 1.1, [1320, -100], {"respondWith": "json", "responseBody": "={{ JSON.stringify($json) }}", "options": {}}),
    node("Respond: Rejected", "n8n-nodes-base.respondToWebhook", 1.1, [440, 120],
         {"respondWith": "json", "responseBody": "={{ JSON.stringify({ ok: false, question: $json.question, reason: $json.reason, sql: $json.sql }) }}",
          "options": {"responseCode": 422}}),
]
C = lambda *t: {"main": [[{"node": x, "type": "main", "index": 0} for x in t]]}
conns = {"Manual Test": C("Question"), "Webhook (POST /ask-db)": C("Question"), "Question": C("Get Schema"), "Get Schema": C("Build SQL Prompt"),
         "Build SQL Prompt": C("Gemini: Write SQL"), "Gemini: Write SQL": C("SQL Guard"), "SQL Guard": C("Safe?"),
         "Safe?": {"main": [[{"node": "Run Query", "type": "main", "index": 0}], [{"node": "Respond: Rejected", "type": "main", "index": 0}]]},
         "Run Query": C("Build Answer Prompt"), "Build Answer Prompt": C("Gemini: Explain"), "Gemini: Explain": C("Final Answer"),
         "Final Answer": C("Respond: Answer")}
workflow = {"name": "Postgres SQL Agent", "nodes": nodes, "connections": conns, "settings": {"executionOrder": "v1"}}

if __name__ == "__main__":
    out = HERE / "Postgres_SQL_Agent.json"
    out.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}")
