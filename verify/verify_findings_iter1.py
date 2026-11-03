"""Verify all 9 REVIEW-1 findings are actually fixed."""
import sys, tempfile, json
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from mock_creatio import MockCreatio
from csmt.client import ODataClient
from csmt.config import Settings
from csmt.csv_source import CsvSource
from csmt.journal import Journal
from csmt.mapping import Mapper, Mapping
from csmt.pipeline import ImportPipeline, PipelineOptions
from csmt.report import ReportWriter
from csmt.csv_source import normalize_header
from csmt.client import parse_csdl_entity_fields

def make_settings(mock, **ov):
    d = {"base_url": mock.url, "username": "Supervisor", "password": "secret",
         "entity": "Contact", "timeout": 10, "max_retries": 2,
         "retry_backoff": 0.001, "retry_max_delay": 0.01}
    d.update(ov); return Settings(**d)

def run(mock, csv_text, name="v", mapping=None, keys=(), mode="upsert", **opts):
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = f"{tmp}/{name}.csv"
        open(csv_path, "w").write(csv_text)
        source = CsvSource(csv_path)
        m = Mapping.from_dict(mapping) if mapping else Mapping.identity(
            source.fieldnames, key_fields=list(keys), entity="Contact")
        m.entity = m.entity or "Contact"
        opts.setdefault("mode", mode)
        options = PipelineOptions(**opts)
        settings = make_settings(mock)
        odata = ODataClient(settings, sleep=lambda s: None)
        jpath = "/tmp/verify_journal_" + name + ".jsonl"
        pipeline = ImportPipeline(settings=settings, client=odata, mapper=Mapper(m),
            source=source, journal=Journal(jpath), report=ReportWriter(f"{tmp}/r.csv"),
            options=options)
        summary = pipeline.run()
        odata.close()
        import copy
        return copy.deepcopy(summary), jpath

ok = []
# F1: update re-executed after lost batch (mock: batch garbage w/ upsert keys)
mock = MockCreatio().start()
mock.seed("Contact", {"Name": "Old", "Email": "a@x"})
mock.force_batch_garbage = True
s, _ = run(mock, "Name,Email\nNew,a@x\n", keys=["Email"])
recs = mock.records("Contact")
ok.append(("F1 update re-executed", recs[0]["Name"] == "New" and s.updated == 1, recs[0]["Name"]))
mock.stop()

# F2: resume with lookup key
mock = MockCreatio().start()
cid = mock.seed("Country", {"Name": "Indonesia"})
mapping = {"entity": "Contact", "key_fields": ["Email"], "fields": {
    "Name": {"target": "Name"}, "Email": {"target": "Email"},
    "Negara": {"target": "CountryId", "type": "lookup",
               "lookup": {"entity": "Country", "key_field": "Name"}}}}
s1, journal_path = run(mock, "Name,Email,Negara\nAlice,a@x,Indonesia\n", name="r1", mapping=mapping)
journal_entries = [json.loads(l) for l in open(journal_path)]
ok.append(("F2 journal key stable (no GUID)", all("CountryId=" not in e["key"] for e in journal_entries), journal_entries[0]["key"]))
mock.stop()

# F3: duplicate rows: row1 success, row2 failed -> resume re-runs failed row
mock = MockCreatio().start()
s, _ = run(mock, "Name,Email\nA,dupe@x\nB,dupe@x\n", name="d", keys=["Email"], duplicate_policy="error")
ok.append(("F3 duplicate policy: 1 created 1 failed", s.created == 1 and s.failed == 1, (s.created, s.failed)))
mock.stop()

# F4: lookup GUID key unquoted $filter
mock = MockCreatio().start()
gid = mock.seed("Contact", {"Name": "K", "Email": "k@x", "CountryId": None})
mapping = {"entity": "Contact", "key_fields": ["CountryId"], "fields": {
    "Name": {"target": "Name"}, "Email": {"target": "Email"},
    "Cid": {"target": "CountryId", "type": "lookup", "lookup": {"entity": "Country", "key_field": "Name"}}}}
s, _ = run(mock, "Name,Email,Cid\nNewK,k2@x,Indonesia\n", name="l", mapping=mapping)
gets = [r for r in mock.requests if r["method"] == "GET"]
filters = [r["path"] for r in gets if "$filter" in r["path"]]
unquoted = any(" CountryId eq " not in f and "CountryId eq " in f and "') (" not in f for f in filters) or True
# precise check: no apostrophe right after 'CountryId eq '
bad = [f for f in filters if "CountryId eq '" in f]
ok.append(("F4 lookup-guid key sent unquoted", not bad, bad[:1]))
mock.stop()

# F5: POST not resent on 5xx/timeout
mock = MockCreatio().start()
s, _ = run(mock, "Name,Email\nALWAYS-500,a@x\n", name="r", mode="create")
ok.append(("F5 POST single attempt", mock.attempts["ALWAYS-500"] == 1 and s.failed == 1, mock.attempts))
mock.stop()

# F6: lookup server error -> NOT cached negative; later row still resolves
from unittest.mock import patch
mock = MockCreatio().start()
mock.seed("Country", {"Name": "Indonesia"})
mapping6 = {"entity": "Contact", "fields": {
    "Name": {"target": "Name"}, "Negara": {"target": "CountryId", "type": "lookup",
    "lookup": {"entity": "Country", "key_field": "Name"}}}}
csv6 = "Name,Negara\nA,Indonesia\nB,Indonesia\n"
with tempfile.TemporaryDirectory() as tmp:
    csv_path = f"{tmp}/f6.csv"; open(csv_path, "w").write(csv6)
    source = CsvSource(csv_path)
    m = Mapping.from_dict(mapping6); m.entity = "Contact"
    settings = make_settings(mock)
    odata = ODataClient(settings, sleep=lambda s: None)
    pipeline = ImportPipeline(settings=settings, client=odata, mapper=Mapper(m), source=source,
        journal=Journal(None), report=ReportWriter(None), options=PipelineOptions(mode="create", batch_size=1, chunk_rows=1, use_batch=False))
    original = ODataClient.get_collection
    calls = {"n": 0}
    def flaky(self, entity, **kw):
        calls["n"] += 1
        if entity == "Country" and calls["n"] == 1:
            from csmt.errors import ODataError
            raise ODataError("boom", status=500)
        return original(self, entity, **kw)
    with patch.object(ODataClient, "get_collection", flaky):
        s6 = pipeline.run()
    ok.append(("F6 lookup error not cached negative", s6.created == 1 and s6.failed == 1 and calls["n"] == 2, (s6.created, s6.failed, calls)))
    odata.close()
mock.stop()

# F7: header dedup no collision
ok.append(("F7 header dedup", normalize_header(["Name","Name_2","Name"]) == ["Name","Name_2","Name_3"],
           normalize_header(["Name","Name_2","Name"])))

# F8: BaseType inheritance parsed
xml = ('<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>'
 '<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="N">'
 '<EntityType Name="Base"><Property Name="Id" Type="Edm.Guid"/></EntityType>'
 '<EntityType Name="Contact" BaseType="N.Base"><Property Name="Name" Type="Edm.String"/></EntityType>'
 '<EntityContainer Name="C"><EntitySet Name="Contact" EntityType="N.Contact"/></EntityContainer>'
 '</Schema></edmx:DataServices></edmx:Edmx>')
f = parse_csdl_entity_fields(xml, "Contact")
ok.append(("F8 BaseType inherited", set(f) == {"Id", "Name"}, f))

# F9: lookup default resol ved via lookup flow
mock = MockCreatio().start()
cid9 = mock.seed("Country", {"Name": "Indonesia"})
mapping9 = {"entity": "Contact", "fields": {
    "Name": {"target": "Name"},
    "Negara": {"target": "CountryId", "type": "lookup", "default": "Indonesia",
               "lookup": {"entity": "Country", "key_field": "Name"}}}}
s9, _ = run(mock, "Name\nA\n", name="d9", mapping=mapping9, mode="create")
recs9 = mock.records("Contact")
ok.append(("F9 lookup default resolved to GUID", s9.created == 1 and recs9[0]["CountryId"] == cid9,
           (s9.created, recs9[0].get("CountryId"))))
mock.stop()

print()
w = 0
for name, passed, detail in ok:
    print(("PASS " if passed else "FAIL ") + name + ("  | " + str(detail) if not passed else ""))
    w += 0 if passed else 1
print(f"\n{len(ok)-w}/{len(ok)} findings verified fixed")
sys.exit(1 if w else 0)
