"""Tests for the Select operator."""
import logging
import os
import unittest
import chisel.operators as _op
import chisel.optimizer as _opt
from test.utils import TestHelper

logger = logging.getLogger(__name__)
if os.getenv('CHISEL_TEST_VERBOSE'):
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())


def count(iterable):
    """Simple func to count elements in an iterable"""
    return sum(1 for _ in iterable)


class TestSelect (unittest.TestCase):
    """Basic tests for Select operator."""

    _test_helper = TestHelper()
    _child = _op.JSONScan(object_payload=_test_helper.test_data)

    def test_select_eq_on_field_0(self):
        comparison = _opt.Comparison(self._test_helper.FIELDS[0], '=', 0)
        oper = _op.Select(self._child, comparison)
        self.assertDictEqual(self._child.description, oper.description, "table definition should match source")
        self.assertEqual(1, count(oper), 'incorrect number of rows returned by operator')

    def test_select_eq_on_field_1(self):
        comparison = _opt.Comparison(self._test_helper.FIELDS[1], '=', self._test_helper.test_data[0][self._test_helper.FIELDS[1]])
        oper = _op.Select(self._child, comparison)
        self.assertDictEqual(self._child.description, oper.description, "table definition should match source")
        self.assertLess(1, count(oper), 'incorrect number of rows returned by operator')

    def test_select_conjunction(self):
        comparisons = [
            _opt.Comparison(self._test_helper.FIELDS[0], '=', 0),
            _opt.Comparison(self._test_helper.FIELDS[1], '=', self._test_helper.test_data[0][self._test_helper.FIELDS[1]])
            ]
        comparison = _opt.Conjunction(comparisons)
        oper = _op.Select(self._child, comparison)
        self.assertDictEqual(self._child.description, oper.description, "table definition should match source")
        self.assertEqual(1, count(oper), 'incorrect number of rows returned by operator')


if __name__ == '__main__':
    unittest.main()