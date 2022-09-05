import unittest

class TestModuleStructure(unittest.TestCase):

    def test_module_structure(self):

        try:
            from  .. import utils
        except ImportError:
            raise Exception("Unable to import 'utils'")

        try:
            from  .. import maxfilter
        except ImportError:
            raise Exception("Unable to import 'maxfilter'")

        try:
            from  .. import preprocessing
        except ImportError:
            raise Exception("Unable to import 'preprocessing'")

        try:
            from  .. import report
        except ImportError:
            raise Exception("Unable to import 'report'")

        try:
            from  .. import rhino
        except ImportError:
            raise Exception("Unable to import 'rhino'")

        try:
            from  .. import parcellation
        except ImportError:
            raise Exception("Unable to import 'parcellation'")