#!/usr/bin/env python
"""
Example of using the 'ReifySub' transformation.
"""
import os
import chisel

__dry_run__ = os.getenv('CHISEL_EXAMPLE_DRY_RUN', True)
__catalog_url__ = os.getenv('CHISEL_EXAMPLE_CATALOG_URL', 'http://localhost/ermrest/catalog/1')

catalog = chisel.connect(__catalog_url__)
print('CONNECTED')

# Create a new relation computed from the reifySubed column(s) of the source relation
dataset = catalog.s['isa'].t['dataset']  # assigning to local var just for readability
catalog.s['isa'].t['dataset_jbrowse'] = dataset.reify_sub(dataset.c['show_in_jbrowse'])
catalog.commit(dry_run=__dry_run__)
print('DONE')
