"""Verify all 5 REVIEW-2 findings are fixed."""
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

def make_settings(mock, **ov):
    d = {"base_url": mock.url, "username": "Supervisor", "password": "secret",
         "entity": "Contact", "timeout": 10, "max_retries": 2,
         "retry_backoff": 0.001, "retry_max_delay": 0.01}
    d.update(ov); return Settings(**d)

JDIR = "/tmp/v2journal"

def run(mock, csv_text, name="v", mapping=None, keys=(), mode="upsert", resume=False, **opts):
    import os, shutil
    os.makedirs(JDIR, exist_ok=True)
    jpath = f"{JDIR}/{name}.jsonl"
    rpath = f"{JDIR}/{name}.report.csv"
    if not resume:
        for f in (jpath, rpath):
            if os.path.exists(f):
                os.remove(f)
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = f"{tmp}/{name}.csv"
        open(csv_path, "w").write(csv_text)
        source = CsvSource(csv_path)
        m = Mapping.from_dict(mapping) if mapping else Mapping.identity(
            source.fieldnames, key_fields=list(keys), entity="Contact")
        m.entity = m.entity or "Contact"
        options = PipelineOptions(mode=mode, **opts)
        settings = make_settings(mock)
        odata = ODataClient(settings, sleep=lambda s: None)
        pipeline = ImportPipeline(settings=settings, client=odata, mapper=Mapper(m),
            source=source, journal=Journal(jpath if resume else None), report=ReportWriter(rpath, ),
            options=options)
        try:
            summary = pipeline.run()
        finally:
            odata.close()
        return summary, jpath, rpath
ok = []

# R2#1: lookup key matched via resolved GUID (upsert updates, not duplicate)
mock = MockCreatio().start()
cid = mock.seed("Country", {"Name": "Indonesia"})
mock.seed("Contact", {"Name": "Old", "Email": "a@x", "CountryId": cid})
mapping1 = {"entity": "Contact", "key_fields": ["Email", "CountryId"], "fields": {
    "Name": {"target": "Name"}, "Email": {"target": "Email"},
    "Negara": {"target": "CountryId", "type": "lookup",
               "lookup": {"entity": "Country", "key_field": "Name"}}}}
s, _, _ = run(mock, "Name,Email,Negara\nNew,a@x,Indonesia\n", name="r2f1", mapping=mapping1)
recs = mock.records("Contact")
ok.append(("R2#1 lookup-key upsert matches via GUID", len(recs) == 1 and recs[0]["Name"] == "New" and s.updated == 1,
           (len(recs), s.updated, s.created, recs[0].get("Name"))))
mock.stop()

# R2#2: reconciliation pops cache (single POST lost response → reconcile)
mock = MockCreatio().start()
mapping2 = {"entity": "Contact", "key_fields": ["Email"], "fields": {
    "Name": {"target": "Name"}, "Email": {"target": "Email"}}}
# force single-op batch: batch_size=1 → batch of 1? use_batch needs >1 op → use no_batch
class LostClient(ODataClient):
    def batch(self, operations, **kw):
        # simulate: POST applied on server, response lost
        for op in operations:
            self.create(self.settings.entity, op["body"])
        from csmt.errors import BatchError
        raise BatchError("simulated lost batch response", payload="garbage")
with tempfile.TemporaryDirectory() as tmp:
    csv_path = f"{tmp}/x.csv"; open(csv_path, "w").write("Name,Email\nAlice,a@x\n")
    d = make_settings(mock)
    odata = LostClient(d, sleep=lambda s: None)
    source = CsvSource(csv_path)
    m = Mapping.from_dict(mapping2); m.entity = "Contact"
    pipeline = ImportPipeline(settings=d, client=odata, mapper=Mapper(m), source=source,
        journal=Journal(None), report=ReportWriter(None),
        options=PipelineOptions(mode="upsert", use_batch=True, batch_size=1))
    s2 = pipeline.run()
    # recovery re-executes update/create one-by-one; Alice must exist exactly once
    alices = mock.find("Contact", Email="a@x")
    ok.append(("R2#2 recovery recreates after lost batch", len(alices) == 1 and s2.created == 1,
               (len(alices), s2.as_dict())))
    odata.close()
mock.stop()

# R2#3: CSV shifted between runs → no silent skip of new content
mock = MockCreatio().start()
# run 1: rows A,B both succeed
s1, jpath, _ = run(mock, "Name,Email\nA,a@x\nB,b@x\n", name="r2f3", mode="create", keys=["Email"], resume=False)
assert s1.created == 2
# CSV shifts: new row inserted at top; row on line 3 now NEW content "C" must NOT be skipped
s2, _, _ = run(mock, "Name,Email\nX,x@x\nA,a@x\nC,c@x\n", name="r2f3", mode="create", keys=["Email"], resume=True)
names = {r["Name"] for r in mock.records("Contact")}
# "C" content on line 3 conflicts with journal line 3 (key B) -> must be re-processed, not skipped
ok.append(("R2#3 shifted CSV re-processes mismatched line", "C" in names,
           (names, s2.as_dict())))
mock.stop()

# R2#4: resumed rows register dedupe key → later duplicate row still caught
mock = MockCreatio().start()
s1, jpath, _ = run(mock, "Name,Email\nD,dupe@x\n", name="r2f4", mode="create", keys=["Email"], resume=False)
assert s1.created == 1
csv4 = "Name,Email\nD,dupe@x\nE,dupe@x\n"
s2, _, _ = run(mock, csv4, name="r2f4", mode="create", keys=["Email"], resume=True)
ok.append(("R2#4 resume registers duplicate detection",
           s2.failed == 1 and s2.created == 0 and len(mock.records("Contact")) == 1,
           (s2.as_dict(), len(mock.records("Contact")))))
mock.stop()

# R2#5: POST retried on 429, NOT on 500
class RateLimitMock(MockCreatio):
    def apply_create(self, entity, body):
        import json as _json
        try:
            payload = _json.loads(body or b"{}")
        except ValueError:
            payload = {}
        name = str(payload.get("Name") or "")
        if name == "TRIGGER-429" and self.attempts.get(name, 0) < 1:
            self.attempts[name] = self.attempts.get(name, 0) + 1
            return 429, {"error": {"message": "rate limited"}}, {}
        return super().apply_create(entity, body)
mock = RateLimitMock().start()
from csmt.errors import ODataError
client = ODataClient(make_settings(mock), sleep=lambda s: None)
try:
    rec = client.create("Contact", {"Name": "TRIGGER-429"})
    ok.append(("R2#5a POST retried on 429", bool(rec.get("Id")), rec))
    n_before = mock.attempts["TRIGGER-429"]
    try:
        client.create("Contact", {"Name": "ALWAYS-500"})
        ok_5xx = False
    except ODataError:
        ok_5xx = True
    ok.append(("R2#5b POST not retried on 500", ok_5xx and mock.attempts["ALWAYS-500"] == 1,
               mock.attempts))
finally:
    client.close()
mock.stop()

print()
w = 0
for name, passed, detail in ok:
    print(("PASS " if passed else "FAIL ") + name + ("" if passed else "  | " + str(detail)))
    w += 0 if passed else 1
print(f"\n{len(ok)-w}/{len(ok)} iter-2 findings verified fixed")
sys.exit(1 if w else 0)
