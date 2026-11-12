"""Metadata repair changes delivery fields without altering frozen/model inputs."""
import copy
from pathlib import Path
from fixtures import Sandbox
from test_sheet_batch import SheetCase
import fbr_fields as fields
import fbrlib as lib
import fbr_delivery as delivery
import fbr_sheet


def revision(task):
    directory = task / 'evidence/field-revisions'
    directory.mkdir(parents=True, exist_ok=True)
    schema = directory / 'platform.json'
    proof = directory / 'environment.json'
    lib.save_json(schema, {'platform':'https://solo2.jzxhnh.com', 'checked_at':'2026-11-12',
        'fingerprint':'offline-fixture', 'fields':[{'field_key':'repro_level',
        'field_type':'select','options':['已容器化，可一键起环境']}]})
    lib.save_json(proof, {'kind':'offline environment evidence fixture'})
    ref = lambda p: {'path':p.relative_to(task).as_posix(),'sha256':fields.sha(p)}
    doc = {'version':1,'task_sha256':fields.sha(task/'task.json'), 'fields':{'repro_level':{
        'old_value':lib.load_task(task).get('repro_level'), 'value':'已容器化，可一键起环境',
        'verified_at':'2026-11-12','reason':'Offline revision fixture',
        'schema':ref(schema),'sources':[ref(proof)]}}}
    lib.save_json(task/fields.REVISIONS,doc)
    return doc,proof


class FieldRevisions(Sandbox):
    def test_valid_revision_leaves_frozen_file_unchanged(self):
        task=self.make_task();before=(task/'task.json').read_bytes()
        revision(task)
        self.assertEqual(fields.effective_record(task)['repro_level'],'已容器化，可一键起环境')
        self.assertEqual((task/'task.json').read_bytes(),before)

    def test_missing_or_changed_evidence_cannot_apply(self):
        for remove in (False,True):
            task=self.make_task(2 if remove else 1);doc,proof=revision(task)
            proof.unlink() if remove else proof.write_text('{}')
            with self.assertRaisesRegex(lib.FbrError,'来源缺失或摘要已变化'):
                fields.effective_record(task)

    def test_unknown_enum_and_frozen_drift_are_rejected(self):
        task=self.make_task();doc,_=revision(task)
        doc['fields']['repro_level']['value']='Linux arm64 已启动'
        lib.save_json(task/fields.REVISIONS,doc)
        with self.assertRaisesRegex(lib.FbrError,'准确选项'):fields.effective_record(task)
        doc['task_sha256']='0'*64;lib.save_json(task/fields.REVISIONS,doc)
        with self.assertRaisesRegex(lib.FbrError,'冻结 task'):fields.effective_record(task)

    def test_protected_fields_and_cross_task_sources_are_rejected(self):
        task=self.make_task();doc,_=revision(task)
        bad=copy.deepcopy(doc);bad['fields']['score']=5
        lib.save_json(task/fields.REVISIONS,bad)
        with self.assertRaisesRegex(lib.FbrError,'未支持字段'):fields.effective_record(task)
        doc['fields']['repro_level']['sources'][0]['path']='../other/evidence.json'
        lib.save_json(task/fields.REVISIONS,doc)
        with self.assertRaisesRegex(lib.FbrError,'本题 evidence'):fields.effective_record(task)


class RevisionDelivery(SheetCase):
    def test_row_summary_and_delivery_inventory_use_verified_revision(self):
        task=self.gated();revision(task)
        row,_=self.row(task)
        self.assertEqual(row['环境可复现等级'],'已容器化，可一键起环境')
        manifest=delivery.export_batch(self.root,task.parent.name,self.cfg)
        exported=delivery.delivery_dir(self.root,task.parent.name)/task.name
        self.assertEqual(lib.load_json(exported/'task-summary.json')['repro_level'],row['环境可复现等级'])
        names={x['path'] for x in manifest['tasks'][0]['files']}
        self.assertTrue(any(x.endswith(fields.REVISIONS) for x in names))
        self.assertTrue(any(x.endswith('evidence/field-revisions/platform.json') for x in names))
