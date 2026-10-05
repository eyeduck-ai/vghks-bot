"""Clinical readiness projections; report and numeric bodies stay on demand."""
from __future__ import annotations

from .migrations import migrate, statements


def initialize(db):
    def apply(db):
        statements(db,
            "CREATE TABLE analysis_step_metadata (mrn TEXT NOT NULL,key TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,saved_at TEXT NOT NULL,account TEXT NOT NULL,assessment TEXT NOT NULL,PRIMARY KEY(mrn,key))",
            "CREATE INDEX analysis_step_metadata_kind ON analysis_step_metadata(mrn,kind)",
            "CREATE TABLE analysis_run_metadata (id TEXT PRIMARY KEY,cohort_id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE INDEX analysis_run_metadata_cohort ON analysis_run_metadata(cohort_id,updated_at DESC)",
            "CREATE INDEX analysis_runs_cohort ON analysis_runs(json_extract(payload,'$.cohort_id'),updated_at DESC)")
        projection = ("CASE WHEN {p}.kind IN ('numeric','numeric_structured','asset') THEN '{{}}' "
                      "WHEN {p}.kind='order_report' THEN json_object('status',json_extract({p}.payload,'$.status'),"
                      "'assets',json(coalesce(json_extract({p}.payload,'$.assets'),'[]'))) "
                      "ELSE {p}.payload END")
        header = ("json_object('id',{p}.id,'cohort_id',json_extract({p}.payload,'$.cohort_id'),"
                  "'status',json_extract({p}.payload,'$.status'),'options',json(json_object("
                  "'modules',json(coalesce(json_extract({p}.payload,'$.options.modules'),'[]')),"
                  "'mrn',json_extract({p}.payload,'$.options.mrn'))),"
                  "'name',json_extract({p}.payload,'$.name'),'message',json_extract({p}.payload,'$.message'),"
                  "'created_at',json_extract({p}.payload,'$.created_at'),'finished_at',json_extract({p}.payload,'$.finished_at'),"
                  "'counts',json(coalesce(json_extract({p}.payload,'$.counts'),'{{}}')),"
                  "'member_mrns',json((SELECT json_group_array(json_extract(value,'$.mrn')) FROM json_each({p}.payload,'$.members'))))")
        for operation in ("INSERT", "UPDATE"):
            db.execute(f"CREATE TRIGGER analysis_step_meta_{operation.lower()} AFTER {operation} ON analysis_steps BEGIN "
                       "INSERT OR REPLACE INTO analysis_step_metadata SELECT new.mrn,new.key,new.kind," +
                       projection.format(p="new") + ",new.saved_at,new.account,new.assessment; END")
            db.execute(f"CREATE TRIGGER analysis_run_meta_{operation.lower()} AFTER {operation} ON analysis_runs BEGIN "
                       "INSERT OR REPLACE INTO analysis_run_metadata SELECT new.id,coalesce(json_extract(new.payload,'$.cohort_id'),'')," +
                       header.format(p="new") + ",new.updated_at; END")
        statements(db,
            "CREATE TRIGGER analysis_step_meta_delete AFTER DELETE ON analysis_steps BEGIN DELETE FROM analysis_step_metadata WHERE mrn=old.mrn AND key=old.key; END",
            "CREATE TRIGGER analysis_run_meta_delete AFTER DELETE ON analysis_runs BEGIN DELETE FROM analysis_run_metadata WHERE id=old.id; END")
        db.execute("INSERT INTO analysis_step_metadata SELECT s.mrn,s.key,s.kind," + projection.format(p="s") +
                   ",s.saved_at,s.account,s.assessment FROM analysis_steps s")
        db.execute("INSERT INTO analysis_run_metadata SELECT s.id,coalesce(json_extract(s.payload,'$.cohort_id'),'')," +
                   header.format(p="s") + ",s.updated_at FROM analysis_runs s")
    migrate(db, "analysis_metadata_v1", apply)
