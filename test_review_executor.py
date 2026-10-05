"""Unit tests use fake provider objects. Live canary is separately opt-in."""
import copy
import json
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from hyemi_review import (CapabilityGate, ReviewError, ReviewExecutor, build_messages,
                          failure, make_job, parse_result, review_with_client, validate_profile)

REQUEST = {"kind": "review", "to": "hyeji", "contents": "1+1=3", "criteria": "산술 정확성", "revision": 1}


def good(outcome="changes_required"):
    findings = [{"id": "F1", "location": "contents 1행: 1+1=3", "issue": "합계 오류",
                 "reason": "두 개의 1의 합은 2입니다.", "improvement": "1+1=2로 수정",
                 "resolution_criteria": "합계가 2로 표기됨"}] if outcome == "changes_required" else []
    return {"kind": "review", "outcome": outcome, "hyemi_result": {
        "summary": "수정 필요" if findings else "텍스트 검토 완료", "findings": findings,
        "resolved": [], "remaining": [], "new": ["F1"] if findings else [],
        "verified_scope": ["제출된 수식"], "limitations": [],
        "criterion_checks": [{"criterion": "산술 정확성", "evidence_scope": "submitted_text",
                              "status": "needs_changes" if findings else "satisfied",
                              "evidence": ["1+1=3" if findings else "1+1=2"]}]},
        "telegram_report": "수식 검토 결과입니다."}


class FakeClient:
    def __init__(self, content=None, calls=None):
        self.calls = []
        self.response = NS(choices=[NS(message=NS(content=content, tool_calls=calls))])
        self.chat = NS(completions=NS(create=self.create))
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class ReviewTests(unittest.TestCase):
    def test_fabricated_mutation_denied_at_real_dispatch(self):
        for name in ("write_file", "terminal", "delegate_task", "send_message", "skill_manage", "cronjob", "mcp__notion_update", "read_file", "functions.terminal"):
            with self.subTest(name=name):
                client = FakeClient(json.dumps(good()), [NS(function=NS(name=name, arguments='{"path":"/tmp/target"}'))])
                with self.assertRaisesRegex(ReviewError, "capability_denied"):
                    review_with_client(client, "exact-model", make_job(REQUEST, "test"))
                self.assertEqual(client.calls[0]["tools"], [])
                self.assertEqual(len(client.calls), 1)

    def test_empty_gate_cannot_install_handlers(self):
        gate = CapabilityGate()
        self.assertEqual(gate.schemas, [])
        with self.assertRaises(AttributeError):
            gate.handlers = {"terminal": print}
        with self.assertRaises(ReviewError):
            gate.dispatch("anything", {})

    def test_profile_and_path_denied(self):
        for name in ("hyemi", "default", "../hyerin", "/root/.hermes"):
            with self.assertRaises(ReviewError):
                validate_profile(name)
        with self.assertRaises(ReviewError):
            validate_profile("hyerin", "/root/.hermes")
        with self.assertRaises(ReviewError):
            ReviewExecutor({"review_source": "/tmp/evil"})
        with self.assertRaises(ReviewError):
            ReviewExecutor({"review_profile_paths": {"hyeji": "/tmp/profile"}})

    def test_positive_prompt_and_exact_history(self):
        history = [{"revision": 1, "result": good(), "evidence": "literal Ω 자료"}]
        original = copy.deepcopy(history)
        job = make_job(REQUEST, "test", history)
        messages = build_messages(job)
        self.assertEqual(json.loads(messages[1]["content"])["history"], original)
        self.assertEqual(history, original)
        for text in ("do not", "don't", "never", "하지 마", "금지"):
            self.assertNotIn(text, messages[0]["content"].lower())
        self.assertNotIn("SOUL", messages[0]["content"])
        self.assertEqual(len(make_job(REQUEST, "test", [{}] * 13)["history"]), 13)

    def test_typed_detailed_result(self):
        result = parse_result(json.dumps(good()), make_job(REQUEST, "test"))
        self.assertEqual(result["outcome"], "changes_required")
        self.assertIs(result["completion_signal"], False)
        self.assertEqual(result["hyemi_result"]["findings"][0]["location"], "contents 1행: 1+1=3")
        self.assertIn("직접 확인하지", result["limitations"][0])
        result = parse_result(json.dumps(good("passed_in_scope")), make_job(dict(REQUEST, contents="1+1=2"), "test"))
        self.assertIs(result["completion_signal"], True)

    def test_requested_external_and_runtime_criteria_cannot_pass(self):
        for criterion, expected in (("실제 배포 상태 확인", "runtime"), ("외부 출처 검증", "external_source"),
                                    ("Verify deployed arithmetic service", "runtime"),
                                    ("정확성 확인", "submitted_text"),
                                    ("arithmetic and confirm the service is healthy", "submitted_text")):
            with self.subTest(criterion=criterion):
                value = good("passed_in_scope")
                value["hyemi_result"]["criterion_checks"][0]["criterion"] = criterion
                client = FakeClient(json.dumps(value))
                result = review_with_client(client, "exact-model", make_job(dict(REQUEST, contents="1+1=2", criteria=criterion), "test"))
                self.assertEqual(result["outcome"], "unverified")
                self.assertIs(result["completion_signal"], False)
                detail = result["hyemi_result"]
                self.assertEqual(detail["model_outcome"], "passed_in_scope")
                self.assertEqual(detail["criterion_checks"][0]["status"], "unverified")
                self.assertEqual(detail["criterion_checks"][0]["evidence_scope"], expected)
                self.assertEqual(detail["verified_scope"], [])
                self.assertTrue(any(criterion in item for item in detail["limitations"]))

    def test_criteria_exact_coverage_and_literal_evidence(self):
        request = dict(REQUEST, contents="1+1=2", criteria="- 산술 정확성\n\n- 수식 서식")
        value = good("passed_in_scope")
        checks = value["hyemi_result"]["criterion_checks"]
        checks[0]["criterion"] = "- 산술 정확성"
        checks.append(dict(checks[0], criterion="- 수식 서식"))
        job = make_job(request, "test")
        self.assertIs(parse_result(json.dumps(value), job)["completion_signal"], True)
        variants = []
        for change in ("missing", "extra", "reworded", "forged", "empty", "punctuation"):
            bad = copy.deepcopy(value)
            items = bad["hyemi_result"]["criterion_checks"]
            if change == "missing": items.pop()
            if change == "extra": items.append(copy.deepcopy(items[0]))
            if change == "reworded": items[0]["criterion"] = "산술 정확성"
            if change == "forged": items[0]["evidence"] = ["not supplied"]
            if change == "empty": items[0]["evidence"] = []
            if change == "punctuation": items[0]["evidence"] = ["+"]
            variants.append(bad)
        for bad in variants:
            self.assertEqual(parse_result(json.dumps(bad), job)["outcome"], "unverified")
        self.assertEqual(parse_result(json.dumps(value))["outcome"], "unverified")

    def test_revision_resolution_and_invalid_ids(self):
        history = [{"request": REQUEST, "hyemi_result": good()["hyemi_result"], "known_finding_ids": ["F1"], "active_finding_ids": ["F1"]}]
        job = make_job(dict(REQUEST, contents="1+1=2", revision=2), "test", history)
        value = good("passed_in_scope")
        value["hyemi_result"]["resolved"] = ["F1"]
        self.assertIs(parse_result(json.dumps(value), job)["completion_signal"], True)
        for resolved in ([], ["F2"], ["F1", "F1"]):
            bad = copy.deepcopy(value)
            bad["hyemi_result"]["resolved"] = resolved
            self.assertEqual(parse_result(json.dumps(bad), job)["outcome"], "unverified")
        for field in ("remaining", "new"):
            bad = good()
            bad["hyemi_result"][field] = ["F1"]
            bad["hyemi_result"]["resolved"] = ["F1"]
            self.assertEqual(parse_result(json.dumps(bad), job)["outcome"], "unverified")
        missing = make_job(dict(REQUEST, contents="1+1=2", revision=2), "test")
        self.assertEqual(parse_result(json.dumps(value), missing)["outcome"], "unverified")
        broken = copy.deepcopy(job)
        broken["history"][0]["hyemi_result"]["remaining"] = ["F1"]
        self.assertEqual(parse_result(json.dumps(value), broken)["outcome"], "unverified")
        malformed = dict(job, history=[{"request": REQUEST}])
        self.assertEqual(parse_result(json.dumps(value), malformed)["outcome"], "unverified")

    def test_remaining_ids_and_historical_id_reuse(self):
        first = {"request": REQUEST, "hyemi_result": good()["hyemi_result"], "known_finding_ids": ["F1"], "active_finding_ids": ["F1"]}
        value = good()
        value["hyemi_result"].update(remaining=["F1"], new=[])
        job = make_job(dict(REQUEST, revision=2), "test", [first])
        self.assertEqual(parse_result(json.dumps(value), job)["outcome"], "changes_required")
        resolved = good("passed_in_scope")["hyemi_result"]
        resolved["resolved"] = ["F1"]
        second = {"request": dict(REQUEST, contents="1+1=2"), "hyemi_result": resolved,
                  "known_finding_ids": ["F1"], "active_finding_ids": []}
        job = make_job(dict(REQUEST, revision=3), "test", [first, second])
        self.assertEqual(parse_result(json.dumps(good()), job)["outcome"], "unverified")
        value = good()
        value["hyemi_result"]["new"] = ["F2"]
        value["hyemi_result"]["findings"][0]["id"] = "F2"
        self.assertEqual(parse_result(json.dumps(value), job)["outcome"], "changes_required")

    def test_registry_validation_and_legacy_direct_job_fallback(self):
        record = {"request": REQUEST, "hyemi_result": good()["hyemi_result"]}
        request = dict(REQUEST, contents="1+1=2", revision=2)
        value = good("passed_in_scope")
        value["hyemi_result"]["resolved"] = ["F1"]
        missing = make_job(request, "test", [record])
        self.assertEqual(parse_result(json.dumps(value), missing)["outcome"], "unverified")
        legacy = dict(missing)
        del legacy["known_finding_ids"]
        self.assertTrue(parse_result(json.dumps(value), legacy)["completion_signal"])
        for registry in (None, "F1", {}, [""], [" "], [1], ["F1", "F1"], []):
            with self.subTest(registry=registry):
                job = make_job(request, "test", [dict(record, known_finding_ids=registry)])
                self.assertEqual(parse_result(json.dumps(value), job)["outcome"], "unverified")
        # A malformed earlier annotation remains invalid even if the last is valid.
        job = make_job(request, "test", [dict(record, known_finding_ids=None),
                                          dict(record, known_finding_ids=["F1"])])
        self.assertEqual(parse_result(json.dumps(value), job)["outcome"], "unverified")
        # Strict limits also apply to direct jobs, not only serialized inputs.
        legacy["known_finding_ids"] = ["X" * 160001]
        self.assertEqual(parse_result(json.dumps(value), legacy)["outcome"], "unverified")

    def test_registry_applies_to_current_not_historical_new_ids(self):
        first = {"request": REQUEST, "hyemi_result": good()["hyemi_result"]}
        second_detail = good()["hyemi_result"]
        second_detail.update(resolved=["F1"], new=["F2"])
        second_detail["findings"][0]["id"] = "F2"
        second = {"request": REQUEST, "hyemi_result": second_detail,
                  "known_finding_ids": ["F1", "F2", "older-resolved"], "active_finding_ids": ["F2"]}
        job = make_job(dict(REQUEST, contents="1+1=2", revision=3,
                            known_finding_ids=["foreign-request-id"]), "test", [first, second])
        self.assertEqual(job["known_finding_ids"], ["F1", "F2", "older-resolved"])
        value = good("passed_in_scope")
        value["hyemi_result"]["resolved"] = ["F2"]
        self.assertTrue(parse_result(json.dumps(value), job)["completion_signal"])
        self.assertIn("이전 전체 finding ID registry", build_messages(job)[0]["content"])
        # IDs alone are classification context, never textual evidence.
        value["hyemi_result"]["criterion_checks"][0]["evidence"] = ["older-resolved"]
        self.assertEqual(parse_result(json.dumps(value), job)["outcome"], "unverified")

    def test_all_round_registry_through_store_twelve_record_window(self):
        import tempfile
        from pathlib import Path
        from hyemi_contracts import owner_id, normalize_executor_result
        from hyemi_store import WorkflowStore
        from test_workflow_store import request
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / "registry.sqlite")
            owner = owner_id("registry", "test")
            prior = None
            for revision in range(1, 16):
                fields = dict(contents="1+1=3" if revision in (1, 15) else "1+1=2",
                              criteria="산술 정확성", revision=revision)
                if prior:
                    fields.update(work_id=prior["work_id"], previous_event_id=prior["event_id"])
                current = request("review", **fields)
                store.admit(owner, current)
                history = store.review_history(owner, current["work_id"], current["event_id"])
                job = make_job(current, "registry-test", history)
                if revision == 15:
                    self.assertEqual(len(history), 12)
                    self.assertEqual(history[0]["revision"], 3)
                    self.assertIn("F1", job["known_finding_ids"])
                    reused = parse_result(json.dumps(good()), job)
                    self.assertEqual(reused["outcome"], "unverified")
                    self.assertFalse(reused["completion_signal"])
                    fresh = good()
                    fresh["hyemi_result"]["new"] = ["F2"]
                    fresh["hyemi_result"]["findings"][0]["id"] = "F2"
                    self.assertEqual(parse_result(json.dumps(fresh), job)["outcome"], "changes_required")
                    passed = good("passed_in_scope")
                    passed_job = dict(job, submission="1+1=2")
                    self.assertTrue(parse_result(json.dumps(passed), passed_job)["completion_signal"])
                    for field in ("known_finding_ids",):
                        broken_history = copy.deepcopy(history)
                        for entry in broken_history:
                            entry.pop(field, None)
                        omitted = make_job(dict(current, contents="1+1=2"), "test", broken_history)
                        self.assertEqual(parse_result(json.dumps(passed), omitted)["outcome"], "unverified")
                    break
                value = good() if revision == 1 else good("passed_in_scope")
                if revision == 2:
                    value["hyemi_result"]["resolved"] = ["F1"]
                result = parse_result(json.dumps(value), job)
                self.assertEqual(result["outcome"], value["outcome"])
                store.finish(current["event_id"], normalize_executor_result(result, "review"))
                prior = current

    def test_persistent_failure_continuity_and_failure_only_window(self):
        import tempfile
        from pathlib import Path
        from hyemi_contracts import owner_id
        from hyemi_store import WorkflowStore, StoreConflict
        from hyemi_workers import failure as worker_failure, normalize_review
        from test_workflow_store import request
        for mode in ("worker", "executor", "window", "executor_window", "scope"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "continuity.sqlite"
                store = WorkflowStore(path)
                owner = owner_id("continuity", mode)
                criterion = "외부 출처 검증" if mode == "scope" else "산술 정확성"
                first = request("review", contents="1+1=3", criteria=criterion)
                store.admit(owner, first)
                initial_value = good()
                initial_value["hyemi_result"]["criterion_checks"][0]["criterion"] = criterion
                initial = parse_result(json.dumps(initial_value), make_job(first, "test"))
                self.assertEqual(initial["outcome"], "unverified" if mode == "scope" else "changes_required")
                store.finish(first["event_id"], normalize_review(initial))
                prior = first
                failures = 12 if "window" in mode else 1
                for revision in range(2, failures + 2):
                    current = request("review", work_id=first["work_id"], revision=revision,
                                      previous_event_id=prior["event_id"], contents="중단된 수정 1+1=2",
                                      criteria="산술 정확성")
                    store.admit(owner, current)
                    failed = (normalize_review(failure("backend_timeout")) if mode.startswith("executor")
                              else worker_failure("worker_interrupted"))
                    self.assertEqual(failed["outcome"], "unverified")
                    store.finish(current["event_id"], failed)
                    prior = current
                store = WorkflowStore(path)  # Persistent registry, not process-local state.
                current = request("review", work_id=first["work_id"], revision=failures + 2,
                                  previous_event_id=prior["event_id"], contents="1+1=2", criteria="산술 정확성")
                store.admit(owner, current)
                history = store.review_history(owner, current["work_id"], current["event_id"])
                job = make_job(current, "test", history)
                self.assertEqual(job["active_finding_ids"], ["F1"])
                self.assertEqual(job["known_finding_ids"], ["F1"])
                if "window" in mode:
                    self.assertEqual(len(history), 12)
                    self.assertEqual(history[0]["revision"], 2)
                omitted = parse_result(json.dumps(good("passed_in_scope")), job)
                self.assertFalse(omitted["completion_signal"])
                value = good("passed_in_scope")
                value["hyemi_result"]["resolved"] = ["F1"]
                result = parse_result(json.dumps(value), job)
                self.assertEqual(result["outcome"], "passed_in_scope")
                self.assertTrue(result["completion_signal"])
                canonical = normalize_review(result)
                store.finish(current["event_id"], canonical)
                self.assertEqual(store.result_for_internal(current["event_id"]), canonical)
                next_request = request("review", work_id=first["work_id"], revision=failures + 3,
                                       contents="1+1=3", criteria="산술 정확성")
                store.admit(owner, next_request)
                next_history = store.review_history(owner, first["work_id"], next_request["event_id"])
                next_job = make_job(next_request, "test", next_history)
                self.assertEqual(next_job["known_finding_ids"], ["F1"])
                self.assertEqual(next_job["active_finding_ids"], [])
                self.assertFalse(parse_result(json.dumps(good()), next_job)["completion_signal"])
                self.assertEqual(parse_result(json.dumps(good()), next_job)["outcome"], "unverified")
                with self.assertRaises(StoreConflict):
                    store.finish(next_request["event_id"], normalize_review(initial))

    def test_failure_markers_do_not_erase_or_supply_classifications(self):
        from hyemi_review import history_context
        first = {"request": REQUEST, "result": good()}
        value = good("passed_in_scope")
        value["hyemi_result"]["resolved"] = ["F1"]
        for outcome in ("failed", "cancelled", "interrupted", "unverified"):
            for error_site in ("result", "detail", "metadata", "record"):
                with self.subTest(outcome=outcome, error_site=error_site):
                    result = good("passed_in_scope")  # Pretend failure erased F1.
                    result["outcome"] = outcome
                    record = {"request": dict(REQUEST, contents="failure evidence"), "result": result}
                    error = {"code": "interrupted"}
                    if error_site == "result": result["error"] = error
                    if error_site == "detail": result["hyemi_result"]["error"] = error
                    if error_site == "metadata": result["hyemi_result"]["review_metadata"] = {"error": error}
                    if error_site == "record": record["error"] = error
                    record.update(known_finding_ids=["F1"], active_finding_ids=["F1"])
                    job = make_job(dict(REQUEST, revision=3, contents="1+1=2"), "test", [first, record])
                    self.assertTrue(parse_result(json.dumps(value), job)["completion_signal"])
                    self.assertFalse(parse_result(json.dumps(good("passed_in_scope")), job)["completion_signal"])
                    self.assertIn("failure evidence", history_context(job)[2])
                    legacy = {k: v for k, v in job.items() if k not in ("known_finding_ids", "active_finding_ids")}
                    legacy["history"] = [first, {k: v for k, v in record.items()
                                                if k not in ("known_finding_ids", "active_finding_ids")}]
                    self.assertTrue(parse_result(json.dumps(value), legacy)["completion_signal"])

    def test_active_annotation_and_scope_unverified_are_fail_closed(self):
        result = good()
        result["outcome"] = "unverified"  # Evidence scope, not execution failure.
        record = {"request": REQUEST, "result": result,
                  "known_finding_ids": ["F1"], "active_finding_ids": ["F1"]}
        current = dict(REQUEST, contents="1+1=2", revision=2, active_finding_ids=[])
        value = good("passed_in_scope")
        value["hyemi_result"]["resolved"] = ["F1"]
        self.assertTrue(parse_result(json.dumps(value), make_job(current, "test", [record]))["completion_signal"])
        for active in (None, "F1", {}, [1], [""], [" "], ["F1", "F1"], ["F2"], []):
            with self.subTest(active=active):
                bad = dict(record, active_finding_ids=active)
                self.assertFalse(parse_result(json.dumps(value), make_job(current, "test", [bad]))["completion_signal"])
        for broken in ({"summary": "legacy"}, dict(result["hyemi_result"], remaining=["F1"]), None):
            bad = dict(record, result=dict(result, hyemi_result=broken))
            self.assertFalse(parse_result(json.dumps(value), make_job(current, "test", [bad]))["completion_signal"])
        no_result = dict(record, result=None, hyemi_result=None)
        self.assertFalse(parse_result(json.dumps(value), make_job(current, "test", [no_result]))["completion_signal"])
        missing = dict(record)
        del missing["active_finding_ids"]
        self.assertFalse(parse_result(json.dumps(value), make_job(current, "test", [missing]))["completion_signal"])

    def test_legacy_function_mutation_is_denied(self):
        client = FakeClient(json.dumps(good()))
        client.response.choices[0].message.function_call = NS(name="terminal", arguments="{}")
        with self.assertRaisesRegex(ReviewError, "capability_denied"):
            review_with_client(client, "model", make_job(REQUEST, "test"))

    def test_failure_completion_is_boolean_false(self):
        self.assertIs(failure("backend_failed")["completion_signal"], False)

    def test_malformed_is_unverified_once(self):
        for raw in ("bad json", "[]", "{}", "```json\n{}\n```", json.dumps({"kind": "review"})):
            client = FakeClient(raw)
            result = review_with_client(client, "model", make_job(REQUEST, "test"))
            self.assertEqual(result["outcome"], "unverified")
            self.assertEqual(result["error"]["code"], "format_failed")
            self.assertEqual(len(client.calls), 1)
        bad = good()
        bad["outcome"] = "passed_in_scope"
        self.assertEqual(parse_result(json.dumps(bad))["error"]["code"], "format_failed")

    def test_no_process_global_environment_changes(self):
        before = dict(os.environ)
        response = json.dumps({"type": "final", "result": failure("backend_failed")})
        proc = NS(returncode=0, communicate=lambda *a, **kw: (response, None))
        with patch("hyemi_review.subprocess.Popen", return_value=proc) as spawn:
            result = ReviewExecutor({}).run(REQUEST, "test")
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["HERMES_HOME"], "/root/.hermes")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(result["error"]["code"], "backend_failed")

    def test_backend_failure_is_sanitized(self):
        with patch("hyemi_review.subprocess.Popen", side_effect=RuntimeError("SECRET_SENTINEL")):
            result = ReviewExecutor({}).run(REQUEST, "test")
        self.assertEqual(result["error"]["code"], "backend_failed")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))

    def test_timeout_kills_child_process_group(self):
        import subprocess
        proc = NS(pid=1234567, returncode=None)
        from unittest.mock import Mock
        proc.communicate = Mock(side_effect=[subprocess.TimeoutExpired("child", 1), ("", None)])
        with patch("hyemi_review.subprocess.Popen", return_value=proc), patch("hyemi_review.os.killpg") as kill:
            result = ReviewExecutor({"review_timeout_seconds": 1}).run(REQUEST, "test")
        self.assertEqual(result["error"]["code"], "backend_timeout")
        kill.assert_called_once()

    @unittest.skipUnless(os.getenv("HYEMI_HOST_RUNTIME_INTEGRATION") == "1",
                         "requires installed Hermes runtime; host integration opt-in")
    def test_runtime_import_fence_in_isolated_process(self):
        import subprocess
        from hyemi_review import PYTHON, SOURCE
        script = '''from hyemi_review_entry import install_runtime_fence
from hyemi_review import ReviewError
install_runtime_fence()
for name in ("run_agent", "model_tools", "tools.registry", "tools.mcp_tool"):
    try:
        __import__(name)
    except ReviewError:
        pass
    else:
        raise AssertionError(name)
from hermes_cli import plugins
try:
    plugins.discover_plugins()
except ReviewError:
    pass
else:
    raise AssertionError("plugin discovery")
print("fence_verified")
'''
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/root", "PYTHONPATH": SOURCE,
               "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run([PYTHON, "-c", script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, "isolated import fence failed")
        self.assertEqual(proc.stdout.strip(), "fence_verified")

    def test_child_environment_identity_guard(self):
        from hyemi_review_entry import run_backend
        with patch.dict(os.environ, {"HYEMI_REVIEW_CHILD": "1", "HERMES_HOME": "/root/.hermes"}):
            with self.assertRaisesRegex(ReviewError, "profile_path_denied"):
                run_backend({"profile": "hyerin"})

    @unittest.skipUnless(os.getenv("HYEMI_LIVE_REVIEW_CANARY") == "1", "live backend opt-in")
    def test_live_content_only(self):
        result = ReviewExecutor({"review_timeout_seconds": 90}).run(REQUEST, "hyemi-review-content-canary")
        print("LIVE_CANARY=" + json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["outcome"], "changes_required")
        self.assertEqual(result["backend"]["exposed_tools"], [])
        self.assertTrue(result["hyemi_result"]["findings"])
        revised = dict(REQUEST, contents="1+1=2", revision=2, changes="합계를 2로 수정")
        second = ReviewExecutor({"review_timeout_seconds": 90}).run(
            revised, "hyemi-review-content-canary",
            [{"request": REQUEST, "hyemi_result": result["hyemi_result"],
              "known_finding_ids": [f["id"] for f in result["hyemi_result"]["findings"]],
              "active_finding_ids": [f["id"] for f in result["hyemi_result"]["findings"]]}])
        print("LIVE_REVISION=" + json.dumps(second, ensure_ascii=False))
        self.assertEqual(second["outcome"], "passed_in_scope")
        self.assertIs(second["completion_signal"], True)
        self.assertEqual(second["hyemi_result"]["resolved"], [f["id"] for f in result["hyemi_result"]["findings"]])
        self.assertEqual(second["backend"]["exposed_tools"], [])


if __name__ == "__main__":
    unittest.main()
