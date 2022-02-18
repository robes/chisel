"""Core physical operators."""

import abc
import collections
from itertools import chain
import json
import logging
from operator import itemgetter
from pprint import pprint
import uuid
from deriva.core import ermrest_model as _em
from ..optimizer import symbols

logger = logging.getLogger(__name__)

# placeholders for tname
__tname_key__ = '__computed_relation__'
__tname_placeholder__ = '{%s}' % __tname_key__

def _make_constraint_name(tname, *cnames, suffix=''):
    """Returns a constraint name from the given components."""
    constraint_name = f'{tname}_' + '_'.join(cnames)
    return constraint_name[:63 - len(suffix)] + f'_{suffix}'  # max supported length

#
# Physical operator (abstract) base class definition
#

class PhysicalOperator:
    """Abstract base class for the physical operators.

    A physical operator has two primary purposes:
      1. it should determine the table definition (i.e., relation schema) of the computed relation; and
      2. it must compute the relation using an iterator pattern, which should efficiently yield its rows.
    """
    def __init__(self):
        super(PhysicalOperator, self).__init__()

    @property
    def description(self):
        """Describes the computed relation (i.e., its relation schema).
        """
        # This method will return the instance's `_description` if it has one, if not it will return this instance's
        # `_child.description` if child is defined, else will return `None`.
        if hasattr(self, '_description'):
            return self._description
        elif hasattr(self, '_child') and hasattr(self._child, 'description'):
            return self._child.description
        else:
            return None

    @abc.abstractmethod
    def __iter__(self):
        """Returns a generator function. Must be implemented by subclasses."""

    @classmethod
    def _rename_row_attributes(cls, row, renames, always_copy=False):
        """Renames the attributes in the input `row` according to the `renames` mapping.

        :param row: an input row as a dictionary
        :param renames: a mapping of new to old attribute names in the form `{ new_name: old_name [, ...] }`
        :param always_copy: if True, always returns a copy of the input row rather than the original row, even when no columns are renamed
        :return: output row with columns renamed
        """
        if not renames:
            if always_copy:
                return row.copy()
            else:
                return row

        new_row = row.copy()
        for new_name, old_name in renames.items():
            new_row[new_name] = row[old_name]
            if old_name in new_row:
                del new_row[old_name]
        return new_row


#
# Supplementary operator definitions: buffer, metadata, temp var reference
#

class BufferedOperator (PhysicalOperator):
    """Buffers the tuples generated by an arbitrary child operator."""
    def __init__(self, child):
        super(BufferedOperator, self).__init__()
        assert isinstance(child, PhysicalOperator)
        self._child = child
        self._buffer = collections.deque()

    def __iter__(self):
        # This is not intended to be re-entrant, but could be made so if needed
        if self._buffer:
            for item in self._buffer:
                yield item
        else:
            for item in self._child:
                self._buffer.append(item)
                yield item


class Metadata (PhysicalOperator):
    """Metadata pass through operator."""
    def __init__(self, description):
        super(Metadata, self).__init__()
        self._description = description

    def __iter__(self):
        raise NotImplementedError('The "Metadata" operator cannot be iterated.')


class TempVarRef (PhysicalOperator):
    """References a temporary variable (i.e., computed relation)."""
    def __init__(self, computed_relation):
        super(TempVarRef, self).__init__()
        assert computed_relation is not None and hasattr(computed_relation, 'fetch')
        self._description = computed_relation.prejson()
        self._computed_relation = computed_relation

    def __iter__(self):
        return iter(self._computed_relation.fetch())


#
# Mutation primitive operators: assign, create, alter, drop
#

class Assign (PhysicalOperator):
    """Assign operator names the relation and passes through the child iterator."""
    def __init__(self, child, schema_name, table_name):
        super(Assign, self).__init__()
        assert isinstance(child, PhysicalOperator)
        self._child = child
        self._description = child.description.copy()
        self._description['schema_name'] = schema_name
        self._description['table_name'] = table_name
        # finalize f/key names
        for cdef in self._description['keys']:
            self._assign_constraint_name(schema_name, table_name, cdef)
        for cdef in self._description['foreign_keys']:
            self._assign_constraint_name(schema_name, table_name, cdef)

    def __iter__(self):
        return iter(self._child)

    @property
    def child(self):
        return self._child

    def _assign_constraint_name(self, schema_name, table_name, constraint_def):
        if constraint_def['names']:
            computed_name = constraint_def['names'][0]
            final_name = [schema_name, computed_name[1].replace(__tname_placeholder__, table_name)]
            constraint_def['names'] = [final_name]
            # todo: fix annotations/acls


class Create (Assign):
    """Create operator."""
    def __init__(self, child, schema_name, table_name):
        super(Create, self).__init__(child, schema_name, table_name)


class Alter (Assign):
    """Alter operator takes the 'projection' which will define the mutation on the relation."""
    def __init__(self, child, src_sname, src_tname, dst_sname, dst_tname, projection):
        super(Alter, self).__init__(child, dst_sname, dst_tname)
        self.src_sname, self.src_tname, self.dst_sname, self.dst_tname = src_sname, src_tname, dst_sname, dst_tname
        self.projection = projection


class Drop (Assign):
    """Drop operator."""
    def __init__(self, child, schema_name, table_name):
        super(Drop, self).__init__(child, schema_name, table_name)


#
# Basic primitive operators: select, project, rename, distinct, union
#


class Select (PhysicalOperator):
    """Base Select operator.
    """
    def __init__(self, child, formula):
        """Initializes the Select operator.

        :param child: input expression
        :param formula: select formula; basically a parsed where-clause
        """
        super(Select, self).__init__()
        assert isinstance(child, PhysicalOperator)
        assert isinstance(formula, symbols.Comparison) or \
               isinstance(formula, symbols.Conjunction) or isinstance(formula, symbols.Disjunction)
        logger.debug('Select "formula" => %s' % str(formula))
        self._child = child

        if isinstance(formula, symbols.Comparison):
            self._comparisons = [formula]
        else:
            self._comparisons = formula.comparisons

        if isinstance(formula, symbols.Disjunction):
            self._connector_fn = any
        else:
            self._connector_fn = all

    def __iter__(self):
        return filter(self._eval_formula, self._child)

    def _eval_comparison(self, row: dict, comparison: symbols.Comparison):
        """Evaluates a single comparison.
        """
        assert isinstance(comparison, symbols.Comparison)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('eval: %s %s %s' % (row[comparison.operand1], comparison.operator, comparison.operand2))
        return getattr(row[comparison.operand1], '__%s__' % comparison.operator)(comparison.operand2)

    def _eval_formula(self, row):
        """Evalutates the current row against the select operator's formula.
        """
        if not self._comparisons:  # if no comparisons than return tuple by default
            return True
        # otherwise, test that all comparisons match
        return self._connector_fn([self._eval_comparison(row, comparison) for comparison in self._comparisons])


class Project (PhysicalOperator):
    """Basic projection operator."""
    def __init__(self, child, projection):
        super(Project, self).__init__()
        projection = projection or [symbols.AllAttributes()]
        assert hasattr(projection, '__iter__'), "Projection is not an iterable collection"
        assert isinstance(child, PhysicalOperator)
        self._child = child
        self._attributes = set()
        self._alias_to_cname = {}
        self._cname_to_alias = collections.defaultdict(list)
        removals = set()
        additions = []
        key_renamed = {}
        key_dropped = []
        fkey_renamed = {}
        fkey_dropped = []
        fkey_defs = []

        # Redefine the description of the child operator based on the projection
        table_def = self.description
        logger.debug("projecting from child relation: %s", table_def)

        # Attributes may contain an introspection function. If so, call it on the table model object, and combine its
        # results with the rest of the given attributes list.
        for item in projection:
            if isinstance(item, symbols.AllAttributes):
                logger.debug("projecting all attributes")
                self._attributes |= {col_def['name'] for col_def in table_def['column_definitions']}
            elif isinstance(item, str):
                logger.debug("projecting attribute by name: %s", item)
                self._attributes.add(item)
            elif isinstance(item, symbols.IntrospectionFunction):
                logger.debug("projecting attributes returned by an introspection function: %s", item)
                # don't introspect on a comp rel
                if table_def['table_name'] == __tname_placeholder__:
                    raise ValueError('This operation is not supported on a computed relation')
                # introspect attributes, handle special case for 'RID'
                attrs = item.fn(table_def)
                pk_cols = attrs.copy()
                fk_cols = attrs.copy()
                if 'RID' in attrs:
                    attrs.remove('RID')
                    renamed_rid = table_def['table_name'] + '_RID'
                    self._alias_to_cname[renamed_rid] = 'RID'
                    self._cname_to_alias['RID'].append(renamed_rid)
                    fk_cols = [renamed_rid if c == 'RID' else c for c in fk_cols]
                # add to projected attributes
                self._attributes |= set(attrs)
                # add fkey to the source relation
                fkey_defs.append(
                    _em.ForeignKey.define(
                        fk_cols,
                        table_def['schema_name'],  # pk sname
                        table_def['table_name'],   # pk tname
                        pk_cols,                   # pk columns
                        on_update='CASCADE',
                        constraint_names=[[table_def['schema_name'], _make_constraint_name(__tname_placeholder__, *fk_cols, suffix='fkey')]]
                    )
                )
            elif isinstance(item, symbols.AttributeAlias):
                logger.debug("projecting an aliased attribute: %s", item)
                self._alias_to_cname[item.alias] = item.name
                self._cname_to_alias[item.name].append(item.alias)
            elif isinstance(item, symbols.AttributeDrop):
                logger.debug("projection with attribute drop: %s", item)
                removals.add(item.name)
            elif isinstance(item, symbols.AttributeAdd):
                logger.debug("projection with attribute add: %s", item)
                additions.append(json.loads(item.definition))
            else:
                raise ValueError("Unsupported projection type '{}'.".format(type(item).__name__))

        logger.debug("alias to cnames: %s", self._alias_to_cname)
        logger.debug("cname to aliases: %s", self._cname_to_alias)

        # Create a new table definition based on the appropriate projection of columns and their types.
        projected_attrs = set()
        col_defs = []
        for col_def in table_def['column_definitions']:
            cname = col_def['name']
            if cname in self._attributes and cname not in removals and cname not in self._cname_to_alias:
                col_defs.append(col_def)
                projected_attrs.add(cname)
            elif cname in self._cname_to_alias:
                for alias in self._cname_to_alias[cname]:
                    # copy and rename column def
                    col_def = col_def.copy()
                    # if projecting a RID as a new column, fix its default and type
                    if col_def['name'] == 'RID':
                        col_def['type'] = _em.builtin_types.text.prejson()
                        col_def['default'] = None
                    col_def['name'] = alias
                    col_defs.append(col_def)
        col_defs.extend(additions)

        # Updated projection of attributes
        self._attributes = projected_attrs
        # set of all projected attributes, including those that will be renamed
        # will be used in the next steps to determine which keys and fkeys can be preserved
        all_projected_attributes = self._attributes | self._cname_to_alias.keys()

        # copy all key definitions for which all key columns exist in this projection
        key_defs = []
        for key_def in table_def['keys']:
            unique_columns = key_def['unique_columns']
            # include key if all unique columns are in the projection
            if set(unique_columns) <= all_projected_attributes:
                key_def = key_def.copy()
                key_def['unique_columns'] = [self._cname_to_alias.get(cname, [cname])[0] for cname in unique_columns]
                # generate new name, remember old name(s)
                old_names = key_def['names']
                new_name = [table_def['schema_name'], _make_constraint_name(__tname_placeholder__, *key_def['unique_columns'], suffix='key')]
                key_def['names'] = [new_name]
                key_defs.append(key_def)
                # record key new-old name, for renaming in annotations
                key_renamed[tuple(new_name)] = old_names
            else:
                # record key name, for pruning from annotations
                key_dropped.append(key_def['names'])

        # copy all fkey definitions for which all fkey columns exist in this projection
        for fkey_def in table_def['foreign_keys']:
            foreign_key_columns = [fkey_col['column_name'] for fkey_col in fkey_def['foreign_key_columns']]
            # include fkey if all fkey columns are in the projection
            if set(foreign_key_columns) <= all_projected_attributes:
                fkey_def = fkey_def.copy()
                revised_fkcols = [self._cname_to_alias.get(cname, [cname])[0] for cname in foreign_key_columns]
                fkey_def['foreign_key_columns'] = [
                    {'column_name': cname} for cname in revised_fkcols
                ]
                # generate new name, remember old name(s)
                old_names = fkey_def['names']
                new_name = [table_def['schema_name'], _make_constraint_name(__tname_placeholder__, *revised_fkcols, suffix='fkey')]
                fkey_def['names'] = [new_name]
                fkey_defs.append(fkey_def)
                # record fkey new-old name, for renaming in annotations
                fkey_renamed[tuple(new_name)] = old_names
            else:
                # record fkey name, for pruning from annotations
                fkey_dropped.append(fkey_def['names'])

        # print('KEY ADDED', key_def)
        # print('RENAMED', key_renamed)
        # print('DROPPED', key_dropped)
        # print('FKEY ADDED', fkey_defs)
        # print('RENAMED', fkey_renamed)
        # print('DROPPED', fkey_dropped)

        # todo: swap/prune (f)keys from annotations and acls documents

        # Define the table
        self._description = _em.Table.define(
            __tname_placeholder__,
            column_defs=col_defs,
            key_defs=key_defs,
            fkey_defs=fkey_defs,
            comment=table_def.get('comment', ''),
            acls=table_def.get('acls', {}),
            acl_bindings=table_def.get('acl_bindings', {}),  # TODO: Filter these and handle renames
            annotations=table_def.get('annotations', {}),  # TODO: Filter these and handle renames
            provide_system=False
        )

    def __iter__(self):
        original_attributes = self._attributes | self._cname_to_alias.keys()
        getter = itemgetter(*original_attributes)
        for row in self._child:
            values = getter(row)
            values = values if isinstance(values, tuple) else (values,)
            assert len(original_attributes) == len(values)
            row = dict(zip(original_attributes, values))
            yield self._rename_row_attributes(row, self._alias_to_cname)


class Rename (Project):
    def __init__(self, child, renames):
        assert isinstance(child, PhysicalOperator)
        assert child.description
        assert isinstance(renames, tuple)
        assert all([isinstance(rename, symbols.AttributeAlias) for rename in renames])

        # compile list of renames, and handle >1 alias per original column
        rename_dict = collections.defaultdict(list)
        for rename in renames:
            rename_dict[rename.name].append(rename)

        # create a projection from original column definitions modulo the renamed columns
        projection = []
        for col in child.description['column_definitions']:
            if col['name'] in rename_dict:
                projection.extend(rename_dict[col['name']])
            else:
                projection.append(col['name'])

        # let the Project operator do the rest
        super(Rename, self).__init__(child, projection)


class HashDistinct (PhysicalOperator):
    """Distinct operator using in-memory hash data structure."""
    def __init__(self, child, attributes):
        super(HashDistinct, self).__init__()
        assert isinstance(child, PhysicalOperator)
        self._child = child
        self._distinct_on = attributes

    def __iter__(self):
        getter = itemgetter(*self._distinct_on)
        tuples = set()
        for row in self._child:
            tuple_ = getter(row)
            if tuple_ not in tuples:
                tuples.add(tuple_)
                yield row


class Union (PhysicalOperator):
    """Basic select operator."""
    def __init__(self, child, other):
        super(Union, self).__init__()
        assert isinstance(child, PhysicalOperator)
        assert isinstance(other, PhysicalOperator)
        # TODO: validate that 'other' has same columns as 'child'
        self._child = child
        self._other = other

    def __iter__(self):
        return chain(self._child, self._other)


#
# Nest and unnest operators
#

class NestedLoopsSimilarityAggregation (PhysicalOperator):
    """Nested loops similarity aggregation operator implementation."""
    def __init__(self, child, grouping, nesting, similarity_fn, grouping_fn):
        super(NestedLoopsSimilarityAggregation, self).__init__()
        # TODO: support 1+ grouping keys
        assert len(grouping) == 1, 'must specify one grouping attribute'
        assert len(nesting) <= 1, 'only 1 nesting attribute allowed'
        assert isinstance(child, PhysicalOperator)
        self._child = child
        self._grouping = grouping
        self._nesting = nesting
        self._similarity_fn = similarity_fn
        self._grouping_fn = grouping_fn
        col_defs = [
            col for col in child.description['column_definitions'] if col['name'] in self._grouping
        ] + [
            _em.Column.define(
                col['name'],
                _em.builtin_types[col['type']['typename'] + '[]'],
                comment=col['comment'],
                acls=col['acls'] if 'acls' in col else {},
                acl_bindings=col['acl_bindings'] if 'acl_bindings' in col else {},
                annotations=col['annotations'] if 'annotations' in col else {}
            ) for col in self.description['column_definitions'] if col['name'] in self._nesting
        ]
        self._description = _em.Table.define(
            child.description['table_name'] + ':' + uuid.uuid1().hex,  # todo: placeholder?
            column_defs=col_defs,
            provide_system=False
        )

    def __iter__(self):
        # TODO: revisit the complexity of this algorithm... O(N) + O(N^2) + O(M)
        #       where N is the number of rows, and M is the number of groups

        # item getters
        key_getter = itemgetter(*self._grouping)
        nested_getter = itemgetter(*self._nesting) if self._nesting else None

        # keep a local cache of rows, b/c it will be iterated repeatedly
        rows = []
        # keep track of each key's membership in a group (i.e., grouping reverse index)
        member_of_list = []
        for row in self._child:
            rows.append(row)
            member_of_list.append({'key': key_getter(row), 'member_of': None})

        # accumulate groups
        groups = {}
        for row in rows:
            key1 = key_getter(row)
            for i, candidate in enumerate(member_of_list):
                if not candidate['member_of'] and self._similarity_fn(key1, candidate['key']) < 1.0:
                    # update the reverse index of groups
                    candidate['member_of'] = key1
                    # update the groups, by getting the corresponding i-th row and adding it to the group
                    if self._nesting:
                        group = groups.get(key1, set())
                        group.add(nested_getter(rows[i]))
                    else:
                        group = rows[i]
                    groups[key1] = group

        # yield groups
        for k, v in groups.items():
            # due to current limitation, assume a length of 1 for both parts of projection
            if self._nesting:
                yield {self._grouping[0]: k, self._nesting[0]: list(v)}
            else:
                # TODO: should probably yield only the grouping key and nothing else
                yield v


class Unnest (PhysicalOperator):
    """Unnest operator that allows user-defined function for custom unnesting."""
    def __init__(self, child, unnest_fn, attribute):
        """
        Creates an Unnest operator.
        :param child: the child operator
        :param unnest_fn: unnesting function that takes a value and yields zero or more values
        :param attribute: name of the attribute to be used as input to the unnest_fn function
        """
        super(Unnest, self).__init__()
        assert isinstance(child, PhysicalOperator)
        self._child = child
        self._unnest_fn = unnest_fn
        self._attribute = attribute

        # update the table definition  -- TODO: may be able to improve this
        table_def = child.description
        self._description = _em.Table.define(
            uuid.uuid1().hex,  # computed relation name for this projection  # todo: placeholder?
            column_defs=table_def['column_definitions'],
            # key_defs=[], -- key defs are empty because the unnested relation should break unique constraints
            fkey_defs=table_def['foreign_keys'],  # TODO: sanity check that unnest attr is not in a fkey
            comment=table_def.get('comment', ''),
            acls=table_def.get('acls', {}),
            acl_bindings=table_def.get('acl_bindings', {}),  # TODO: Filter these and handle renames
            annotations=table_def.get('annotations', {}),  # TODO: Filter these and handle renames
            provide_system=False
        )

    def __iter__(self):
        for row in self._child:
            for atom in self._unnest_fn(row[self._attribute]):
                # for each generated value produced by the unnest function, yield a copied row with the yielded atom
                copy = row.copy()
                copy[self._attribute] = atom
                yield copy


#
# Cross join and Similarity join operators
#

class CrossJoin (PhysicalOperator):
    """Cross-Join operator implementation."""
    def __init__(self, left, right):
        super(CrossJoin, self).__init__()
        assert isinstance(left, PhysicalOperator) and isinstance(right, PhysicalOperator)
        self._left = left
        self._right = right

        syscols = {'RID', 'RCB', 'RMB', 'RCT', 'RMT'}
        left_def = left.description
        right_def = right.description

        # determine conflicting column names in the cross-join
        conflicts = {
            left_col_def['name'] for left_col_def in left_def['column_definitions']
        } & {
            right_col_def['name'] for right_col_def in right_def['column_definitions']
        }
        logger.debug('conflicting column name(s) in crossjoin: %s', conflicts)

        # detemine column defs and renamed columns mappings for the cross-join
        col_defs = []
        self._left_renames, self._right_renames = dict(), dict()
        for table_def, renames in [(left_def, self._left_renames), (right_def, self._right_renames)]:
            for col_def in table_def['column_definitions']:
                col_def = col_def.copy()
                if col_def['name'] in conflicts:
                    old_name = col_def['name']
                    new_name = col_def['name'] = table_def['table_name'] + ':' + col_def['name'] # TODO: bug, this doesn't prevent conflicts when same column name exists in both tables
                    renames[new_name] = old_name
                col_defs.append(col_def)
        logger.debug('columns in crossjoin: %s', col_defs)
        logger.debug('left renames: %s', self._left_renames)
        logger.debug('right renames: %s', self._right_renames)

        # define table representing cross-join
        self._description = _em.Table.define(
            left_def['table_name'] + "_" + right_def['table_name'],  # todo: placeholder
            column_defs=col_defs,
            # TODO: keys: keys from left side of join - tbd
            # TODO: joined acls
            # TODO: joined acl_bindings
            # TODO: joined fkeys
            # TODO: joined annotations
            provide_system=False
        )

    def __iter__(self):
        for left_row in self._left:
            row = self._rename_row_attributes(left_row, self._left_renames, always_copy=True)
            for right_row in self._right:
                right_row = self._rename_row_attributes(right_row, self._right_renames)
                row.update(right_row)
                yield row


class NestedLoopsSimilarityJoin (CrossJoin):
    """Nested loops similarity join operator."""
    def __init__(self, left, right, condition):
        super(NestedLoopsSimilarityJoin, self).__init__(left, right)
        self._condition = condition
        assert isinstance(self._condition, symbols.Similar), "only similarity is supported in the comparison, currently"
        self._target = condition.attribute
        self._domain = condition.domain
        self._synonyms = condition.synonyms
        self._similarity_fn = condition.similarity_fn

    def __iter__(self):
        # TODO use grouping function to improve algorithm by comparing rows within sub-groups only
        right_rows = list(self._right)  # cache a copy of the right rows
        for left_row in self._left:
            target = left_row[self._target]
            best_match_score = 1.0
            best_match_row = None

            # look for the best match in the right_rows
            for right_row in right_rows:
                # compile list of domain name and synonyms
                synonyms = right_row[self._synonyms] if right_row[self._synonyms] is not None else []
                domain_and_synonyms = [right_row[self._domain]] + synonyms
                # test similarity of domain and synonyms
                for term in domain_and_synonyms:
                    similarity = self._similarity_fn(target, term)
                    if similarity < best_match_score:
                        best_match_score = similarity
                        best_match_row = right_row
                        if similarity == 0.0:
                            # found best possible match, stop comparing
                            break
                if best_match_score == 0.0:
                    # found best possible match, stop comparing
                    break

            if best_match_row:
                # only yield a row if a near match was satisfied
                row = self._rename_row_attributes(left_row, self._left_renames, always_copy=True)
                # join with best match
                row.update(
                    self._rename_row_attributes(best_match_row, self._right_renames, always_copy=True))
                yield row
