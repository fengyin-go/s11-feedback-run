"""Caches/parallel slots: real isolated Git and mocked Docker/model; no network."""
import copy
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from fixtures import Sandbox, write, sh, done, REAL_RUN
import fbrlib as lib
import fbr_dependencies as deps
import fbr_common as common
import fbr_review_slots as slots


class DependencyCache(Sandbox):
    def baseline(self):
        task=self.make_task();repo=task/'workspace'
        write(repo/'package.json','{"name":"selftest","version":"1.0.0"}')
        write(repo/'.gitignore','.env\nnode_modules/\n.venv/\n__pycache__/\n')
        sh(repo,'add','-A');sh(repo,'commit','-qm','dependency baseline')
        record=lib.load_task(task);record['initial_commit']=sh(repo,'rev-parse','HEAD')
        lib.save_json(task/'task.json',record)
        return task,repo

    def test_same_baseline_installs_once_and_copies_isolated_ignored_trees(self):
        task,repo=self.baseline()
        installs=[]
        def steps(image,clone,commands,**kwargs):
            self.assertFalse(kwargs['_seed'])
            if commands[0]['name']=='setup':
                installs.append(clone)
                write(clone/'node_modules/pkg/index.js','original')
            else:
                self.assertEqual((clone/'node_modules/pkg/index.js').read_text(),'original')
                self.assertEqual(commands[0]['cmd'],'true')
            return [{'rc':0},{'rc':0}]
        def run(args,**kwargs):
            if args[:3]==['docker','image','inspect']:
                return done(0,self.cfg['image_id']+'|linux|arm64')
            return REAL_RUN(args,**kwargs)
        with mock.patch.dict(os.environ,{'FBR_SCRATCH':str(self.root/'_scratch')}), \
             mock.patch.object(lib,'run',side_effect=run), \
             mock.patch.object(lib,'run_steps_in_image',side_effect=steps):
            self.assertTrue(deps.seed(repo,self.cfg,task=task))
            second=lib.clean_clone(repo,sh(repo,'rev-parse','HEAD'),'second')
            self.assertTrue(deps.seed(second,self.cfg))
        self.assertEqual(len(installs),1)
        self.assertEqual(sh(repo,'status','--porcelain'),'')
        status=sh(repo,'status','--porcelain','--ignored')
        self.assertFalse(deps.unexpected_ignored(task,status))
        write(second/'node_modules/pkg/index.js','changed')
        self.assertEqual((repo/'node_modules/pkg/index.js').read_text(),'original')
        self.assertNotIn('node_modules',sh(repo,'ls-tree','-r','--name-only','HEAD'))
        self.assertEqual(deps.snapshot_problems(repo,'HEAD'),[])

    def test_same_dependency_content_is_shared_across_different_task_commits(self):
        first, first_repo = self.baseline()
        second = self.make_task(2)
        second_repo = second / 'workspace'
        write(second_repo/'package.json', '{"name":"selftest","version":"1.0.0"}')
        write(second_repo/'.gitignore', '.env\nnode_modules/\n.venv/\n__pycache__/\n')
        write(second_repo/'src/app.py', 'print("different task commit")\n')
        sh(second_repo, 'add', '-A')
        sh(second_repo, 'commit', '-qm', 'different commit same dependencies')
        record = lib.load_task(second)
        record['initial_commit'] = sh(second_repo, 'rev-parse', 'HEAD')
        lib.save_json(second/'task.json', record)
        installs = []

        def steps(image, clone, commands, **kwargs):
            if commands[0]['name'] == 'setup':
                installs.append(clone)
                write(clone/'node_modules/pkg/index.js', 'shared dependency')
            else:
                self.assertTrue((clone/'node_modules/pkg/index.js').is_file())
            return [{'rc': 0}, {'rc': 0}]

        def run(args, **kwargs):
            if args[:3] == ['docker', 'image', 'inspect']:
                return done(0, self.cfg['image_id'] + '|linux|arm64')
            return REAL_RUN(args, **kwargs)

        with mock.patch.dict(os.environ, {'FBR_SCRATCH': str(self.root/'_scratch')}), \
             mock.patch.object(lib, 'run', side_effect=run), \
             mock.patch.object(lib, 'run_steps_in_image', side_effect=steps):
            self.assertTrue(deps.seed(first_repo, self.cfg, task=first))
            self.assertTrue(deps.seed(second_repo, self.cfg, task=second))
        self.assertEqual(len(installs), 1)
        self.assertEqual((first_repo/'node_modules/pkg/index.js').read_text(), 'shared dependency')
        self.assertEqual((second_repo/'node_modules/pkg/index.js').read_text(), 'shared dependency')
        manifests = list((self.root/'_registry/dependencies').glob('*/manifest.json'))
        self.assertEqual(len(manifests), 1)

    def test_missing_ignore_and_tracked_dependencies_are_rejected(self):
        task=self.make_task();repo=task/'workspace'
        write(repo/'package.json','{}');sh(repo,'add','-A');sh(repo,'commit','-qm','package')
        with self.assertRaisesRegex(lib.FbrError,'忽略规则'):deps.roots(repo,self.cfg)
        write(repo/'.gitignore','node_modules/\n');write(repo/'node_modules/pkg/file','code')
        sh(repo,'add','-f','node_modules/pkg/file');sh(repo,'add','.gitignore');sh(repo,'commit','-qm','bad baseline')
        with self.assertRaisesRegex(lib.FbrError,'已被 Git 跟踪'):deps.roots(repo,self.cfg)

    def test_nonportable_dependency_tree_falls_back_without_seeding(self):
        task,repo=self.baseline()
        installs=[]
        def steps(image,clone,commands,**kwargs):
            if commands[0]['name']=='setup':
                installs.append(clone)
                write(clone/'node_modules/pkg/file','requires original container')
                return [{'rc':0},{'rc':0}]
            return [{'rc':0},{'rc':1}]
        def run(args,**kwargs):
            if args[:3]==['docker','image','inspect']:
                return done(0,self.cfg['image_id']+'|linux|arm64')
            return REAL_RUN(args,**kwargs)
        with mock.patch.dict(os.environ,{'FBR_SCRATCH':str(self.root/'_scratch')}), \
             mock.patch.object(lib,'run',side_effect=run), \
             mock.patch.object(lib,'run_steps_in_image',side_effect=steps):
            self.assertFalse(deps.seed(repo,self.cfg,task=task))
            self.assertFalse(deps.seed(repo,self.cfg,task=task))
        self.assertEqual(len(installs),1)
        self.assertFalse((repo/'node_modules').exists())
        self.assertFalse((task/'run/dependencies.json').exists())
        manifests=list((self.root/'_registry/dependencies').glob('*/manifest.json'))
        self.assertEqual(len(manifests),1)
        self.assertEqual(lib.load_json(manifests[0])['state'],'unavailable')

    def test_removed_dependency_in_intermediate_commit_is_still_rejected(self):
        task,repo=self.baseline();initial=sh(repo,'rev-parse','HEAD')
        write(repo/'node_modules/pkg/file','code');sh(repo,'add','-f','node_modules/pkg/file');sh(repo,'commit','-qm','bad intermediate')
        sh(repo,'rm','-qr','node_modules');sh(repo,'commit','-qm','remove dependency')
        self.assertEqual(deps.snapshot_problems(repo,'HEAD'),[])
        self.assertTrue(deps.snapshot_problems(repo,'HEAD',history_tip='HEAD',initial=initial))

    def test_all_destinations_checked_before_copy(self):
        task,repo=self.baseline();cache=self.root/'cache'
        write(cache/'trees/node_modules/p/file','data');write(cache/'trees/.venv/lib/file','data')
        write(repo/'.venv/lib/existing','keep')
        with mock.patch.object(deps,'ensure',return_value=(cache,{'paths':['node_modules','.venv']})):
            with self.assertRaisesRegex(lib.FbrError,'已有内容'):deps.seed(repo,self.cfg)
        self.assertFalse((repo/'node_modules').exists())
        self.assertEqual((repo/'.venv/lib/existing').read_text(),'keep')

    def test_batch_prewarm_finishes_before_model_dispatch_and_uses_concurrency(self):
        tasks = [self.make_task(i) for i in range(1, 4)]
        lock = threading.Lock()
        active = maximum = 0

        def seed(_repo, _cfg, task=None):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.1)
            with lock:
                active -= 1
            return True

        self.cfg['concurrency'] = 3
        with mock.patch.object(deps, 'seed', side_effect=seed):
            state = deps.prewarm_batch(self.root, tasks[0].parent.name, self.cfg, tasks)
        self.assertEqual((state['state'], state['prepared'], state['failed']), ('completed', 3, 0))
        self.assertEqual(maximum, 3)
        saved = lib.load_json(tasks[0].parent/'dependency-prewarm.json')
        self.assertEqual(saved['model_concurrency'], 3)


class CommonAndSlots(Sandbox):
    def test_common_profile_reused_when_values_omitted_and_conflicts_rejected(self):
        identity={'source_repo':'source','initial_commit':'abc','image_id':'image'}
        values={k:'verified-'+k for k in common.FIELDS}
        first,doc=common.profile(self.root,identity,values,{'kind':'offline fixture'})
        second,reused=common.profile(self.root,identity,{}, {'kind':'later fixture'})
        self.assertEqual((first,doc),(second,reused))
        with self.assertRaisesRegex(lib.FbrError,'冲突'):
            common.profile(self.root,identity,{**values,'language':'different'},{})
        other,_=common.profile(self.root,{**identity,'initial_commit':'new'},values,{})
        self.assertNotEqual(first,other)

    def test_review_cap_is_five_even_if_setting_is_higher(self):
        cfg={**self.cfg,'review_parallel':20,'_deadline':time.time()+5}
        lock=threading.Lock();active=0;maximum=0
        def work(_):
            nonlocal active,maximum
            with slots.slot(cfg):
                with lock:active+=1;maximum=max(maximum,active)
                time.sleep(.1)
                with lock:active-=1
        with ThreadPoolExecutor(8) as pool:list(pool.map(work,range(8)))
        self.assertEqual(maximum,5)
