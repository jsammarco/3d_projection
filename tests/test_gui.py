"""Integration checks for local uploads, real meshes, ray tracing and downloads."""
import base64
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

import numpy as np
from PIL import Image, ImageDraw
import trimesh

import gui
import generate as g


class StudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = gui.create_app(cls.temp.name)
        cls.client = cls.app.test_client()
        cls.headers = {'X-Studio-Token': cls.app.config['TOKEN']}
        cls.job = cls.build({})

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['studio'].close()
        cls.temp.cleanup()

    @classmethod
    def build(cls, settings, image=None):
        data = {'settings': json.dumps(settings)}
        if image:
            data['image'] = image
        response = cls.client.post('/api/jobs', data=data, headers=cls.headers)
        assert response.status_code == 202, response.json
        job = response.json
        deadline = time.monotonic() + 30
        while job['state'] in ('queued', 'building') and time.monotonic() < deadline:
            time.sleep(.05)
            job = cls.client.get('/api/jobs/'+job['id']).json
        assert job['state'] == 'ready', job
        return job

    def test_download_contains_seven_flat_watertight_parts_and_settings(self):
        scene = self.job['scene']
        self.assertEqual(len(scene['parts']), 7)
        self.assertTrue(scene['watertight'])
        result = self.client.get(scene['download_url'])
        self.assertEqual(result.status_code, 200)
        self.assertIn('fish-print-parts.zip', result.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
            self.assertEqual(len(archive.namelist()), 9)
            manifest = json.loads(archive.read('project.json'))
            self.assertEqual(manifest['settings']['emitter_diameter'], 5)
            for part in scene['parts']:
                mesh = trimesh.load(io.BytesIO(archive.read(part['file'])), file_type='stl')
                self.assertTrue(mesh.is_watertight)
                self.assertAlmostEqual(mesh.bounds[0, 2], 0, places=5)
                self.assertEqual(len(mesh.split()), 1)
                if part['name'].startswith('wall_'):
                    self.assertAlmostEqual(mesh.extents[2], 2, places=5)
                mesh.apply_transform(part['assembly_matrix'])
                self.assertGreaterEqual(mesh.bounds[0, 2], -1e-5)
        result.close()

    def test_uploaded_image_and_changed_dimensions_drive_output(self):
        image = Image.new('RGBA', (360, 240), (255, 255, 255, 0))
        ImageDraw.Draw(image).polygon([(10, 30), (210, 50), (310, 210), (90, 130)], fill='black')
        data = io.BytesIO()
        image.save(data, format='PNG'); data.seek(0)
        job = self.build({'size': 140, 'projection_width': 900}, (data, 'my-art.png'))
        scene = job['scene']
        self.assertEqual(scene['input_name'], 'my-art.png')
        np.testing.assert_allclose(scene['model_size'][:2], [152, 152])
        bounds = scene['projection_bounds']
        self.assertAlmostEqual(bounds[2]-bounds[0], 900)
        self.assertAlmostEqual(bounds[3]-bounds[1], 600)
        response = self.client.get(scene['image_url'])
        with Image.open(io.BytesIO(response.data)) as normalized:
            self.assertEqual(normalized.size, (360, 240))
        response.close()
        self.assertTrue(scene['watertight'])

    def test_point_source_matches_independent_manifold_segment_tests(self):
        s = g.Settings()
        art, bounds = g.image_geometry(g.ROOT/'fish.jpg', s)
        model = g.build_model(g.build_panels(art, bounds, s), s)
        job = self.app.extensions['studio'].get(self.job['id'])
        visibility = gui.trace_visibility(job['raytracer'], bounds.bounds, [0, 0, s.light_z, -s.wall_gap], width=37)
        xs = np.linspace(bounds.bounds[0], bounds.bounds[2], 37)
        ys = np.linspace(bounds.bounds[3], bounds.bounds[1], visibility.shape[0])
        for row in range(0, len(ys), 3):
            for col in range(0, 37, 3):
                blocked = bool(model.ray_cast([0, 0, s.light_z], [xs[col], ys[row], -s.wall_gap]))
                self.assertEqual(visibility[row, col], float(not blocked))

    def test_finite_source_produces_partial_shadows(self):
        response = self.client.post('/api/jobs/'+self.job['id']+'/simulate', json={'emitter_size': 1}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        raw = base64.b64decode(response.json['mask'].split(',')[1])
        pixels = np.asarray(Image.open(io.BytesIO(raw)))
        self.assertGreater(np.count_nonzero((pixels > 0) & (pixels < 255)), 100)
        self.assertEqual(pixels.shape, (640, 640))

    def test_invalid_input_and_expired_jobs_return_useful_errors(self):
        for settings in ({'threshold': 400}, {'size': float('nan')}, {'bridge_width': .1}, {'unknown': 1}):
            response = self.client.post('/api/jobs', data={'settings':json.dumps(settings)}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json['error'])
        bad = self.client.post('/api/jobs', data={'image':(io.BytesIO(b'not an image'), 'bad.png')}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post('/api/jobs').status_code, 403)
        self.assertEqual(self.client.get('/api/health', headers={'Host':'example.com'}).status_code, 403)
        self.assertEqual(self.client.get('/api/jobs/missing').status_code, 404)
        bad_size = self.client.post('/api/jobs/'+self.job['id']+'/simulate', json={'emitter_size': -1}, headers=self.headers)
        self.assertEqual(bad_size.status_code, 400)
        self.assertEqual(self.client.get('/api/health').status_code, 200)

    def test_browser_modules_have_correct_mime_type(self):
        with self.client.get('/static/app.js') as response:
            self.assertEqual(response.mimetype, 'text/javascript')
            self.assertIn(b'import', response.data)

    def test_cancelled_queued_job_has_no_download(self):
        studio = self.app.extensions['studio']
        # Hold the worker so cancellation is deterministic.
        import threading
        gate = threading.Event()
        studio.pool.submit(gate.wait, 5)
        try:
            response = self.client.post('/api/jobs', headers=self.headers)
            job_id = response.json['id']
            self.client.post('/api/jobs/'+job_id+'/cancel', headers=self.headers)
        finally:
            gate.set()
        deadline = time.monotonic()+5
        while self.client.get('/api/jobs/'+job_id).json['state'] == 'queued' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.client.get('/api/jobs/'+job_id).json['state'], 'cancelled')
        self.assertEqual(self.client.get('/api/jobs/'+job_id+'/download').status_code, 409)


if __name__ == '__main__':
    unittest.main()
