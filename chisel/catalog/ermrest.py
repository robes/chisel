"""Catalog model for remote ERMrest catalog services."""

import logging
from deriva import core as _deriva_core
from deriva.core import ermrest_model as _em
from deriva.utils.catalog.components import deriva_model as _dm
from .. import optimizer
from .. import operators
from .. import util
from . import base

logger = logging.getLogger(__name__)


def connect(url, credentials=None, use_deriva_catalog_manage=False):
    """Connect to an ERMrest data source.

    :param url: connection string url
    :param credentials: user credentials
    :param use_deriva_catalog_manage: flag to use deriva catalog manage implementation rather than deriva core
    :return: catalog for data source
    """
    parsed_url = util.urlparse(url)
    if not credentials:
        credentials = _deriva_core.get_credential(parsed_url.netloc)
    ec = _deriva_core.ErmrestCatalog(parsed_url.scheme, parsed_url.netloc, parsed_url.path.split('/')[-1], credentials)
    if use_deriva_catalog_manage:
        return DerivaCatalog(ec)
    else:
        return ERMrestCatalog(ec)


class ERMrestCatalog (base.AbstractCatalog):
    """Database catalog backed by a remote ERMrest catalog service."""

    """instance wide setting for providing system columns when creating new tables (default: True)"""
    provide_system = True

    """The set of system columns."""
    syscols = {'RID', 'RCB', 'RMB', 'RCT', 'RMT'}

    def __init__(self, ermrest_catalog):
        super(ERMrestCatalog, self).__init__(ermrest_catalog.getCatalogSchema())
        self.ermrest_catalog = ermrest_catalog

    def _new_schema_instance(self, schema_doc):
        return ERMrestSchema(schema_doc, self)

    def _materialize_relation(self, plan):
        """Materializes a relation from a physical plan.

        :param plan: a `PhysicalOperator` instance from which to materialize the relation
        :return: None
        """
        if isinstance(plan, operators.Alter):
            logger.debug("Altering table '{tname}'.".format(tname=plan.description['table_name']))
            if not self._evolve_ctx.allow_alter:
                raise base.CatalogMutationError('"allow_alter" flag is not True')

            altered_schema_name, altered_table_name = plan.description['schema_name'], plan.description['table_name']
            self._do_alter_table(altered_schema_name, altered_table_name, plan.projection)

            # Note: repair the model following the alter table
            #  invalidate the altered table model object
            schema = self.schemas[altered_schema_name]
            invalidated_table = self[altered_schema_name].tables._backup[altered_table_name]  # get the original table
            invalidated_table.valid = False
            #  introspect the schema on the revised table
            model_doc = self.ermrest_catalog.getCatalogSchema()
            table_doc = model_doc['schemas'][altered_schema_name]['tables'][altered_table_name]
            table = ERMrestTable(table_doc, schema=schema)
            schema.tables._backup[altered_table_name] = table  # TODO: this part is kludgy and needs to be revised
            # TODO: refresh the referenced_by of the catalog

        elif isinstance(plan, operators.Drop):
            logger.debug("Dropping table '{tname}'.".format(tname=plan.description['table_name']))
            if not self._evolve_ctx.allow_drop:
                raise base.CatalogMutationError('"allow_drop" flag is not True')

            dropped_schema_name, dropped_table_name = plan.description['schema_name'], plan.description['table_name']
            self._do_drop_table(dropped_schema_name, dropped_table_name)

            # Note: repair the model following the drop table
            #  invalidate the dropped table model object
            schema = self.schemas[dropped_schema_name]
            dropped_table = schema.tables[dropped_table_name]
            dropped_table.valid = False
            #  remove dropped table model object from schema
            del schema.tables._backup[dropped_table_name]  # TODO: this part is kludgy and needs to be revised
            # TODO: refresh the referenced_by of the catalog

        elif isinstance(plan, operators.Assign):
            logger.debug("Creating table '{tname}'.".format(tname=plan.description['table_name']))
            assigned_schema_name, assigned_table_name = plan.description['schema_name'], plan.description['table_name']

            # Redefine table from plan description (allows us to provide system columns)
            desc = plan.description
            table_doc = _em.Table.define(
                desc['table_name'],
                column_defs=desc['column_definitions'],
                key_defs=desc['keys'],
                fkey_defs=desc['foreign_keys'],
                comment=desc['comment'],
                acls=desc.get('acls', {}),
                acl_bindings=desc.get('acl_bindings', {}),
                annotations=desc.get('annotations', {}),
                provide_system=ERMrestCatalog.provide_system
            )

            # Create table
            self._do_create_table(plan.description['schema_name'], table_doc)

            # Insert tuples in new table
            paths = self.ermrest_catalog.getPathBuilder()
            new_table = paths.schemas[assigned_schema_name].tables[assigned_table_name]
            # ...Determine the nondefaults for the insert
            new_table_cnames = set([col['name'] for col in desc['column_definitions']])
            nondefaults = {'RID', 'RCB', 'RCT'} & new_table_cnames
            new_table.insert(plan, nondefaults=nondefaults)

            # Repair catalog model
            #  ...introspect the schema on the revised table
            model_doc = self.ermrest_catalog.getCatalogSchema()
            table_doc = model_doc['schemas'][assigned_schema_name]['tables'][assigned_table_name]
            schema = self.schemas[assigned_schema_name]
            table = schema._new_table_instance(table_doc)
            schema.tables._backup[assigned_table_name] = table  # TODO: this part is kludgy and needs to be revised
            # TODO: refresh the referenced_by of the catalog

        else:
            raise ValueError('Plan cannot be materialized.')

    def _do_create_table(self, schema_name, table_doc):
        """Create table in the catalog."""
        schema = self.ermrest_catalog.getCatalogModel().schemas[schema_name]
        schema.create_table(self.ermrest_catalog, table_doc)

    def _do_copy_table(self, src_schema_name, src_table_name, dst_schema_name, dst_table_name):
        """Copy table in the catalog."""
        src_schema = self.schemas[src_schema_name]
        dst_schema = self.schemas[dst_schema_name]
        src_table = src_schema.tables[src_table_name]
        dst_schema.tables[dst_table_name] = src_table.select()  # requires that this be performed w/in evolve block

    def _do_rename_table(self, schema_name, table_name, new_name):
        """Rename table in the catalog."""
        schema = self.schemas[schema_name]
        table = schema.tables[table_name]
        with self.evolve():  # TODO: should refactor this so that it doesn't have to be performed in a evolve block
            schema.tables[new_name] = table.select()
        del schema.tables[table_name]

    def _do_alter_table_add_column(self, schema_name, table_name, column_doc):
        """Alter table Add column in the catalog"""
        model = self.ermrest_catalog.getCatalogModel()
        ermrest_table = model.schemas[schema_name].tables[table_name]
        ermrest_table.create_column(self.ermrest_catalog, column_doc)

    def _do_alter_table(self, schema_name, table_name, projection):
        """Alter table (general) in the catalog."""
        model = self.ermrest_catalog.getCatalogModel()
        schema = model.schemas[schema_name]
        table = schema.tables[table_name]
        original_columns = {c.name: c for c in table.column_definitions}

        # Notes: currently, there are two distinct scenarios in a projection,
        #  1) 'general' case: the projection is an improper subset of the relation's columns, and may include some
        #     aliased columns from the original columns. Also, columns may be aliased more than once.
        #  2) 'special' case for deletes only: as a syntactic sugar, many formulations of project support the
        #     notation of "-foo,-bar,..." meaning that the operator will project all _except_ those '-name' columns.
        #     We support that by first including the special symbol 'AllAttributes' followed by 'AttributeRemoval'
        #     symbols.

        if projection[0] == optimizer.AllAttributes():  # 'special' case for deletes only
            logger.debug("Dropping columns that were explicitly removed.")
            for removal in projection[1:]:
                assert isinstance(removal, optimizer.AttributeRemoval)
                logger.debug("Deleting column '{cname}'.".format(cname=removal.name))
                original_columns[removal.name].delete(self.ermrest_catalog)

        else:  # 'general' case

            # step 1: copy aliased columns, and record nonaliased column names
            logger.debug("Copying 'aliased' columns in the projection")
            nonaliased_column_names = set()
            for projected in projection:
                if isinstance(projected, optimizer.AttributeAlias):
                    original_column = original_columns[projected.name]
                    # 1.a: clone the column
                    cloned_def = original_column.prejson()
                    cloned_def['name'] = projected.alias
                    table.create_column(self.ermrest_catalog, cloned_def)
                    # 1.b: get the datapath table for column
                    pb = self.ermrest_catalog.getPathBuilder()
                    dp_table = pb.schemas[table.sname].tables[table.name]
                    # 1.c: read the RID,column values
                    data = dp_table.attributes(
                        dp_table.column_definitions['RID'],
                        **{projected.alias: dp_table.column_definitions[projected.name]}
                    )
                    # 1.d: write the RID,alias values
                    dp_table.update(data)
                else:
                    assert isinstance(projected, str)
                    nonaliased_column_names.add(projected)

            # step 2: remove columns that were not projected
            logger.debug("Dropping columns not in the projection.")
            for column in original_columns.values():
                if column.name not in nonaliased_column_names | self.syscols:
                    logger.debug("Deleting column '{cname}'.".format(cname=column.name))
                    column.delete(self.ermrest_catalog)

    def _do_drop_table(self, schema_name, table_name):
        """Drop table in the catalog."""
        # Delete table from the ermrest catalog
        model = self.ermrest_catalog.getCatalogModel()
        schema = model.schemas[schema_name]
        table = schema.tables[table_name]
        table.delete(self.ermrest_catalog)

    def _determine_model_changes(self, computed_relation):
        """Determines the model changes to be produced by this computed relation."""
        return dict(mappings=[], constraints=[], policies=[])

    def _relax_model_constraints(self, model_changes):
        """Relaxes model constraints in the prior conditions of the model changes."""
        pass

    def _apply_model_changes(self, model_changes):
        """Apply model changes in the post conditions of the model changes."""
        pass


class ERMrestSchema (base.Schema):
    """Represents a 'schema' (a.k.a., a namespace) in a database catalog."""

    def _new_table_instance(self, table_doc):
        return ERMrestTable(table_doc, self)

    @base.valid_model_object
    def _create_table(self, table_doc):
        """ERMrest specific implementation of create table function."""

        # Revise table doc to include sys columns, per static flag
        table_doc_w_syscols = _em.Table.define(
            table_doc['table_name'],
            column_defs=table_doc.get('column_definitions', []),
            key_defs=table_doc.get('keys', []),
            fkey_defs=table_doc.get('foreign_keys', []),
            comment=table_doc.get('comment', None),
            acls=table_doc.get('acls', {}),
            acl_bindings=table_doc.get('acl_bindings', {}),
            annotations=table_doc.get('annotations', {}),
            provide_system=ERMrestCatalog.provide_system
        )

        # Create the table using evolve block for isolation
        with self.catalog.evolve():
            self.catalog._do_create_table(self.name, table_doc_w_syscols)
            return self._new_table_instance(table_doc_w_syscols)


class ERMrestTable (base.Table):
    """Extant table in an ERMrest catalog."""

    @base.valid_model_object
    def _add_column(self, column_doc):
        """ERMrest specific implementation of add column function."""
        with self.schema.catalog.evolve():
            self.schema.catalog._do_alter_table_add_column(self.schema.name, self.name, column_doc)
            return self._new_column_instance(column_doc)

    @property
    def logical_plan(self):
        """The logical plan used to compute this relation; intended for internal use."""
        return optimizer.ERMrestExtant(self.schema.catalog, self.schema.name, self.name)

    @base.valid_model_object
    def _rename(self, new_name):
        """Internal implementation method for renaming the table."""
        # TODO: do rename table should be refactored so that this can be done in an evolve block
        self.schema.catalog._do_rename_table(self.schema.name, self.name, new_name)
        # repair local model state
        self.valid = False
        if self.name in self.schema.tables._backup:
            del self.schema.tables._backup[self.name]
            self.schema.tables.reset()

    @base.valid_model_object
    def copy(self, table_name, schema_name=None):
        """ERMrest catalog specific implementation of 'copy' method."""
        with self.schema.catalog.evolve():
            self.schema.catalog._do_copy_table(self.schema.name, self.name, schema_name or self.schema.name, table_name)


class DerivaCatalog (ERMrestCatalog):
    """ERMrest catalog with implementation using the deriva-catalog-manage package."""

    def _do_create_table(self, schema_name, table_doc):
        deriva_catalog = _dm.DerivaCatalog(None, None, None, ermrest_catalog=self.ermrest_catalog, validate=False)
        deriva_schema = deriva_catalog.schema(schema_name)

        # TODO: convert definition; this is just enough to pass a basic test
        deriva_schema.create_table(
            table_doc['table_name'],
            column_defs=[_dm.DerivaColumn.define(col['name'], col['type']['typename'], nullok=col['nullok']) for col in table_doc['column_definitions']],
            key_defs=[],  # TODO: convert -> table_doc['keys'],
            fkey_defs=[],  # TODO: convert -> table_doc['foreign_keys'],
            comment=table_doc['comment'],
            acls=table_doc.get('acls', {}),
            acl_bindings=table_doc.get('acl_bindings', {}),
            annotations=table_doc.get('annotations', {})
        )

    def _do_copy_table(self, src_schema_name, src_table_name, dst_schema_name, dst_table_name):
        deriva_catalog = _dm.DerivaCatalog(None, None, None, ermrest_catalog=self.ermrest_catalog, validate=False)
        deriva_schema = deriva_catalog.schema(src_schema_name)
        deriva_table = deriva_schema.table(src_table_name)
        deriva_table.copy_table(dst_schema_name, dst_table_name)

        #  repair local model state
        model_doc = self.ermrest_catalog.getCatalogSchema()
        table_doc = model_doc['schemas'][dst_schema_name]['tables'][dst_table_name]
        schema = self.schemas[dst_schema_name]
        table = ERMrestTable(table_doc, schema=schema)
        schema.tables._backup[dst_table_name] = table  # TODO: this part is kludgy and needs to be revised

    def _do_rename_table(self, schema_name, table_name, new_name):
        deriva_catalog = _dm.DerivaCatalog(None, None, None, ermrest_catalog=self.ermrest_catalog, validate=False)
        deriva_schema = deriva_catalog.schema(schema_name)
        deriva_table = deriva_schema.table(table_name)
        with self.evolve():  # TODO: should remove this guard when other rename function is refactored
            deriva_table.move_table(schema_name, new_name)

        #  repair local model state
        model_doc = self.ermrest_catalog.getCatalogSchema()
        table_doc = model_doc['schemas'][schema_name]['tables'][new_name]
        schema = self.schemas[schema_name]
        table = ERMrestTable(table_doc, schema=schema)
        schema.tables._backup[new_name] = table  # TODO: this part is kludgy and needs to be revised

    def _do_alter_table(self, schema_name, table_name, projection):
        """Alter table (general) in the catalog."""
        deriva_catalog = _dm.DerivaCatalog(None, None, None, ermrest_catalog=self.ermrest_catalog, validate=False)
        deriva_schema = deriva_catalog.schema(schema_name)
        deriva_table = deriva_schema.table(table_name)
        original_columns = {column.name: column for column in deriva_table.columns}

        # Notes: currently, there are two distinct scenarios in a projection,
        #  1) 'general' case: the projection is an improper subset of the relation's columns, and may include some
        #     aliased columns from the original columns. Also, columns may be aliased more than once.
        #  2) 'special' case for deletes only: as a syntactic sugar, many formulations of project support the
        #     notation of "-foo,-bar,..." meaning that the operator will project all _except_ those '-name' columns.
        #     We support that by first including the special symbol 'AllAttributes' followed by 'AttributeRemoval'
        #     symbols.

        if projection[0] == optimizer.AllAttributes():  # 'special' case for deletes only
            logger.debug("Dropping columns that were explicitly removed.")
            for removal in projection[1:]:
                assert isinstance(removal, optimizer.AttributeRemoval)
                logger.debug("Deleting column '{cname}'.".format(cname=removal.name))
                original_columns[removal.name].delete()

        else:  # 'general' case
            logger.debug("Copying 'aliased' columns in the projection")

            # step 1: get all the unaliased column names
            nonaliased_column_names = {column_name for column_name in projection if isinstance(column_name, str)}
            renamed_columns = []

            # step 2: COPY or RENAME the aliased columns in the projection
            for projected in projection:
                if isinstance(projected, optimizer.AttributeAlias):
                    original_column = original_columns[projected.name]
                    if projected.name not in nonaliased_column_names:  # RENAME
                        logger.debug('Renaming column "%s"' % original_column.name)
                        column_map = {
                            original_column: _dm.DerivaColumn(table=deriva_table, name=projected.alias,
                                                              type=original_column.type, nullok=original_column.nullok,
                                                              default=original_column.default, define=True)
                        }
                        deriva_table.rename_columns(column_map)
                        renamed_columns.append(original_column.name)
                    else:  # COPY
                        logger.debug('Copying column "%s"' % original_column.name)
                        column_map = {
                            original_column: _dm.DerivaColumn(table=deriva_table, name=projected.alias,
                                                              type=original_column.type, nullok=original_column.nullok,
                                                              default=original_column.default, define=True)
                        }
                        deriva_table.copy_columns(column_map)

            # step 3: remove columns that were not projected unless they were already renamed
            logger.debug("Dropping columns not in the projection.")
            for column in original_columns.values():
                if (column.name not in renamed_columns) and (column.name not in nonaliased_column_names | self.syscols):
                    logger.debug('Deleting column "%s"' % column.name)
                    column.delete()

    def _do_alter_table_add_column(self, schema_name, table_name, column_doc):
        """Alter table Add column in the catalog"""
        deriva_catalog = _dm.DerivaCatalog(None, None, None, ermrest_catalog=self.ermrest_catalog, validate=False)
        deriva_table = deriva_catalog.schema(schema_name).table(table_name)
        deriva_table.create_columns(  # TODO: do the conversion more completely
            _dm.DerivaColumn.define(column_doc['name'], column_doc['type']['typename'], nullok=column_doc['nullok']))

    def _do_drop_table(self, schema_name, table_name):
        deriva_catalog = _dm.DerivaCatalog(None, None, None, ermrest_catalog=self.ermrest_catalog, validate=False)
        deriva_schema = deriva_catalog.schema(schema_name)
        deriva_table = deriva_schema.table(table_name)
        deriva_table.delete()
