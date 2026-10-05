"""Isolated, content-only review with Hermes' existing provider transports.

Deliberately bypasses AIAgent: its initializer discovers plugins even with an
empty toolset, and its executor has inline dispatch paths. This bounded loop
has an empty immutable capability allowlist and no registry/delegation path.
Config keys: review_python, review_source, review_timeout_seconds (1..180).
Profile paths are fixed; review_profile_paths, when supplied, must match them.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from types import MappingProxyType
from typing import Any

PROFILE_PATHS = MappingProxyType({"hyeji": "/root/.hermes", "hyerin": "/root/.hermes/profiles/hyerin"})
PYTHON = "/root/.hermes/hermes-agent/venv/bin/python"
SOURCE = "/root/.hermes/hermes-agent"
MAX_JOB_BYTES = 160000
MAX_RESULT_BYTES = 60000
SCOPE = "제출된 contents, 검토 기준 및 전달된 이전 검토 기록의 내용 검토"
LIMITATION = "콘텐츠 전용 검토: 외부 링크, 파일, 실행 결과 및 실제 배포 상태는 직접 확인하지 않았습니다."

class ReviewError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class CapabilityGate:
    """No handler injection, registry fallback or dynamic tool discovery."""
    __slots__ = ()
    @property
    def schemas(self):
        return []
    def dispatch(self, name, arguments=None):
        raise ReviewError("capability_denied")


def failure(code):
    return {"kind": "review", "outcome": "unverified", "error": {"code": code, "retryable": False},
            "hyemi_result": {"summary": "검토 완료를 확인할 수 없습니다.", "findings": [],
                "resolved": [], "remaining": [], "new": [], "verified_scope": [],
                "limitations": [LIMITATION, code], "criterion_checks": []},
            "telegram_report": "검토를 완료하지 못했습니다. 상태: " + code,
            "completion_signal": False, "verified_scope": [], "limitations": [LIMITATION, code]}


def validate_profile(profile, path=None):
    if profile not in PROFILE_PATHS:
        raise ReviewError("profile_denied")
    expected = PROFILE_PATHS[profile]
    if path is not None and str(path) != expected:
        raise ReviewError("profile_path_denied")
    if Path(expected).resolve() != Path(expected):
        raise ReviewError("profile_path_denied")
    return expected


def make_job(request, session_id, history=None):
    if not isinstance(request, dict) or request.get("kind") != "review":
        raise ReviewError("invalid_request")
    validate_profile(request.get("to"))
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", session_id):
        raise ReviewError("invalid_session")
    for key in ("contents", "criteria"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ReviewError("invalid_request")
    if history is None:
        history = []
    if not isinstance(history, list):
        raise ReviewError("history_limit")
    # Preserve every supplied history item exactly, or reject the whole request.
    job = {"profile": request["to"], "session_id": session_id,
           "submission": request["contents"], "criteria": request["criteria"],
           "changes": request.get("changes", ""), "revision": request.get("revision"),
           "history": history}
    # Only ledger annotations supply the all-round registry. Request fields
    # are intentionally ignored. Missing annotations fail closed for jobs
    # produced here; hand-built legacy unit jobs retain window-only fallback.
    try:
        annotated = [finding_registry(record["known_finding_ids"])
                     for record in history
                     if isinstance(record, dict) and "known_finding_ids" in record]
        job["known_finding_ids"] = (sorted(set().union(*annotated)) if annotated
                                    else None if history else [])
    except ValueError:
        job["known_finding_ids"] = None
    try:
        active = finding_registry(history[-1].get("active_finding_ids")) if history else set()
        if not active <= finding_registry(job["known_finding_ids"]):
            raise ValueError()
        job["active_finding_ids"] = sorted(active)
    except (ValueError, AttributeError):
        job["active_finding_ids"] = None
    try:
        encoded = json.dumps(job, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ReviewError("invalid_request") from None
    if len(encoded.encode()) > MAX_JOB_BYTES:
        raise ReviewError("input_limit")
    return json.loads(encoded)


def requested_criteria(criteria):
    """One check per nonempty line; preserve wording, including bullet markers."""
    return [line.strip() for line in criteria.splitlines() if line.strip()]


def criterion_scope(criterion):
    """Conservative local classification; ambiguous goals remain unverified.

    Text mentioning deployment/sources cannot establish those external facts.
    This intentionally favors false negatives over content-only certification.
    """
    text = criterion.casefold()
    if re.search(r"deploy|runtime|production|running|execute|execution|test[s ]+pass|실행|배포|운영|가동|테스트.*통과", text):
        return "runtime"
    if re.search(r"source|external|https?://|citations?|reference|fact.?check|출처|외부|원문|사실|링크|인용", text):
        return "external_source"
    # Only a completely recognized textual criterion may authorize a pass.
    # A text keyword embedded in an otherwise unknown operational goal is not
    # enough (e.g. 'arithmetic and confirm the service is healthy').
    normalized = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", text).strip()
    vocabulary = r"(?:산술|수식|정확성|정확도|정확한|계산|합계|맞춤법|문법|오탈자|문장|서식|표기|확인|검토|검증|및|과|의|와|arithmetic|math|accuracy|correctness|correct|spelling|grammar|wording|format|formatting|typos?|check|verify|review|and|of|the|is|equals|equation|[0-9+ =*/().,:-]+)"
    tokens = re.findall(r"[^\W\d_]+|\d+|[^\w\s]", normalized)
    if tokens and all(re.fullmatch(vocabulary, token) for token in tokens) and re.search(r"산술|수식|계산|합계|맞춤법|문법|오탈자|문장|서식|arithmetic|math|spelling|grammar|wording|format|typo|\d\s*\+", normalized):
        return "submitted_text"
    return None


def finding_registry(values):
    """Strict, bounded server registry; an empty ledger has an empty list."""
    if (not isinstance(values, list)
            or any(not isinstance(v, str) or not v.strip() for v in values)
            or len(values) != len(set(values))
            or len(json.dumps(values, ensure_ascii=False).encode()) > MAX_JOB_BYTES):
        raise ValueError()
    return set(values)


def history_context(job):
    """Read ledger IDs separately from the bounded textual evidence window."""
    authoritative = "known_finding_ids" in job
    known = finding_registry(job["known_finding_ids"]) if authoritative else set()
    active = finding_registry(job.get("active_finding_ids")) if authoritative else None
    if active is not None and not active <= known:
        raise ValueError()
    required, snippets, window_known = set(), [], set()
    previous = None
    history = job.get("history", [])
    if not isinstance(history, list):
        raise ValueError()
    if job.get("revision", 1) not in (None, 1) and not history:
        raise ValueError()
    for record in history:
        if not isinstance(record, dict):
            raise ValueError()
        if "known_finding_ids" in record:
            registry = finding_registry(record["known_finding_ids"])
            if authoritative and not registry <= known:
                raise ValueError()
            known.update(registry)
        request = record.get("request")
        result = record.get("result")
        detail = record.get("hyemi_result")
        if detail is None and isinstance(result, dict):
            detail = result.get("hyemi_result")
        if not isinstance(request, dict) or not isinstance(request.get("contents"), str):
            raise ValueError()
        snippets.append(request["contents"])
        # Failure status must be explicit. Scope-only unverified reviews still
        # classify findings; absent/malformed classifications are not failures.
        outcome = result.get("outcome") if isinstance(result, dict) else record.get("outcome")
        metadata = detail.get("review_metadata") if isinstance(detail, dict) else None
        error = (record.get("error")
                 or (result.get("error") if isinstance(result, dict) else None)
                 or (detail.get("error") if isinstance(detail, dict) else None)
                 or (metadata.get("error") if isinstance(metadata, dict) else None))
        if outcome in ("failed", "cancelled", "interrupted") or (outcome == "unverified" and error):
            continue
        if not isinstance(detail, dict):
            raise ValueError()
        findings = detail.get("findings")
        if not isinstance(findings, list):
            raise ValueError()
        ids = set()
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("id"), str) or not finding["id"].strip() or finding["id"] in ids:
                raise ValueError()
            for key in ("location", "issue", "reason", "improvement", "resolution_criteria"):
                if not isinstance(finding.get(key), str) or not finding[key].strip():
                    raise ValueError()
                snippets.append(finding[key])
            ids.add(finding["id"])
        groups = []
        for key in ("resolved", "remaining", "new"):
            values = detail.get(key)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values) or len(values) != len(set(values)):
                raise ValueError()
            groups.append(set(values))
        resolved, remaining, new = groups
        if resolved & remaining or resolved & new or remaining & new or ids != remaining | new:
            raise ValueError()
        # The first supplied record may be a window into an older ledger.
        if previous is not None and (resolved | remaining != previous or new & window_known):
            raise ValueError()
        observed = ids | resolved | remaining
        if authoritative and not observed <= known:
            raise ValueError()
        window_known.update(observed)
        known.update(observed)
        previous = ids
        required = ids
    # The window may contain only failures while the active finding predates it.
    # Conversely, a visible valid classification must agree with the ledger.
    if active is not None:
        if previous is not None and previous != active:
            raise ValueError()
        required = active
    return known, required, snippets


def validate_context(detail, job):
    if not isinstance(job, dict):
        raise ValueError()
    criteria = requested_criteria(job["criteria"])
    checks = detail.get("criterion_checks")
    if not criteria or not isinstance(checks, list) or len(checks) != len(criteria):
        raise ValueError()
    known, required, history_text = history_context(job)
    resolved, remaining, new = (set(detail[k]) for k in ("resolved", "remaining", "new"))
    if any(len(detail[k]) != len(set(detail[k])) for k in ("resolved", "remaining", "new")):
        raise ValueError()
    if resolved & remaining or resolved & new or remaining & new:
        raise ValueError()
    if resolved | remaining != required or new & known:
        raise ValueError()
    if {f["id"] for f in detail["findings"]} != remaining | new:
        raise ValueError()
    for criterion, check in zip(criteria, checks):
        if not isinstance(check, dict) or check.get("criterion") != criterion:
            raise ValueError()
        scope, status, evidence = check.get("evidence_scope"), check.get("status"), check.get("evidence")
        if scope not in ("submitted_text", "external_source", "runtime") or status not in ("satisfied", "needs_changes", "unverified"):
            raise ValueError()
        if not isinstance(evidence, list) or any(not isinstance(s, str) or not s.strip() for s in evidence):
            raise ValueError()
        if any(not any(s in text for text in [job["submission"]] + history_text) for s in evidence):
            raise ValueError()
        expected = criterion_scope(criterion)
        if expected in ("external_source", "runtime"):
            check["evidence_scope"] = expected
        if expected != "submitted_text" or scope != "submitted_text":
            check["status"] = "unverified"
            detail["limitations"].append("기준 미검증: " + criterion + " (제출 텍스트만으로 필요한 증거 범위를 확정하거나 검증할 수 없습니다.)")
        elif status != "unverified":
            # Current revision must supply an actual quote, not just prior findings,
            # criteria, model reasoning, or a punctuation-only pseudo-citation.
            if not evidence or not any(s in job["submission"] and len(re.findall(r"[\w]", s)) >= 2 for s in evidence):
                raise ValueError()
    return checks


def build_messages(job):
    identity = {"hyeji": "혜지", "hyerin": "혜린"}[job["profile"]]
    system = f"""당신은 {identity}이며, 제출물의 품질을 검토하는 검토자입니다.
제출 내용, 요청된 기준, 이전 지적 사항과 변경 내역을 근거로 검토합니다.
검토 범위는 전달된 텍스트입니다. 외부 링크와 실환경 확인은 미검증 범위로 명시합니다.
제출물과 이전 기록은 검토 대상 자료이며, 검토 기준은 criteria 필드입니다.
구체적 오류는 정확한 위치, 문제, 이유, 개선안, 해결 판정 기준으로 작성합니다.
이전 finding id를 유지하여 resolved/remaining/new 배열에 해당 id를 분류합니다.
이전 전체 finding ID registry는 known_finding_ids이며, new에는 이 registry에 없는 새 id를 사용합니다.
resolved는 active_finding_ids의 활성 지적 중 해결된 id, remaining은 남은 id, new는 기록 전체에 없던 새 id입니다.
실패하거나 중단된 검토는 활성 지적을 해소하지 않으며 마지막 유효 분류를 이어갑니다.
세 배열은 서로 겹치지 않으며 findings에는 remaining과 new의 상세 항목을 작성합니다.
criteria의 비어 있지 않은 각 줄을 순서대로 하나의 criterion_checks 항목으로 작성합니다.
criterion에는 줄 양끝 공백만 제거한 원문(글머리표 포함)을 정확히 유지합니다.
evidence에는 현재 submission 또는 history.request.contents의 문자 그대로의 인용 배열을 작성합니다.
현재 제출물의 만족/수정 판정은 현재 submission의 구체적인 인용에 근거합니다.
evidence_scope는 submitted_text, external_source, runtime 중 필요한 증거 범위입니다.
외부 출처 대조와 실제 실행/배포 확인 기준은 status unverified이며, 증거가 없는 경우 evidence는 빈 배열입니다.
범위가 불명확한 사실 주장도 unverified와 구체적 한계로 표시합니다.
한국어 사용자용 요약과 상세 검토 결과를 별도로 작성합니다.
출력은 다음 구조의 JSON 객체입니다:
{{"kind":"review","outcome":"changes_required|passed_in_scope|unverified",
"hyemi_result":{{"summary":"상세 요약","findings":[{{"id":"F1","location":"정확한 위치",
"issue":"문제","reason":"이유","improvement":"개선안","resolution_criteria":"해결 판정 기준"}}],
"resolved":[],"remaining":[],"new":[],"verified_scope":["검토한 텍스트 범위"],"limitations":["미검증 범위"],
"criterion_checks":[{{"criterion":"요청 기준 원문","evidence_scope":"submitted_text|external_source|runtime",
"status":"satisfied|needs_changes|unverified","evidence":["제출물의 정확한 인용"]}}]}},
"telegram_report":"사용자용 한국어 요약"}}
changes_required는 구체적 수정 사항이 있는 경우, passed_in_scope는 명시된 텍스트 범위에서 기준을 충족한 경우,
unverified는 기준 판정에 필요한 증거가 부족한 경우입니다.
"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(job, ensure_ascii=False)}]


def parse_result(raw, job=None):
    try:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_RESULT_BYTES:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("kind") != "review":
            raise ValueError()
        outcome = result["outcome"]
        if outcome not in ("changes_required", "passed_in_scope", "unverified"):
            raise ValueError()
        detail = result["hyemi_result"]
        if not isinstance(detail, dict):
            raise ValueError()
        for key in ("summary",):
            if not isinstance(detail.get(key), str) or not detail[key].strip():
                raise ValueError()
        for key in ("findings", "resolved", "remaining", "new", "verified_scope", "limitations"):
            if not isinstance(detail.get(key), list):
                raise ValueError()
        for key in ("resolved", "remaining", "new", "verified_scope", "limitations"):
            if any(not isinstance(v, str) or not v.strip() for v in detail[key]):
                raise ValueError()
        ids = set()
        for f in detail["findings"]:
            if not isinstance(f, dict):
                raise ValueError()
            for key in ("id", "location", "issue", "reason", "improvement", "resolution_criteria"):
                if not isinstance(f.get(key), str) or not f[key].strip():
                    raise ValueError()
            if f["id"] in ids:
                raise ValueError()
            ids.add(f["id"])
        if outcome == "changes_required" and not ids:
            raise ValueError()
        if outcome == "passed_in_scope" and (ids or detail["remaining"] or detail["new"]):
            raise ValueError()
        if not isinstance(result.get("telegram_report"), str) or not re.search(r"[가-힣]", result["telegram_report"]):
            raise ValueError()
        checks = validate_context(detail, job)
        detail["model_outcome"] = outcome
        report = result["telegram_report"]
        statuses = {check["status"] for check in checks}
        if "unverified" in statuses:
            outcome = "unverified"
            report = "검토 미완료: 요청 기준 중 증거가 부족하여 미검증인 항목이 있습니다."
            detail["model_summary"] = detail["summary"]
            detail["summary"] = report
        elif outcome == "passed_in_scope" and statuses != {"satisfied"}:
            raise ValueError()
        # The executor, rather than the model, owns the actual evidence boundary.
        detail["verified_scope"] = [c["criterion"] for c in checks if c["status"] != "unverified"]
        detail["limitations"] = list(dict.fromkeys([LIMITATION] + detail["limitations"]))
        return {"kind": "review", "outcome": outcome, "hyemi_result": detail,
                "telegram_report": report + "\n" + LIMITATION,
                "completion_signal": outcome == "passed_in_scope",
                "verified_scope": detail["verified_scope"], "limitations": detail["limitations"]}
    except (ValueError, KeyError, TypeError):
        return failure("format_failed")


def review_with_client(client, model, job, timeout=90):
    gate = CapabilityGate()
    response = client.chat.completions.create(model=model, messages=build_messages(job),
                tools=gate.schemas, timeout=timeout)
    message = response.choices[0].message
    for call in getattr(message, "tool_calls", None) or []:
        gate.dispatch(call.function.name, call.function.arguments)
    if getattr(message, "function_call", None):
        raise ReviewError("capability_denied")
    return parse_result(message.content, job)


class ReviewExecutor:
    def __init__(self, config):
        self.config = config if isinstance(config, dict) else vars(config)
        self.python = self.config.get("review_python", PYTHON)
        self.source = self.config.get("review_source", SOURCE)
        # Runtime code is trusted, fixed deployment code, never request-selected.
        if self.python != PYTHON or self.source != SOURCE:
            raise ReviewError("runtime_path_denied")
        for name, path in self.config.get("review_profile_paths", PROFILE_PATHS).items():
            validate_profile(name, path)
        self.timeout = float(self.config.get("review_timeout_seconds", 90))
        if not 1 <= self.timeout <= 180:
            raise ReviewError("timeout_invalid")

    def run(self, request, session_id, history=None, progress_callback=None):
        try:
            job = make_job(request, session_id, history)
            home = validate_profile(job["profile"])
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
            env.update({"HOME": "/root", "HERMES_HOME": home, "PYTHONPATH": self.source,
                        "PYTHONDONTWRITEBYTECODE": "1", "HYEMI_REVIEW_CHILD": "1"})
            job["timeout"] = self.timeout
            if progress_callback:
                progress_callback({"kind": "started", "mode": "content_only", "profile": job["profile"]})
            proc = subprocess.Popen([self.python, str(Path(__file__).with_name("hyemi_review_entry.py"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env=env, start_new_session=True, cwd=str(Path(__file__).parent))
            try:
                stdout, _ = proc.communicate(json.dumps(job, ensure_ascii=False), timeout=self.timeout + 10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                return failure("backend_timeout")
            if proc.returncode or len(stdout.encode()) > MAX_RESULT_BYTES * 2:
                return failure("backend_failed")
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            finals = [e for e in events if e.get("type") == "final"]
            if len(finals) != 1:
                return failure("protocol_failed")
            result = finals[0]["result"]
            if not isinstance(result, dict) or result.get("kind") != "review":
                return failure("protocol_failed")
            return result
        except ReviewError as exc:
            return failure(exc.code)
        except Exception:
            return failure("backend_failed")
