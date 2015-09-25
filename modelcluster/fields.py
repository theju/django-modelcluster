from __future__ import unicode_literals

from django.core import checks
from django.db import models, IntegrityError, router
from django.db.models.fields.related import ForeignKey, ForeignRelatedObjectsDescriptor, \
    ManyToManyField, ManyRelatedObjectsDescriptor, ReverseManyRelatedObjectsDescriptor
from django.utils.functional import cached_property

from modelcluster.utils import sort_by_fields

from modelcluster.queryset import FakeQuerySet
from modelcluster.models import get_related_model, ClusterableModel


def create_deferring_foreign_related_manager(related, original_manager_cls):
    """
    Create a DeferringRelatedManager class that wraps an ordinary RelatedManager
    with 'deferring' behaviour: any updates to the object set (via e.g. add() or clear())
    are written to a holding area rather than committed to the database immediately.
    Writing to the database is deferred until the model is saved.
    """

    relation_name = related.get_accessor_name()
    rel_field = related.field
    rel_model = get_related_model(related)
    superclass = rel_model._default_manager.__class__

    class DeferringRelatedManager(superclass):
        def __init__(self, instance):
            super(DeferringRelatedManager, self).__init__()
            self.model = rel_model
            self.instance = instance

        def get_live_query_set(self):
            # deprecated; renamed to get_live_queryset to match the move from
            # get_query_set to get_queryset in Django 1.6
            return self.get_live_queryset()

        def get_live_queryset(self):
            """
            return the original manager's queryset, which reflects the live database
            """
            return original_manager_cls(self.instance).get_queryset()

        def get_queryset(self):
            """
            return the current object set with any updates applied,
            wrapped up in a FakeQuerySet if it doesn't match the database state
            """
            try:
                results = self.instance._cluster_related_objects[relation_name]
            except (AttributeError, KeyError):
                return self.get_live_queryset()

            return FakeQuerySet(get_related_model(related), results)

        def get_prefetch_queryset(self, instances, queryset=None):
            if queryset is None:
                db = self._db or router.db_for_read(self.model, instance=instances[0])
                queryset = super(DeferringRelatedManager, self).get_queryset().using(db)

            rel_obj_attr = rel_field.get_local_related_value
            instance_attr = rel_field.get_foreign_related_value
            instances_dict = dict((instance_attr(inst), inst) for inst in instances)

            query = {'%s__in' % rel_field.name: instances}
            qs = queryset.filter(**query)
            # Since we just bypassed this class' get_queryset(), we must manage
            # the reverse relation manually.
            for rel_obj in qs:
                instance = instances_dict[rel_obj_attr(rel_obj)]
                setattr(rel_obj, rel_field.name, instance)
            cache_name = rel_field.related_query_name()
            return qs, rel_obj_attr, instance_attr, False, cache_name

        def get_object_list(self):
            """
            return the mutable list that forms the current in-memory state of
            this relation. If there is no such list (i.e. the manager is returning
            querysets from the live database instead), one is created, populating it
            with the live database state
            """
            try:
                cluster_related_objects = self.instance._cluster_related_objects
            except AttributeError:
                cluster_related_objects = {}
                self.instance._cluster_related_objects = cluster_related_objects

            try:
                object_list = cluster_related_objects[relation_name]
            except KeyError:
                object_list = list(self.get_live_queryset())
                cluster_related_objects[relation_name] = object_list

            return object_list


        def add(self, *new_items):
            """
            Add the passed items to the stored object set, but do not commit them
            to the database
            """
            items = self.get_object_list()

            # Rule for checking whether an item in the list matches one of our targets.
            # We can't do this with a simple 'in' check due to https://code.djangoproject.com/ticket/18864 -
            # instead, we consider them to match IF:
            # - they are exactly the same Python object (by reference), or
            # - they have a non-null primary key that matches
            items_match = lambda item, target: (item is target) or (item.pk == target.pk and item.pk is not None)

            for target in new_items:
                item_matched = False
                for i, item in enumerate(items):
                    if items_match(item, target):
                        # Replace the matched item with the new one. This ensures that any
                        # modifications to that item's fields take effect within the recordset -
                        # i.e. we can perform a virtual UPDATE to an object in the list
                        # by calling add(updated_object). Which is semantically a bit dubious,
                        # but it does the job...
                        items[i] = target
                        item_matched = True
                        break
                if not item_matched:
                    items.append(target)

                # update the foreign key on the added item to point back to the parent instance
                setattr(target, related.field.name, self.instance)

            # Sort list
            if rel_model._meta.ordering and len(items) > 1:
                sort_by_fields(items, rel_model._meta.ordering)

        def remove(self, *items_to_remove):
            """
            Remove the passed items from the stored object set, but do not commit the change
            to the database
            """
            items = self.get_object_list()

            # Rule for checking whether an item in the list matches one of our targets.
            # We can't do this with a simple 'in' check due to https://code.djangoproject.com/ticket/18864 -
            # instead, we consider them to match IF:
            # - they are exactly the same Python object (by reference), or
            # - they have a non-null primary key that matches
            items_match = lambda item, target: (item is target) or (item.pk == target.pk and item.pk is not None)

            for target in items_to_remove:
                # filter items list in place: see http://stackoverflow.com/a/1208792/1853523
                items[:] = [item for item in items if not items_match(item, target)]

        def create(self, **kwargs):
            items = self.get_object_list()
            new_item = get_related_model(related)(**kwargs)
            items.append(new_item)
            return new_item

        def clear(self):
            """
            Clear the stored object set, without affecting the database
            """
            try:
                cluster_related_objects = self.instance._cluster_related_objects
            except AttributeError:
                cluster_related_objects = {}
                self.instance._cluster_related_objects = cluster_related_objects

            cluster_related_objects[relation_name] = []

        def commit(self):
            """
            Apply any changes made to the stored object set to the database.
            Any objects removed from the initial set will be deleted entirely
            from the database.
            """
            if not self.instance.pk:
                raise IntegrityError("Cannot commit relation %r on an unsaved model" % relation_name)

            try:
                final_items = self.instance._cluster_related_objects[relation_name]
            except (AttributeError, KeyError):
                # _cluster_related_objects entry never created => no changes to make
                return

            original_manager = original_manager_cls(self.instance)

            live_items = list(original_manager.get_queryset())
            for item in live_items:
                if item not in final_items:
                    item.delete()

            for item in final_items:
                original_manager.add(item)

            # purge the _cluster_related_objects entry, so we switch back to live SQL
            del self.instance._cluster_related_objects[relation_name]

    return DeferringRelatedManager


class ChildObjectsDescriptor(ForeignRelatedObjectsDescriptor):
    def __init__(self, related):
        super(ChildObjectsDescriptor, self).__init__(related)

    def __get__(self, instance, instance_type=None):
        if instance is None:
            return self

        return self.child_object_manager_cls(instance)

    def __set__(self, instance, value):
        manager = self.__get__(instance)
        manager.clear()
        manager.add(*value)

    @cached_property
    def child_object_manager_cls(self):
        return create_deferring_foreign_related_manager(self.related, self.related_manager_cls)


class ParentalKey(ForeignKey):
    related_accessor_class = ChildObjectsDescriptor

    # Django 1.8 has a check to prevent unsaved instances being assigned to
    # ForeignKeys. Disable it
    allow_unsaved_instance_assignment = True

    # prior to https://github.com/django/django/commit/fa2e1371cda1e72d82b4133ad0b49a18e43ba411
    # ForeignRelatedObjectsDescriptor is hard-coded in contribute_to_related_class -
    # so we need to patch in that change to look up related_accessor_class instead
    def contribute_to_related_class(self, cls, related):
        # Internal FK's - i.e., those with a related name ending with '+' -
        # and swapped models don't get a related descriptor.
        related_model = get_related_model(related)
        if not self.rel.is_hidden() and not related_model._meta.swapped:
            setattr(cls, related.get_accessor_name(), self.related_accessor_class(related))
            if self.rel.limit_choices_to:
                cls._meta.related_fkey_lookups.append(self.rel.limit_choices_to)
        if self.rel.field_name is None:
            self.rel.field_name = cls._meta.pk.name

        # store this as a child field in meta. NB child_relations only contains relations
        # defined to this specific model, not its superclasses
        try:
            cls._meta.child_relations.append(related)
        except AttributeError:
            cls._meta.child_relations = [related]

    def check(self, **kwargs):
        errors = super(ParentalKey, self).check(**kwargs)

        # Check that the desination model is a subclass of ClusterableModel
        if not issubclass(self.rel.to, ClusterableModel):
            errors.append(
                checks.Error(
                    'ParentalKey must point to a subclass of ClusterableModel.',
                    hint='Change {model_name} into a ClusterableModel or use a ForeignKey instead.'.format(
                        model_name=self.rel.to._meta.app_label + '.' + self.rel.to.__name__,
                    ),
                    obj=self,
                    id='modelcluster.E001',
                )
            )

        return errors


def create_deferring_many_related_manager(related, original_manager_cls):
    """Creates a manager that subclasses 'superclass' (which is a Manager)
    and adds behavior for many-to-many related objects."""
    relation_name = related.get_accessor_name()
    rel_field = related.field
    rel_model = get_related_model(related)
    superclass = rel_model._default_manager.__class__

    class DeferringManyRelatedManager(superclass):
        def __init__(self, model=None, query_field_name=None, instance=None, symmetrical=None,
                     source_field_name=None, target_field_name=None, reverse=False,
                     through=None, prefetch_cache_name=None):
            super(DeferringManyRelatedManager, self).__init__()
            self.model = model
            self.query_field_name = query_field_name

            source_field = through._meta.get_field(source_field_name)
            source_related_fields = source_field.related_fields

            self.core_filters = {}
            for lh_field, rh_field in source_related_fields:
                self.core_filters['%s__%s' % (query_field_name, rh_field.name)] = getattr(instance, rh_field.attname)

            self.instance = instance
            self.symmetrical = symmetrical
            self.source_field = source_field
            self.target_field = through._meta.get_field(target_field_name)
            self.source_field_name = source_field_name
            self.target_field_name = target_field_name
            self.reverse = reverse
            self.through = through
            self.prefetch_cache_name = prefetch_cache_name
            self.related_val = source_field.get_foreign_related_value(instance)

        def get_live_queryset(self):
            try:
                return self.instance._prefetched_objects_cache[self.prefetch_cache_name]
            except (AttributeError, KeyError):
                qs = super(DeferringManyRelatedManager, self).get_queryset()
                qs._add_hints(instance=self.instance)
                if self._db:
                    qs = qs.using(self._db)
                return qs._next_is_sticky().filter(**self.core_filters)

        def get_queryset(self):
            try:
                results = self.instance._cluster_related_objects[relation_name]
            except (AttributeError, KeyError):
                return self.get_live_queryset()

            return FakeQuerySet(get_related_model(related), results)

        def get_prefetch_queryset(self, instances, queryset=None):
            if queryset is None:
                db = self._db or router.db_for_read(self.model, instance=instances[0])
                queryset = super(DeferringManyRelatedManager, self).get_queryset().using(db)

            rel_obj_attr = rel_field.get_local_related_value
            instance_attr = rel_field.get_foreign_related_value
            instances_dict = dict((instance_attr(inst), inst) for inst in instances)

            query = {'%s__in' % rel_field.name: instances}
            qs = queryset.filter(**query)
            # Since we just bypassed this class' get_queryset(), we must manage
            # the reverse relation manually.
            for rel_obj in qs:
                instance = instances_dict[rel_obj_attr(rel_obj)]
                setattr(rel_obj, rel_field.name, instance)
            cache_name = rel_field.related_query_name()
            return qs, rel_obj_attr, instance_attr, False, cache_name

        def get_object_list(self):
            """
            return the mutable list that forms the current in-memory state of
            this relation. If there is no such list (i.e. the manager is returning
            querysets from the live database instead), one is created, populating it
            with the live database state
            """
            try:
                cluster_related_objects = self.instance._cluster_related_objects
            except AttributeError:
                cluster_related_objects = {}
                self.instance._cluster_related_objects = cluster_related_objects

            try:
                object_list = cluster_related_objects[relation_name]
            except KeyError:
                object_list = list(self.get_live_queryset())
                cluster_related_objects[relation_name] = object_list

            return object_list

        def add(self, *new_items):
            items = self.get_object_list()

            items_match = lambda item, target: (item is target) or (item.pk == target.pk and item.pk is not None)

            for target in new_items:
                item_matched = False
                for i, item in enumerate(items):
                    if items_match(item, target):
                        # Replace the matched item with the new one. This ensures that any
                        # modifications to that item's fields take effect within the recordset -
                        # i.e. we can perform a virtual UPDATE to an object in the list
                        # by calling add(updated_object). Which is semantically a bit dubious,
                        # but it does the job...
                        items[i] = target
                        item_matched = True
                        break
                if not item_matched:
                    items.append(target)

                # update the foreign key on the added item to point back to the parent instance
                setattr(target, related.field.name, self.instance)

            # Sort list
            if rel_model._meta.ordering and len(items) > 1:
                sort_by_fields(items, rel_model._meta.ordering)

        def remove(self, *items_to_remove):
            """
            Remove the passed items from the stored object set, but do not commit the change
            to the database
            """
            items = self.get_object_list()

            # Rule for checking whether an item in the list matches one of our targets.
            # We can't do this with a simple 'in' check due to https://code.djangoproject.com/ticket/18864 -
            # instead, we consider them to match IF:
            # - they are exactly the same Python object (by reference), or
            # - they have a non-null primary key that matches
            items_match = lambda item, target: (item is target) or (item.pk == target.pk and item.pk is not None)

            for target in items_to_remove:
                # filter items list in place: see http://stackoverflow.com/a/1208792/1853523
                items[:] = [item for item in items if not items_match(item, target)]

        def clear(self):
            """
            Clear the stored object set, without affecting the database
            """
            try:
                cluster_related_objects = self.instance._cluster_related_objects
            except AttributeError:
                cluster_related_objects = {}
                self.instance._cluster_related_objects = cluster_related_objects

            cluster_related_objects[relation_name] = []

        def create(self, **kwargs):
            items = self.get_object_list()
            new_item = get_related_model(related)(**kwargs)
            items.append(new_item)
            return new_item

        def commit(self):
            """
            Apply any changes made to the stored object set to the database.
            Any objects removed from the initial set will be deleted entirely
            from the database.
            """
            if not self.instance.pk:
                raise IntegrityError("Cannot commit relation %r on an unsaved model" % relation_name)

            try:
                final_items = self.instance._cluster_related_objects[relation_name]
            except (AttributeError, KeyError):
                # _cluster_related_objects entry never created => no changes to make
                return

            original_manager = original_manager_cls(self.model, self.query_field_name,
                                                    self.instance, self.symmetrical,
                                                    self.source_field_name,
                                                    self.target_field_name,
                                                    self.reverse,
                                                    self.through,
                                                    self.prefetch_cache_name)

            live_items = list(original_manager.get_queryset())
            for item in live_items:
                if item not in final_items:
                    original_manager.remove(item)

            for item in final_items:
                item.save()
                original_manager.add(item)

            # purge the _cluster_related_objects entry, so we switch back to live SQL
            del self.instance._cluster_related_objects[relation_name]

    return DeferringManyRelatedManager


class ReverseM2MRelatedObjectsDescriptor(ReverseManyRelatedObjectsDescriptor):
    @cached_property
    def related_m2m_manager_cls(self):
        # Dynamically create a class that subclasses the related model's
        # default manager.
        return create_deferring_many_related_manager(
            self.field.rel,
            self.related_manager_cls
        )

    def __get__(self, instance, instance_type=None):
        if instance is None:
            return self

        return self.related_m2m_manager_cls(
            model = self.field.rel.to,
            query_field_name=self.field.related_query_name(),
            prefetch_cache_name=self.field.name,
            instance=instance,
            symmetrical=self.field.rel.symmetrical,
            source_field_name=self.field.m2m_field_name(),
            target_field_name=self.field.m2m_reverse_field_name(),
            reverse=False,
            through=self.field.rel.through,
        )

    def __set__(self, instance, value):
        manager = self.__get__(instance)
        manager.clear()
        manager.add(*value)


class M2MRelatedObjectsDescriptor(ManyRelatedObjectsDescriptor):
    @cached_property
    def related_manager_cls(self):
        # Dynamically create a class that subclasses the related
        # model's default manager.
        return create_deferring_many_related_manager(
            self.related,
            self.related_manager_cls
        )

    def __set__(self, instance, value):
        manager = self.__get__(instance)
        manager.clear()
        manager.add(*value)


class M2MField(ManyToManyField):
    def contribute_to_class(self, cls, name, **kwargs):
        super(M2MField, self).contribute_to_class(cls, name, **kwargs)
        setattr(cls, self.name, ReverseM2MRelatedObjectsDescriptor(self))
        self.field = getattr(cls, self.name)

    def contribute_to_related_class(self, cls, related):
        if not self.rel.is_hidden() and not get_related_model(related)._meta.swapped:
            setattr(cls, related.get_accessor_name(), M2MRelatedObjectsDescriptor(related))
        try:
            related.model._meta.child_relations.append(related)
        except AttributeError:
            related.model._meta.child_relations = [related]
        super(M2MField, self).contribute_to_related_class(cls, related)

    def get_accessor_name(self):
        return self.name

    def _get_val_from_obj(self, obj):
        mgr = super(M2MField, self)._get_val_from_obj(obj)
        return mgr.get_object_list()

    def get_searchable_content(self, value):
        searchable_content = []
        for val in value:
            searchable_content.append(val.pk)
        return searchable_content

    def value_from_object(self, obj):
        qs = getattr(obj, self.attname).get_queryset()
        qs._result_cache = None
        return qs
