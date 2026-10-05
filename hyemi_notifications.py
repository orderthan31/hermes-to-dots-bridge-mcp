"""Deterministic plain-text notifications; never trust model prose for identity."""
import json


STATUS = {'completed': '완료', 'passed_in_scope': '검토 범위 내 통과',
          'changes_required': '수정 필요', 'unverified': '미검증', 'failed': '실패',
          'blocked': '차단', 'cancelled': '취소', 'interrupted': '중단'}


def notification_identity(row):
    request = row['request']
    kind = row['kind']
    return dict(title=request.get('title', '').strip() or ('검토 요청' if kind == 'review' else '작업 요청'),
                work_id=row['work_id'], event_id=row['event_id'], kind=kind,
                revision=request.get('revision'))


def _header(row, phase):
    identity = notification_identity(row)
    mode = 'review' + str(identity['revision']) if identity['kind'] == 'review' else 'task'
    return (f"{identity['title']} | {mode} | {phase}\n"
            f"작업 ID: {identity['work_id']}\n이벤트 ID: {identity['event_id']}")


def render_progress(row, phase, text):
    if row['kind'] == 'report':
        return text
    label = '시작' if phase == 'started' else '진행 중'
    return _header(row, label) + '\n' + text


def _value(value, default='해당 없음'):
    if value is None or value == '' or value == [] or value == {}:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join('- ' + _value(item) for item in value)
    if isinstance(value, dict):
        return '; '.join(str(k) + ': ' + _value(v) for k, v in value.items())
    return json.dumps(value, ensure_ascii=False)


def render_notification(row, canonical_result):
    """Render validated detail only. Raw reports deliberately bypass all formatting."""
    if row['kind'] == 'report':
        return row['request']['contents']
    detail = canonical_result['hyemi_result']
    request = row['request']
    review = row['kind'] == 'review'
    outcome = canonical_result['outcome']
    review_metadata = detail.get('review_metadata')
    if not isinstance(review_metadata, dict):
        review_metadata = {}
    lines = [_header(row, '결과'), '상태: ' + STATUS.get(outcome, outcome) + ' (' + outcome + ')']
    if review:
        lines.append('검토 범위 제한: 제출 내용(contents)과 기준에 대한 검토입니다. 전체 소스·실제 실행·외부 원본을 검증한 결과가 아닙니다.')
    lines.append('요약: ' + _value(detail.get('summary'), '미검증'))
    sections = [('검토 기준' if review else '수락 기준', request.get('criteria' if review else 'acceptance_criteria'), '미검증'),
                ('확인한 내용', detail.get('verified_scope') if review else detail.get('completed_steps'), '미검증'),
                ('기준별 검증', detail.get('criterion_checks'), '미검증'),
                ('검증 증거', detail.get('evidence'), '미검증'),
                ('문제점', detail.get('findings') or detail.get('issues'), '미검증' if not canonical_result['completion_signal'] else '해당 없음'),
                ('변경 사항', detail.get('changes') or request.get('changes'), '해당 없음'),
                ('해결', detail.get('resolved'), '해당 없음'),
                ('잔존', detail.get('remaining'), '해당 없음'),
                ('신규', detail.get('new'), '해당 없음'),
                ('차단 요인', detail.get('blockers') or detail.get('error'), '미검증' if outcome in {'blocked','failed','interrupted','unverified'} else '해당 없음'),
                ('제한 사항', detail.get('limitations') or review_metadata.get('limitations'), '해당 없음'),
                ('결과 링크', detail.get('result_links'), '해당 없음'),
                ('다음 행동', detail.get('next_actions'), '혜미가 수정 후 재검토 요청' if review and outcome != 'passed_in_scope' else '해당 없음' if canonical_result['completion_signal'] else '혜미가 미검증 사항 확인 후 후속 작업 요청')]
    for label, value, default in sections:
        lines.append(label + ':\n' + _value(value, default))
    rendered = '\n\n'.join(lines)
    if len(rendered.encode('utf-8')) <= 40000:
        return rendered
    # The canonical report contract has a 40 KB ceiling. Keep every section,
    # explicitly compact only the display; exact values remain in request/detail.
    suffix = '\n(표시 축약: 전체 내용은 저장된 요청·상세 결과 참조)'
    def compact(line):
        data = line.encode('utf-8')
        return line if len(data) <= 2200 else data[:2000].decode('utf-8', errors='ignore') + suffix
    return '\n\n'.join(compact(line) for line in lines)


def notification_result(row, canonical_result):
    """Keep exact structured findings and original model prose separate from delivery."""
    detail = dict(canonical_result['hyemi_result'])
    detail['notification'] = dict(notification_identity(row),
                                  model_telegram_report=canonical_result['telegram_report'])
    return dict(canonical_result, hyemi_result=detail,
                telegram_report=render_notification(row, canonical_result))
