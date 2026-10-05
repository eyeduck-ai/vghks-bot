"""Exercise the new projections inside source and frozen synthetic self-tests."""
from .library_search import ensure


def check_efficiency(workspace, task_id):
    db, library = workspace.review.db, workspace.store.library
    ensure(library, workspace.settings)
    with library.read_snapshot() as connection:
        scope = connection.execute("SELECT count(DISTINCT record_id) FROM bot_task_record_refs WHERE task_id=?", (task_id,)).fetchone()[0]
        before = connection.execute("SELECT payload FROM bot_documents WHERE kind='task' AND id=?", (task_id,)).fetchone()[0]
    page = workspace.library_search({"task_id": task_id, "limit": 1})
    assert page["total"] == scope and page["page_total"] == scope
    assert len(page["records"]) == min(scope, 1)
    summary = workspace.review.task(task_id, summary=True)
    assert "members" not in summary and "items" not in summary
    assert summary["total"] == 2 and summary["done"] == 2
    db.update_task(task_id, {"message": "synthetic summary update"})
    with library.read_snapshot() as connection:
        assert connection.execute("SELECT payload FROM bot_documents WHERE kind='task' AND id=?", (task_id,)).fetchone()[0] == before
    assert workspace.review.task(task_id, summary=True)["message"] == "synthetic summary update"
    assert workspace.task_manager.foreground_limit == 4 and workspace.task_manager.pending_limit == 64
    set_id = db.get("task", task_id)["set_id"]
    workspace.review.save_tool_state({"module": "review", "set_id": set_id})
    workspace.review.save_tool_state({"module": "retina", "set_id": set_id})
    workspace.review.save_tool_state({"module": "review", "set_id": ""})
    assert workspace.review.tool_state()["modules"] == {
        "review": {"set_id": ""}, "retina": {"set_id": set_id}}
    inventory = workspace.library_data.read({"limit": 40})
    assert inventory["limit"] == 40 and len(inventory["patients"]) <= 40
