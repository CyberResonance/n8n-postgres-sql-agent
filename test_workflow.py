"""Structure checks + the read-only SQL guard and prompt logic executed in Node.js."""
import json, shutil, subprocess, unittest
from pathlib import Path
import generate_workflow as gw

HERE = Path(__file__).parent
WF = gw.workflow
NODE = shutil.which("node")


def run_js(code, items, refs=None):
    harness = f"""
const items = {json.dumps(items)}; const refs = {json.dumps(refs or {})};
const $input = {{ all: () => items, first: () => items[0] }};
const $ = (n) => ({{ all: () => refs[n] || [], first: () => (refs[n] || [])[0] }});
(async function () {{ {code} }})().then(r => console.log(JSON.stringify(r)));
"""
    res = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=30)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


class TestStructure(unittest.TestCase):
    def test_file_matches_generator(self):
        self.assertEqual(json.loads((HERE / "Postgres_SQL_Agent.json").read_text(encoding="utf-8")), json.loads(json.dumps(WF)))

    def test_connections(self):
        names = {n["name"] for n in WF["nodes"]}
        for src, out in WF["connections"].items():
            self.assertIn(src, names)
            for br in out["main"]:
                for c in br:
                    self.assertIn(c["node"], names)

    def test_no_secrets(self):
        self.assertNotRegex((HERE / "Postgres_SQL_Agent.json").read_text(encoding="utf-8"), r"AIza[0-9A-Za-z_\-]{30,}|\b\d{17,20}\b|\$env\.|password")


@unittest.skipUnless(NODE, "Node.js not installed")
class TestGuard(unittest.TestCase):
    REFS = {"Build SQL Prompt": [{"json": {"question": "q"}}]}

    def guard(self, sql):
        g = [{"json": {"candidates": [{"content": {"parts": [{"text": json.dumps({"sql": sql, "explanation": "e"})}]}}]}}]
        return run_js(gw.GUARD_JS, g, self.REFS)[0]["json"]

    def test_allows_select_and_adds_limit(self):
        r = self.guard("SELECT name FROM customers")
        self.assertTrue(r["ok"])
        self.assertTrue(r["sql"].endswith("LIMIT 100"))

    def test_caps_big_limit(self):
        self.assertTrue(self.guard("select * from t limit 5000;")["sql"].endswith("LIMIT 100"))

    def test_allows_cte(self):
        self.assertTrue(self.guard("WITH x AS (SELECT 1 AS a) SELECT a FROM x LIMIT 10")["ok"])

    def test_keywords_inside_strings_are_fine(self):
        self.assertTrue(self.guard("SELECT * FROM logs WHERE action = 'delete' LIMIT 5")["ok"])

    def test_blocks_writes_and_tricks(self):
        for bad in ["DELETE FROM users", "UPDATE t SET a=1", "DROP TABLE t", "SELECT 1; DROP TABLE t",
                    "select pg_sleep(10)", "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
                    "TRUNCATE t", "COPY t TO '/tmp/x'", "select * from t /* ok */ ; insert into t values (1)"]:
            self.assertFalse(self.guard(bad)["ok"], bad)

    def test_empty_sql_is_rejected_with_reason(self):
        g = [{"json": {"candidates": [{"content": {"parts": [{"text": json.dumps({"sql": "", "explanation": "No such table."})}]}}]}}]
        r = run_js(gw.GUARD_JS, g, self.REFS)[0]["json"]
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "No such table.")

    def test_prompt_compacts_schema(self):
        rows = [{"json": {"table_name": "orders", "column_name": "id", "data_type": "integer"}},
                {"json": {"table_name": "orders", "column_name": "total", "data_type": "numeric"}}]
        r = run_js(gw.PROMPT_JS, rows, {"Question": [{"json": {"question": "Top orders?"}}]})[0]["json"]
        self.assertIn("orders(id integer, total numeric)", r["schema"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
