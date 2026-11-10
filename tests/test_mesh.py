import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

import mesh_engine as me


class MeshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def sphere(self):
        return me.sculpt({'parts':[{'center':[0,0,0], 'radii':[10,10,10]}], 'resolution':40})

    def test_ellipsoid_volume_and_winding(self):
        info = self.sphere().validate()
        self.assertAlmostEqual(info['volume_mm3']/(4*np.pi*1000/3),1,delta=.015)
        self.assertTrue(info['watertight'])
        self.assertEqual(info['components'],1)

    def test_loft_matches_cylinder_volume(self):
        mesh = me.loft({'sections':[[0,0,0,10,10],[0,0,20,10,10]], 'segments':64})
        self.assertAlmostEqual(mesh.validate()['volume_mm3']/(np.pi*100*20),1,delta=.005)

    def test_loft_rejects_reversed_sections(self):
        with self.assertRaises(ValueError):
            me.loft({'sections':[[0,0,10,2,2],[0,0,0,2,2]]})

    def test_disconnected_parts_are_rejected(self):
        mesh = me.sculpt({'parts':[{'center':[0,0,0],'radii':[3,3,3]}, {'center':[15,0,0],'radii':[3,3,3]}], 'resolution':48})
        with self.assertRaisesRegex(ValueError,'disconnected'):
            mesh.validate()
        self.assertEqual(mesh.validate(connected=False)['components'],2)

    def test_tapered_capsule_is_closed(self):
        mesh = me.sculpt({'parts':[{'shape':'capsule','a':[0,0,0],'b':[0,0,30],'radius':5,'radius_end':2}], 'resolution':48})
        self.assertEqual(mesh.validate()['components'],1)

    def test_subtractive_through_hole(self):
        mesh = me.sculpt({'resolution':48,'parts':[{'center':[0,0,0],'radii':[10,10,5]}, {'shape':'capsule','a':[0,0,-10],'b':[0,0,10],'radius':3,'op':'subtract'}]})
        info = mesh.validate()
        self.assertLess(info['volume_mm3'],4*np.pi*10*10*5/3)
        edges = len(np.unique(np.sort(np.concatenate([mesh.faces[:,[0,1]],mesh.faces[:,[1,2]],mesh.faces[:,[2,0]]]),axis=1),axis=0))
        self.assertEqual(len(mesh.vertices)-edges+len(mesh.faces),0)  # genus 1

    def test_human_exports_survive_float32_roundtrip(self):
        mesh = me.human({'resolution':144}).grounded()
        info = mesh.validate()
        self.assertGreater(info['size_mm'][2],175)
        self.assertLess(info['triangles'],me.MAX_FACES)
        me.write_stl(self.root/'human.stl',mesh)
        me.write_glb(self.root/'human.glb',mesh)
        for reader, name in ((me.read_stl,'human.stl'),(me.read_glb,'human.glb')):
            checked = reader(self.root/name).validate()
            np.testing.assert_allclose(checked['size_mm'],info['size_mm'],rtol=1e-5,atol=1e-4)
            self.assertEqual(checked['triangles'],info['triangles'])

    def test_glb_has_metres_y_up_and_normals(self):
        mesh = self.sphere().grounded()
        path = self.root/'sphere.glb'; me.write_glb(path,mesh)
        import struct
        data = path.read_bytes(); size = struct.unpack_from('<I',data,12)[0]
        doc = json.loads(data[20:20+size])
        self.assertEqual(doc['asset']['version'],'2.0')
        self.assertEqual(doc['extras']['exportUnits'],'m')
        self.assertIn('NORMAL',doc['meshes'][0]['primitives'][0]['attributes'])
        self.assertLess(max(doc['accessors'][0]['max']),.021)

    def test_truncated_exports_are_rejected(self):
        mesh = self.sphere()
        for writer,reader,suffix in ((me.write_stl,me.read_stl,'stl'),(me.write_glb,me.read_glb,'glb')):
            path = self.root/('file.'+suffix); writer(path,mesh)
            path.write_bytes(path.read_bytes()[:-5])
            with self.assertRaises(ValueError): reader(path)

    def test_duplicate_triangle_is_rejected(self):
        mesh = self.sphere()
        mesh.faces = np.concatenate([mesh.faces,mesh.faces[:1]])
        with self.assertRaisesRegex(ValueError,'duplicate'):
            mesh.validate()

    def test_open_mesh_is_rejected(self):
        mesh = self.sphere(); mesh.faces = mesh.faces[:-1]
        with self.assertRaisesRegex(ValueError,'two-manifold'):
            mesh.validate()

    def test_inconsistent_winding_is_rejected(self):
        mesh = self.sphere(); mesh.faces[0] = mesh.faces[0,[0,2,1]]
        with self.assertRaisesRegex(ValueError,'winding'):
            mesh.validate()

    def test_nan_vertex_is_rejected(self):
        mesh = self.sphere(); mesh.vertices[0,0] = np.nan
        with self.assertRaises(ValueError): mesh.validate()

    def test_fractional_face_index_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'integers'):
            me.indexed({'vertices':[[0,0,0]]*4,'faces':[[0,1,2.5]]*4})


if __name__ == '__main__':
    unittest.main()
