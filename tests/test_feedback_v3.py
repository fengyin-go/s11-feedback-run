import unittest
from test_gate import (GATE, S1_PASS, S2, S3, S4, audit, evidence, facts, gate, task, v2_feedback)


class FeedbackV3Tests(unittest.TestCase):
    def check(self, doc):
        a = audit(doc['score'])
        return gate.check_feedback(doc, task(), facts(), a, evidence(a=a), GATE)[0]

    def candidate(self, score):
        doc = v2_feedback(score)
        doc['feedback_policy_version'] = 3
        for row in doc['code_review']['changed_files_reviewed']:
            row['consumer_check'] = '从 pack 入口跟到实际生成的清单并核对条目数'
        doc['process'] = v2_feedback(5)['process']
        doc['feedback'] = S2.rstrip('。') + '，' + S3 + (S4 if score == 4 else '') + '\n\n' + S1_PASS
        return doc

    def test_issue_and_public_summary_are_bound_to_the_result(self):
        doc = self.candidate(4)
        self.assertEqual([], self.check(doc))
        doc['score_basis']['remaining'] = S3
        self.assertTrue(any('有 finding' in error for error in self.check(doc)))
        doc['score_basis']['remaining'] = S4
        doc['feedback'] = doc['feedback'].replace('\n\n', ' ')
        self.assertTrue(any('按两段' in error for error in self.check(doc)))
        doc['feedback'] = self.candidate(4)['feedback']
        self.assertEqual([], self.check(doc))

    def test_clean_five_requires_review_of_consumers(self):
        doc = self.candidate(5)
        self.assertEqual([], self.check(doc))
        del doc['code_review']['changed_files_reviewed'][0]['consumer_check']
        self.assertTrue(any('consumer_check' in error for error in self.check(doc)))

    def test_archived_issue_paraphrase_and_current_exact_binding(self):
        issue = '按节点编号筛选时会漏行，使用前需要清理字段'
        basis = {'category': 'delivery_issue', 'remaining': '导出后按节点编号筛选时会漏行。',
                 'anchors': [{'type': 'probe', 'id': 'P1'}]}
        doc = {'score': 4, 'feedback': issue + '。', 'score_basis': basis,
               'issues': [{'sentence': issue, 'trigger': '按编号过滤', 'impact': '统计不完整',
                           'anchors': [{'type': 'probe', 'id': 'P1'}]}]}
        self.assertEqual(issue, gate.public_score_basis(doc)['remaining'])
        self.assertNotEqual(issue, basis['remaining'])
        doc['feedback_policy_version'] = 3
        self.assertEqual(basis, gate.public_score_basis(doc))
