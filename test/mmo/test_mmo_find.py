"""Unit tests for MMO find operation.
"""
import os
import logging
import unittest
from deriva import chisel
from deriva.chisel import mmo
from deriva.core import DerivaServer, ErmrestCatalog, get_credential
from deriva.core.ermrest_model import Schema, Table, Column, Key, ForeignKey, tag, builtin_types

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv('DERIVA_PY_TEST_LOGLEVEL', default=logging.WARNING))
ermrest_hostname = os.getenv('DERIVA_PY_TEST_HOSTNAME')
ermrest_catalog_id = os.getenv('DERIVA_PY_TEST_CATALOG')
catalog = None

# baseline annotation doc for `dept` table
dept_annotations = {
    "tag:isrd.isi.edu,2016:visible-columns": {
        "compact": [
            ["org", "dept_RID_key"],
            "dept_no",
            "name"
        ],
        "detailed": [
            "RID",
            "RCT",
            {
                "source": "RMT",
                "markdown_name": "Last Modified Time"
            },
            "dept_no",
            "name",
            {
                "sourcekey": "head_count",
                "markdown_name": "Head Count"
            },
            {
                "display": {
                    "wait_for": [
                        "personnel"
                    ],
                    "template_engine": "handlebars",
                    "markdown_pattern": "{{#each personnel}}{{{this.values.name}}}{{#unless @last}}, {{/unless}}{{/each}}."
                },
                "markdown_name": "Personnel"
            }
        ]
    },
    "tag:isrd.isi.edu,2016:visible-foreign-keys": {
        "*": [
            [
                "org",
                "person_dept_fkey"
            ]
        ]
    },
    "tag:isrd.isi.edu,2019:source-definitions": {
        "columns": [
            "dept_no",
            "name",
            "RID"
        ],
        "sources": {
            "personnel": {
                "source": [
                    {
                        "inbound": [
                            "org",
                            "person_dept_fkey"
                        ]
                    },
                    "name"
                ]
            },
            "head_count": {
                "source": [
                    {
                        "inbound": [
                            "org",
                            "person_dept_fkey"
                        ]
                    },
                    "RID"
                ],
                "entity": False,
                "aggregate": "cnt_d"
            }
        }
    }
}

# baseline annotation doc for `person` table
person_annotations = {
    "tag:isrd.isi.edu,2016:visible-columns": {
        "compact": [
            ["org", "person_RID_key"],
            "name"
        ],
        "detailed": [
            "RID",
            "name",
            ["org", "person_dept_fkey"],
            {
                "markdown_name": "Department Name",
                "source": [
                    {
                        "outbound": [
                            "org",
                            "person_dept_fkey"
                        ]
                    },
                    "name"
                ],
                "entity": False
            },
            {
                "sourcekey": "dept_size",
                "markdown_name": "Department Size"
            }
        ]
    },
    "tag:isrd.isi.edu,2019:source-definitions": {
        "columns": [
            "RID",
            "name",
            "dept"
        ],
        "fkeys": [
            ["org", "person_dept_fkey"]
        ],
        "sources": {
            "dept_size": {
                "source": [
                    {
                        "outbound": [
                            "org",
                            "person_dept_fkey"
                        ]
                    },
                    {
                        "inbound": [
                            "org",
                            "person_dept_fkey"
                        ]
                    },
                    "RID"
                ],
                "entity": False,
                "aggregate": "cnt_d"
            }
        }
    }
}


@unittest.skipUnless(ermrest_hostname, 'ERMrest hostname not defined.')
class TestMMOFind (unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        global catalog

        # create catalog
        server = DerivaServer('https', ermrest_hostname, credentials=get_credential(ermrest_hostname))
        if ermrest_catalog_id:
            logger.debug(f'Connecting to {ermrest_hostname}/ermrest/catalog/{ermrest_catalog_id}')
            catalog = server.connect_ermrest(ermrest_catalog_id)
        else:
            catalog = server.create_ermrest_catalog()
            logger.debug(f'Created {ermrest_hostname}/ermrest/catalog/{catalog.catalog_id}')

        # get the chiseled model
        model = chisel.Model.from_catalog(catalog)

        # drop `org` schema, if exists
        try:
            model.schemas['org'].drop(cascade=True)
        except Exception as e:
            logger.debug(e)

        # create `org` schema
        model.create_schema(
            Schema.define('org')
        )

        # create `dept` table
        model.schemas['org'].create_table(
            Table.define(
                'dept',
                column_defs=[
                    Column.define('dept_no', builtin_types.int8),
                    Column.define('name', builtin_types.text)
                ],
                key_defs=[
                    Key.define(['dept_no'])
                ],
                annotations=dept_annotations
            )
        )

        # create `person` table
        model.schemas['org'].create_table(
            Table.define(
                'person',
                column_defs=[
                    Column.define('name', builtin_types.text),
                    Column.define('dept', builtin_types.int8)
                ],
                fkey_defs=[
                    ForeignKey.define(['dept'], 'org', 'dept', ['dept_no'])
                ],
                annotations=person_annotations
            )
        )

        # populate for good measure (though not necessary for current set of tests)
        pbuilder = catalog.getPathBuilder()

        pbuilder.org.dept.insert([
            {'dept_no': 1, 'name': 'Dept A'},
            {'dept_no': 2, 'name': 'Dept B'}
        ])

        pbuilder.org.person.insert([
            {'name': 'John', 'dept': 1},
            {'name': 'Helena', 'dept': 1},
            {'name': 'Ben', 'dept': 1},
            {'name': 'Sonia', 'dept': 2},
            {'name': 'Rafael', 'dept': 2},
        ])

    @classmethod
    def tearDownClass(cls):
        global catalog
        if not ermrest_catalog_id and isinstance(catalog, ErmrestCatalog) and int(catalog.catalog_id) > 10000:
            # note: ... > 10000 is simply a guard against accidentally pointing at and deleting a production catalog
            catalog.delete_ermrest_catalog(really=True)
        catalog = None

    def setUp(self):
        # reset annotations to baseline
        assert isinstance(catalog, ErmrestCatalog)
        self.model = catalog.getCatalogModel()
        self.model.schemas['org'].tables['dept'].annotations = dept_annotations
        self.model.schemas['org'].tables['person'].annotations = person_annotations

    def tearDown(self):
        pass

    def test_find_key_in_vizcols(self):
        matches = mmo.find(self.model, ["org", "dept_RID_key"])
        self.assertEqual(len(matches), 1)

    def test_find_col_in_vizcols(self):
        matches = mmo.find(self.model, ["org", "dept", "RCT"])
        self.assertEqual(len(matches), 1)

    def test_find_col_in_vizcols_pseudocol_simple(self):
        matches = mmo.find(self.model, ["org", "dept", "RMT"])
        self.assertEqual(len(matches), 1)

    def test_find_col_in_vizcols_pseudocol(self):
        matches = mmo.find(self.model, ["org", "dept", "name"])
        self.assertTrue(any([m.anchor.name == 'person' and m.tag == tag.visible_columns and isinstance(m.mapping, dict) for m in matches]))

    def test_find_col_in_sourcedefs_columns(self):
        matches = mmo.find(self.model, ["org", "person", "dept"])
        self.assertTrue(any([m.anchor.name == 'person' and m.tag == tag.source_definitions and m.mapping == 'dept' for m in matches]))

    def test_find_col_in_sourcedefs_sources(self):
        matches = mmo.find(self.model, ["org", "person", "RID"])
        self.assertTrue(any([m.tag == tag.source_definitions and m.mapping == 'dept_size' for m in matches]))

    def test_find_fkey_in_vizfkeys(self):
        fkname = ["org", "person_dept_fkey"]
        matches = mmo.find(self.model, fkname)
        self.assertTrue(any([m.tag == tag.visible_foreign_keys and m.mapping == fkname for m in matches]))

    def test_find_fkey_in_vizcols(self):
        fkname = ["org", "person_dept_fkey"]
        matches = mmo.find(self.model, fkname)
        self.assertTrue(any([m.tag == tag.visible_columns and m.mapping == fkname for m in matches]))

    def test_find_fkey_in_sourcedefs_sources(self):
        matches = mmo.find(self.model, ["org", "person_dept_fkey"])
        self.assertTrue(any([m.tag == tag.source_definitions and m.mapping == 'personnel' for m in matches]))

    def test_find_fkey_in_sourcedefs_fkeys(self):
        fkname = ["org", "person_dept_fkey"]
        matches = mmo.find(self.model, fkname)
        self.assertTrue(any([m.tag == tag.source_definitions and m.mapping == fkname for m in matches]))
