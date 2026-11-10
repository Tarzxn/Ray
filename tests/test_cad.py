from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import artifacts
import cad_engine
import mesh_engine


class CADSchemaTests(unittest.TestCase):
    def test_sculpt_parts_survive_schema_normalization(self):
        spec = cad_engine._normalize({'shape':'sculpt','parts':[{'center':[0,0,0],'radii':[5,5,5]}]})
        self.assertIn('parts',spec)
        self.assertNotIn('children',spec)

    def test_legacy_program_does_not_skip_invalid_steps(self):
        with self.assertRaises(cad_engine.CADError):
            cad_engine._legacy_ops_to_tree([{'shape':'box'},'bad step'],cad_engine._Context())


@unittest.skipUnless(cad_engine.AVAILABLE,'manifold3d is not installed in this local test environment')
class CADIntegrationTests(unittest.TestCase):
    def test_bad_cutter_fails_whole_model(self):
        with self.assertRaises(cad_engine.CADError):
            cad_engine.build_program({'tree':{'op':'difference','children':[{'shape':'box','size':[20,20,5]},{'shape':'unknown_cutter'}]}})

    def test_custom_mesh_can_join_cad_solid(self):
        solid,_,_ = cad_engine.build_program({'tree':{'op':'union','children':[{'shape':'loft','sections':[[0,0,0,5,5],[0,0,20,4,4]]},{'shape':'box','size':[12,12,3]}]}})
        mesh = mesh_engine.Mesh(*cad_engine.mesh_arrays(solid))
        self.assertEqual(mesh.validate()['components'],1)

    def test_coat_hanger_cad_export_roundtrip(self):
        spec = {'plan':'180mm small hanger, triangular frame and open hook', 'tree':{'op':'union','children':[
            {'shape':'path','closed':True,'smooth':False,'radius':3,'points':[[-85,0,0],[0,0,43],[85,0,0]]},
            {'shape':'path','radius':3,'smooth':True,'points':[[0,0,43],[0,0,55],[-7,0,62],[-8,0,71],[0,0,77],[9,0,73],[10,0,65]]}]}}
        with tempfile.TemporaryDirectory() as temp, patch.object(cad_engine,'_render_view',return_value=Image.new('RGB',(30,30))):
            root = Path(temp)
            artifacts.produce_artifact(root,{'path':'hanger.stl','kind':'stl','content':spec})
            mesh = mesh_engine.read_stl(root/'hanger.stl')
            info = mesh.validate()
            self.assertEqual(info['components'],1)
            self.assertGreater(info['size_mm'][0],170)


if __name__ == '__main__': unittest.main()
