from functools import partial

from django.db.models.query import QuerySet

from promise import Promise

from graphene.types import Field, List
from graphene.relay import ConnectionField, PageInfo
from graphql_relay.connection.arrayconnection import connection_from_list_slice

from .settings import graphene_settings
from .utils import maybe_queryset


class DjangoListField(Field):
    def __init__(self, _type, *args, **kwargs):
        super(DjangoListField, self).__init__(List(_type), *args, **kwargs)

    @property
    def model(self):
        return self.type.of_type._meta.node._meta.model

    @staticmethod
    def list_resolver(resolver, root, info, **args):
        return maybe_queryset(resolver(root, info, **args))

    def get_resolver(self, parent_resolver):
        return partial(self.list_resolver, parent_resolver)


class DjangoConnectionField(ConnectionField):
    def __init__(self, *args, **kwargs):
        self.on = kwargs.pop("on", False)
        self.max_limit = kwargs.pop(
            "max_limit", graphene_settings.RELAY_CONNECTION_MAX_LIMIT
        )
        self.enforce_first_or_last = kwargs.pop(
            "enforce_first_or_last",
            graphene_settings.RELAY_CONNECTION_ENFORCE_FIRST_OR_LAST,
        )
        super(DjangoConnectionField, self).__init__(*args, **kwargs)

    @property
    def type(self):
        from .types import DjangoObjectType

        _type = super(ConnectionField, self).type
        assert issubclass(
            _type, DjangoObjectType
        ), "DjangoConnectionField only accepts DjangoObjectType types"
        assert _type._meta.connection, "The type {} doesn't have a connection".format(
            _type.__name__
        )
        return _type._meta.connection

    @property
    def node_type(self):
        return self.type._meta.node

    @property
    def model(self):
        return self.node_type._meta.model

    def get_manager(self):
        if self.on:
            return getattr(self.model, self.on)
        else:
            return self.model._default_manager

    @classmethod
    def merge_querysets(cls, default_queryset, queryset):
        if default_queryset.query.distinct and not queryset.query.distinct:
            queryset = queryset.distinct()
        elif queryset.query.distinct and not default_queryset.query.distinct:
            default_queryset = default_queryset.distinct()
        return queryset & default_queryset

    @classmethod
    def resolve_connection(cls, connection, default_manager, args, iterable):
        if iterable is None:
            iterable = default_manager
        iterable = maybe_queryset(iterable)
        if isinstance(iterable, QuerySet):
            if iterable is not default_manager:
                default_queryset = maybe_queryset(default_manager)
                iterable = cls.merge_querysets(default_queryset, iterable)
            _len = iterable.count()
        else:
            _len = len(iterable)
        connection = connection_from_list_slice(
            iterable,
            args,
            slice_start=0,
            list_length=_len,
            list_slice_length=_len,
            connection_type=connection,
            edge_type=connection.Edge,
            pageinfo_type=PageInfo,
        )
        connection.iterable = iterable
        connection.length = _len
        return connection

    @classmethod
    def connection_resolver(
        cls,
        resolver,
        connection,
        default_manager,
        max_limit,
        enforce_first_or_last,
        root,
        info,
        **kwargs
    ):
        first = kwargs.get("first")
        last = kwargs.get("last")
        if first is not None and first <= 0:
            raise ValueError(
                "`first` argument must be positive, got `{first}`".format(first=first)
            )
        if last is not None and last <= 0:
            raise ValueError(
                "`last` argument must be positive, got `{last}`".format(last=last)
            )
        if enforce_first_or_last and not (first or last):
            raise ValueError(
                "You must provide a `first` or `last` value "
                "to properly paginate the `{info.field_name}` connection.".format(
                    info=info
                )
            )

        if max_limit:
            if first is None and last is None:
                kwargs['first'] = max_limit
            else:
                count = min(i for i in (first, last) if i)
                if count > max_limit:
                    raise ValueError(("Requesting {count} records "
                                      "on the `{info.field_name}` connection "
                                      "exceeds the limit of {max_limit} records.").format(
                                          count=count, info=info, max_limit=max_limit))

        iterable = resolver(root, info, **kwargs)
        on_resolve = partial(cls.resolve_connection, connection, default_manager, kwargs)

        if Promise.is_thenable(iterable):
            return Promise.resolve(iterable).then(on_resolve)

        return on_resolve(iterable)

    def get_resolver(self, parent_resolver):
        return partial(
            self.connection_resolver,
            parent_resolver,
            self.type,
            self.get_manager(),
            self.max_limit,
            self.enforce_first_or_last,
        )
