"""Pure presentation/provenance regression tests (no provider or Telegram calls)."""
import copy
import unittest

from hyemi_delivery import split_text
from hyemi_notifications import notification_result, render_notification, render_progress
from hyemi_workers import served_provenance, failure


class NotificationsTest(unittest.TestCase):
    def row(self, kind='review', **values):
        request = dict(title='원본 제목 <태그> *그대로*', revision=7, criteria='권한 확인',
                       acceptance_criteria='도구 결과 확인', contents='raw\n **unchanged**  ')
        request.update(values)
        return dict(kind=kind,work_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    event_id='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',request=request)

    def test_review_fields_and_original_metadata_are_separate(self):
        row=self.row()
        result=dict(outcome='changes_required',completion_signal=False,telegram_report='모델 제목은 무시',
                    hyemi_result=dict(summary='확인 결과',findings=[{'id':'F1','description':'권한 검사 누락'}],
                        verified_scope=['제출 텍스트'],resolved=['F0'],remaining=[],new=['F1'],
                        criterion_checks=[{'criterion':'권한 확인','status':'failed'}],limitations=['내용만 검토']))
        before=copy.deepcopy(result)
        canonical=notification_result(row,result)
        self.assertEqual(result,before)
        for expected in ('원본 제목 <태그> *그대로*','review7','F0','F1','권한 검사 누락','내용만 검토','혜미가 수정 후 재검토 요청'):
            self.assertIn(expected,canonical['telegram_report'])
        self.assertNotIn('모델 제목은 무시',canonical['telegram_report'])
        for key,value in result['hyemi_result'].items():
            self.assertEqual(canonical['hyemi_result'][key],value)
        self.assertEqual(canonical['hyemi_result']['notification']['revision'],7)

    def test_failure_every_round_retains_identity_and_required_sections(self):
        for kind in ('task','review'):
            for outcome in ('failed','interrupted','unverified','cancelled','blocked'):
                row=self.row(kind,title='')
                text=render_notification(row,failure('fixture_failure',outcome))
                for expected in (row['event_id'],row['work_id'],'상태','확인한 내용','문제점','변경 사항','해결','잔존','신규','차단 요인','다음 행동','미검증'):
                    self.assertIn(expected,text)
                self.assertIn('검토 요청' if kind=='review' else '작업 요청',text)

    def test_started_and_progress_have_identity_not_final_claims(self):
        for kind in ('review','task'):
            row=self.row(kind)
            for phase in ('started','progress:0','status:waiting_for_input'):
                text=render_progress(row,phase,'실행 중입니다.')
                for expected in (row['work_id'],row['event_id'],row['request']['title']):
                    self.assertIn(expected,text)
                self.assertNotIn('완료',text)
                self.assertNotIn('| 결과',text)

    def test_raw_report_has_zero_format_changes(self):
        row=self.row('report')
        self.assertEqual(render_notification(row,{}),row['request']['contents'])
        self.assertEqual(render_progress(row,'started',row['request']['contents']),row['request']['contents'])

    def test_long_formatted_artifact_chunks_are_lossless(self):
        row=self.row()
        result=failure('x')
        result['hyemi_result']['findings']=['상세 문제 😀\n'*800]
        text=render_notification(row,result)
        chunks=split_text(text)
        self.assertGreater(len(chunks),1)
        self.assertEqual(''.join(chunks),text)

    def test_contract_ceiling_keeps_sections_and_full_detail(self):
        from hyemi_contracts import validate_result
        row=self.row(criteria='기준'*6000)
        result=failure('x')
        result['hyemi_result']['summary']='상세'*6000
        canonical=notification_result(row,result)
        validate_result(canonical,'review')
        self.assertEqual(canonical['hyemi_result']['summary'],result['hyemi_result']['summary'])
        self.assertIn('표시 축약',canonical['telegram_report'])
        self.assertIn('다음 행동',canonical['telegram_report'])
        self.assertEqual(''.join(split_text(canonical['telegram_report'])),canonical['telegram_report'])

    def test_provenance_positive_allowlist_and_exact_types(self):
        status={'run_id':'run_'+'a'*32,'model':'requested-only',
            'runtime':{'provider':'served','model':'safe/model:version','route_source':'global',
                'model_lock':'session','source':'SECRET','base_url':'SECRET','headers':{'Authorization':'SECRET'},
                'requested':{'provider':'primary','model':'request-model','token':'SECRET'}},
            'usage':{'input_tokens':1,'output_tokens':0,'total_tokens':True,
                     'cache_read_tokens':-1,'cache_write_tokens':'10','api_key':'SECRET'}}
        safe=served_provenance(status)
        self.assertEqual(safe['runtime']['model'],'safe/model:version')
        self.assertEqual(safe['usage'],{'input_tokens':1,'output_tokens':0,'run_id':status['run_id']})
        self.assertNotIn('SECRET',str(safe))
        self.assertNotIn('source',safe['runtime'])
        self.assertEqual(safe['runtime']['requested'],{'provider':'primary','model':'request-model'})

    def test_provenance_rejects_urls_credentials_oversize_and_missing_actual(self):
        run_id='run_'+'a'*32
        for value in ('https://host/token','Bearer abc','sk-secret','x'*257,123,{},True,'bad\nline'):
            result=served_provenance({'run_id':run_id,'runtime':{'model':value},'model':'requested'})
            self.assertNotIn('runtime',result)
        self.assertEqual(served_provenance({'run_id':run_id,'model':'requested'}),{})
        self.assertEqual(served_provenance({'run_id':'bad','runtime':{'model':'real'}}),{})


if __name__=='__main__': unittest.main()
