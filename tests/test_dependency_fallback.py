from pathlib import Path
from unittest import mock
from fixtures import Sandbox, done, write
import fbrlib as lib
import fbr_dependencies as deps

class DependencyFallback(Sandbox):
    def test_dirty_setup_is_not_copied_and_task_remains_clean(self):
        task=self.make_task(); repo=task/'workspace'
        before=lib.git_out(repo,'status','--porcelain')
        cfg={**self.cfg,'dependencies':{'enabled':True}}
        original=lib.run
        def run(args,**kwargs):
            if args[:3]==['docker','image','inspect']:
                return done(0,cfg['image_id']+'|linux|arm64')
            return original(args,**kwargs)
        def install(image,clone,steps,**kwargs):
            write(clone/'frontend/package-lock.json','generated lock')
            (clone/'frontend/node_modules').mkdir()
            return [{'rc':0},{'rc':0}]
        with mock.patch.object(deps,'roots',return_value=['frontend/node_modules']), \
             mock.patch.object(lib,'run',side_effect=run), \
             mock.patch.object(lib,'run_steps_in_image',side_effect=install):
            self.assertFalse(deps.seed(repo,cfg,task=task))
            self.assertFalse(deps.seed(repo,cfg,task=task))
        self.assertEqual(lib.git_out(repo,'status','--porcelain'),before)
        self.assertFalse((repo/'frontend/node_modules').exists())
        manifests=list((self.root/'_registry/dependencies').glob('*/manifest.json'))
        self.assertEqual(len(manifests),1)
        self.assertEqual(lib.load_json(manifests[0])['state'],'unavailable')
