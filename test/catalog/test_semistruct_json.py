from .utils import CatalogHelper, BaseTestCase


class TestSemistructuredJson (BaseTestCase):
    """Units test suite for JSON-based semistructured catalog functionality.
    """

    output_basename = __name__ + '.output.json'
    catalog_helper = CatalogHelper(table_names=[output_basename], file_format=CatalogHelper.JSON)

    def test_catalog_from_json(self):
        self.assertIsNotNone(self._catalog)
        self.assertEqual(len(self._catalog.schemas), 1)

    def test_computed_relation_from_json(self):
        domain = self._catalog.s['.'].t[self.catalog_helper.samples].c['species'].to_domain()
        self.assertIsNotNone(domain)

    def test_materialize_to_json(self):
        domain = self._catalog.s['.'].t[self.catalog_helper.samples].c['species'].to_domain(similarity_fn=None)
        self._catalog.s['.'].t[self.output_basename] = domain
        self._catalog.commit()
        self.assertTrue(self.catalog_helper.exists(self.output_basename))

    def test_do_not_clobber(self):
        # This is actually a general test of the 'do not clobber' feature built into the catalog
        def clobbers():
            domain = self._catalog.s['.'].t[self.catalog_helper.samples].c['species'].to_domain(similarity_fn=None)
            self._catalog.s['.'].t[self.catalog_helper.samples] = domain
        self.assertRaises(ValueError, clobbers)
