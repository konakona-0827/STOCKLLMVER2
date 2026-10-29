"""Explicit DEVICEPW access; fixtures only, never sends a real email."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from support_scout.contracts import DataError
from support_scout.notifications import configure, load_config, _password, email_status, flush, enqueue_report, after_report
from support_scout.storage import atomic_json


class DevicePasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.db, self.env = self.root / 'email.json', self.root / 'events.sqlite3', self.root / '.env'
        self.value = configure('test@example.com', self.config, secret_env_file=self.env)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_quoted_device_password_strips_display_spaces_only(self):
        self.env.write_text("export DEVICEPW = 'abcd efgh ijkl mnop' # local app password\n", encoding='utf-8-sig')
        self.assertEqual(_password(load_config(self.config)), 'abcdefghijklmnop')
        state = email_status(self.config, self.db)
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(state['credential_source'], 'DEVICEPW_ENV_FILE')

    def test_double_quoted_and_unquoted_dotenv_syntax(self):
        for text in ('DEVICEPW="abcd efgh ijkl mnop"', 'DEVICEPW=abcdefghijklmnop # comment',
                     ' DevicePw = abcdefghijklmnop'):
            self.env.write_text(text, encoding='utf-8')
            self.assertEqual(_password(load_config(self.config)), 'abcdefghijklmnop')

    def test_only_explicit_devicepw_is_used_without_importing_other_fields(self):
        self.env.write_text('OPENAI_API_KEY=do-not-import\nBROKER_SECRET=do-not-import\nDEVICEPW=abcdefghijklmnop\n', encoding='utf-8')
        self.assertEqual(_password(load_config(self.config)), 'abcdefghijklmnop')
        self.assertNotIn('OPENAI_API_KEY', os.environ)
        self.assertNotIn('BROKER_SECRET', os.environ)
        self.assertNotIn('DEVICEPW', os.environ)

    def test_process_override_then_explicit_env_before_legacy_file(self):
        self.env.write_text('DEVICEPW=abcdefghijklmnop', encoding='utf-8')
        Path(self.value['password_file']).write_text('old-password', encoding='utf-8')
        with patch.dict(os.environ, {'DEVICEPW': 'qrst uvwx yzab cdef'}):
            self.assertEqual(_password(load_config(self.config)), 'qrstuvwxyzabcdef')
            self.assertEqual(email_status(self.config, self.db)['credential_source'], 'PROCESS_ENV')
        self.assertEqual(_password(load_config(self.config)), 'abcdefghijklmnop')

    def test_explicit_invalid_or_missing_devicepw_never_falls_back_to_old_secret(self):
        Path(self.value['password_file']).write_text('legacy-password', encoding='utf-8')
        for text in ('DEVICEPW=too-short', 'DEVICEPW=$(run-something)', 'OTHER=abcdefghijklmnop',
                     'DEVICEPW="abcdefghijklmnop" trailing', 'DEVICEPW=abcdefghijklmnop\nDEVICEPW=qrstuvwxyzabcdef'):
            self.env.write_text(text, encoding='utf-8')
            state = email_status(self.config, self.db)
            self.assertFalse(state['credential_configured'])
            self.assertNotEqual(state['status'], 'READY')
            with self.assertRaises(DataError):
                _password(load_config(self.config))

    def test_missing_or_unreadable_env_is_controlled_and_never_discloses_secret(self):
        state = email_status(self.config, self.db)
        self.assertEqual(state['credential_error'], 'DEVICEPW_ENV_FILE_UNREADABLE')
        self.env.write_text('DEVICEPW=abcd efgh ijkl mnop', encoding='utf-8')
        output = io.StringIO()
        with redirect_stdout(output):
            state = email_status(self.config, self.db)
        public = output.getvalue() + json.dumps(state)
        for secret in ('abcdefghijklmnop', 'abcd efgh ijkl mnop'):
            self.assertNotIn(secret, public)
        self.assertNotIn(str(self.env), public)

    def test_old_configuration_and_password_file_remain_compatible(self):
        value = configure('test@example.com', self.config)
        Path(value['password_file']).write_text('old-fixture-value', encoding='utf-8')
        self.assertNotIn('secret_env_file', load_config(self.config))
        self.assertEqual(_password(load_config(self.config)), 'old-fixture-value')
        self.assertEqual(email_status(self.config, self.db)['status'], 'READY')

    def test_relative_explicit_env_is_resolved_from_email_configuration(self):
        self.env.write_text('DEVICEPW=abcdefghijklmnop', encoding='utf-8')
        self.value['secret_env_file'] = '.env'
        atomic_json(self.config, self.value)
        self.assertEqual(_password(load_config(self.config)), 'abcdefghijklmnop')

    def test_disabled_even_with_valid_devicepw_does_not_send(self):
        self.env.write_text('DEVICEPW=abcdefghijklmnop', encoding='utf-8')
        self.value['enabled'] = False
        atomic_json(self.config, self.value)
        state = flush(self.config, self.db, sender=lambda *a: self.fail('disabled config sent'))
        self.assertEqual(state['status'], 'DISABLED')
        self.assertEqual(state['sent'], 0)

    def test_missing_credential_does_not_call_sender(self):
        self.env.write_text('DEVICEPW=', encoding='utf-8')
        state = flush(self.config, self.db, sender=lambda *a: self.fail('missing credential sent'))
        self.assertEqual(state['status'], 'NEEDS_GMAIL_APP_PASSWORD')
        self.assertEqual(state['sent'], 0)

    def test_opportunity_action_and_risk_plan_are_queued_and_changes_notify(self):
        report = {'synthetic':False, 'as_of':'2026-10-29T16:00:00+09:00',
            'desk':{'mode':'openai','selected_symbols':['6857.T']}, 'securities':[
                {'symbol':'6857.T','trade_decision':{'conclusion':'NO_TRADE','reason_codes':['OBSERVATION_ONLY']},
                 'opportunity_decision':{'action':'ENTRY_REVIEW','label':'人工積極進場評估',
                    'pattern_id':'H-002','reference_plan':{'support_lower':29700,'entry_max':31500},
                    'manual_checks':['核對最新報價'],'policy_version':'active-v1', 'ignored_internal':'not-a-mail-field'}}]}
        self.assertEqual(len(enqueue_report(report, self.root/'report.html', self.db)['queued']),1)
        self.assertEqual(enqueue_report(report, self.root/'report.html', self.db)['queued'],[])
        db=sqlite3.connect(self.db)
        payload=json.loads(db.execute('SELECT payload FROM events').fetchone()[0]);db.close()
        self.assertEqual(payload['action'],'ENTRY_REVIEW')
        self.assertEqual(payload['opportunity_decision']['reference_plan']['support_lower'],29700)
        self.assertNotIn('ignored_internal',payload['opportunity_decision'])
        report['as_of']='2026-10-29T17:00:00+09:00'
        report['securities'][0]['opportunity_decision']['action']='WAIT'
        self.assertEqual(len(enqueue_report(report, self.root/'report.html', self.db)['queued']),1)

    def test_retrospective_and_synthetic_opportunity_do_not_queue_or_flush(self):
        for synthetic,mode in ((True,''),(False,'RETROSPECTIVE_REAL_PRICE_CASE_RECONSTRUCTION')):
            report={'synthetic':synthetic,'analysis_context':{'mode':mode},'securities':[
                {'symbol':'6857.T','opportunity_decision':{'action':'ENTRY_REVIEW'}}]}
            with patch('support_scout.notifications.flush', side_effect=AssertionError('case sent')):
                result=after_report(report,self.root,config_path=self.config,db_path=self.db)
            self.assertEqual(result['queue']['queued'],[])
            self.assertEqual(result['delivery']['sent'],0)


if __name__ == '__main__':
    unittest.main()
