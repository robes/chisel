"""A few very basic tests.
"""
import unittest
import chisel
from test.helpers import CatalogHelper, BaseTestCase


class TestBaseCatalog (BaseTestCase):
    """Unit test suite for base catalog functionality.
    """

    output_basename = __name__ + '.output.csv'
    catalog_helper = CatalogHelper(table_names=[output_basename])

    def test_evolve_ctx_rollback(self):
        val = 'foo'
        with self._model.begin() as sess:
            sess.rollback()
            val = 'bar'
        self.assertEqual(val, 'foo', "catalog model evolve session did not exit on rollback")

    def test_catalog_describe(self):
        chisel.describe(self._model)

    def test_schema_describe(self):
        chisel.describe(self._model.schemas['.'])

    def test_table_describe(self):
        chisel.describe(self._model.schemas['.'].tables[self.catalog_helper.samples])

    def test_catalog_graph(self):
        chisel.graph(self._model)

    def test_schema_graph(self):
        chisel.graph(self._model.schemas['.'])

    def test_table_graph(self):
        chisel.graph(self._model.schemas['.'].tables[self.catalog_helper.samples])
