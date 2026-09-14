"""Recovery and failure compensation for the competitor monitor."""
import copy
import threading

from backend import pinchuang
from backend.monitor_journal import MonitorJournal
from backend.sync_errors import error_details


class CompetitorRecovery:
    resume_after_restart = True
    require_capture_receipt = True

    def _pause_checkpoint(self, run_id):
        if self.stop_event.is_set():
            raise pinchuang._RunStopping()
        super()._pause_checkpoint(run_id)
        if self.stop_event.is_set():
            raise pinchuang._RunStopping()

    def __init__(self, config_path, state_path, **kwargs):
        from pathlib import Path
        path = Path(state_path)
        self.journal = MonitorJournal(path.with_name(path.stem + "_journal.sqlite3"))
        super().__init__(config_path, state_path, **kwargs)
        # Import old, possibly truncated failure lists without claiming missing
        # historical details can be recovered. New records live in SQLite.
        with self.lock:
            for run in [self.state.get("current_run"), *self.state.get("history", [])]:
                if not run or not run.get("run_id"):
                    continue
                saved = {r["author_id"]: r for r in self.journal.results(run["run_id"])}
                run["creators"] = [saved.get(r["author_id"]) or self.journal.save_result(run["run_id"], r)
                                   for r in run.get("creators", [])]
            self._persist_state_locked()

    def _find_run(self, run_id):
        for run in [self.state.get("current_run"), *self.state.get("history", [])]:
            if run and run.get("run_id") == run_id:
                return copy.deepcopy(run)
        raise ValueError("未找到该同步任务")

    def _run_pipeline(self, run_id):
        with self.lock:
            run = self.state.get("current_run") or {}
            if run.get("run_id") != run_id:
                return
            # A crash after durable result commit but before JSON commit must
            # not replay that author's work or lose its result.
            results = {r["author_id"]: r for r in run.get("creators", [])}
            results.update({r["author_id"]: r for r in self.journal.results(run_id)})
            run["creators"] = list(results.values())
            self._persist_state_locked()
        return super()._run_pipeline(run_id)

    def _creator_result(self, run_id, result):
        summary = self.journal.save_result(run_id, result)
        return super()._creator_result(run_id, summary)

    def _creator_failure_result(self, author, exc, started_at):
        progress = getattr(self, "_current_creator_progress", {})
        stage = progress.get("stage", "capture")
        details = error_details(exc, stage)
        failures = list(progress.get("failures", [])) + [details]
        return {"author_id": author["username"], "author_name": author.get("nickname") or author["username"],
                "status": "failed", "message": details["error"],
                "started_at": progress.get("started_at", started_at), "finished_at": pinchuang.format_beijing(self.now()),
                **{k: int(progress.get(k, 0)) for k in ("refreshed_videos", "existing_videos", "new_videos", "uploaded_videos", "database_written")},
                "database_outcome": "unknown" if stage == "database_write" else "not_written",
                "failed_items": len(failures), "failures": failures}

    def resume_saved_run(self, run_id):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("已有同步任务正在运行")
            run = self._find_run(run_id)
            if run.get("status") not in {"interrupted", "paused", "pausing", "queued", "running"}:
                raise ValueError("该任务已经结束，请使用重试失败项")
            current = self.state.get("current_run") or {}
            if current.get("run_id") != run_id:
                if current.get("status") in pinchuang._ACTIVE_RUN_STATUSES:
                    raise RuntimeError("请先继续当前未完成任务")
                self._archive_current_locked()
            self.state["history"] = [r for r in self.state.get("history", []) if r.get("run_id") != run_id]
            run.update(status="queued", phase="recovering", message="正在从已保存的进度继续", finished_at="")
            self.state["current_run"] = run
            self.stop_event.clear()
            self.resume_event.set()
            self._persist_state_locked()
            self.worker = threading.Thread(target=self._run_pipeline, args=(run_id,), daemon=True,
                                           name=f"{self.thread_prefix}-resume-{run_id[:8]}")
            self.worker.start()
            return copy.deepcopy(run)

    def resume_run(self):
        with self.lock:
            if not (self.worker and self.worker.is_alive()):
                return self.resume_saved_run((self.state.get("current_run") or {}).get("run_id"))
        return super().resume_run()

    def retry_failed_run(self, run_id):
        with self.lock:
            source = self._find_run(run_id)
            plan, selected = [], {}
            authors = {a["username"]: a for a in source.get("creator_plan", [])}
            for result in source.get("creators", []):
                if not result.get("failed_items"):
                    continue
                author_id = result["author_id"]
                plan.append(authors.get(author_id) or {"username": author_id, "nickname": result.get("author_name", author_id)})
                failures = self.journal.failures(run_id, author_id, limit=1000000)["items"]
                # Legacy truncation: refresh the author and compare the master
                # table rather than falsely claiming the retained first 100 are all.
                if result.get("failures_truncated"):
                    selected[author_id] = {"missing_only": True}
                elif any(not f.get("video_id") for f in failures):
                    selected[author_id] = {"all": True}
                    if all(f.get("stage") in {"database_read", "database_write"}
                           for f in failures if not f.get("video_id")):
                        selected[author_id]["preserve_observations"] = True
                else:
                    selected[author_id] = {"ids": [f["video_id"] for f in failures]}
            if not plan:
                raise ValueError("该任务没有可重试的失败项")
            return self.start_run(trigger="retry", run_options={"creator_plan": plan, "retry_selection": selected,
                                  "retry_source_run_id": run_id, "sync_batch_id": source["sync_batch_id"]})

    def failure_page(self, run_id, author_id=None, offset=0, limit=50):
        with self.lock:
            run = self._find_run(run_id)
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分页参数无效")
        page = self.journal.failures(run_id, author_id, offset=offset, limit=limit)
        results = [r for r in run.get("creators", []) if not author_id or r["author_id"] == author_id]
        page["reported_total"] = sum(int(r.get("failed_items", 0)) for r in results)
        page["legacy_details_missing"] = max(0, page["reported_total"] - page["total"])
        return page
